"""Zonal sums with the cell-centre rule, composed from GDAL command-line tools and numpy.

For one raster and one or more polygon sets ("scales") it returns, per polygon, the sum
of the cell values and the number of cells counted.

How a window is processed (a window is a block of raster rows, so memory is bounded):

  1. gdal_translate -srcwin  exports the raster window once, as a raw ENVI file;
  2. gdal_rasterize -a zid   burns the polygons' integer ids onto a grid that is the
                             window cell for cell (GDAL's default rule: a cell belongs
                             to the polygon that contains its centre);
  3. numpy bincount          adds the values per id, skipping NoData cells.

Each cell therefore belongs to at most one polygon of a scale. Overlapping polygons are
not double counted (the last one burned wins), and the sum over the polygons never
exceeds the raster total. Windows that touch no polygon are skipped, and windows are
processed by several worker processes.

Nothing here knows about the pipeline: it takes raster paths and polygon sets.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

ENVI_DTYPES = {1: "u1", 2: "i2", 3: "i4", 4: "f4", 5: "f8", 12: "u2", 13: "u4", 14: "i8", 15: "u8"}
DEFAULT_WINDOW_CELLS = 50_000_000


class ZonalError(Exception):
    """A raster or polygon input cannot be sampled."""


@dataclass(frozen=True)
class ZoneSet:
    """Polygons of one scale: a vector layer with an integer field `zid` (1..n) in EPSG:4326."""
    name: str
    path: str
    layer: str
    n: int                           # number of zones: ids are 1..n
    bounds: tuple[float, float, float, float] | None = None   # union bounds, for window skipping


def _run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise ZonalError(f"{cmd[0]} failed (exit {r.returncode}): {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def raster_info(path: str | Path) -> dict:
    """Size, geotransform, NoData, data type and EPSG of the first band."""
    d = json.loads(_run(["gdalinfo", "-json", str(path)]))
    if not d.get("bands"):
        raise ZonalError(f"{path}: raster has no band")
    b = d["bands"][0]
    return {
        "path": str(path), "width": d["size"][0], "height": d["size"][1], "gt": d["geoTransform"],
        "nodata": b.get("noDataValue"), "dtype": b["type"], "epsg": d.get("stac", {}).get("proj:epsg"),
    }


def _read_envi(base: Path) -> np.ndarray:
    """Read a raw ENVI export (file `base` and header `base`.hdr / with .hdr suffix)."""
    hdr = base.with_suffix(".hdr").read_text()
    fields = {}
    for line in hdr.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            fields[k.strip().lower()] = v.strip()
    code = int(fields["data type"])
    order = ">" if fields.get("byte order", "0").strip() == "1" else "<"
    dt = np.dtype(ENVI_DTYPES[code])
    dt = dt.newbyteorder(order) if dt.itemsize > 1 else dt
    return np.fromfile(base, dtype=dt)


def _valid_mask(val: np.ndarray, nodata) -> np.ndarray:
    ok = np.ones(val.shape, dtype=bool)
    if nodata is not None and not (isinstance(nodata, float) and np.isnan(nodata)):
        ok &= val != np.asarray(nodata).astype(val.dtype)
    if val.dtype.kind == "f":
        ok &= ~np.isnan(val)
    return ok


def _process_windows(raster: str, info: dict, zone_sets: list[ZoneSet], starts: list[tuple[int, int]],
                     tmp_root: str) -> dict:
    """Worker: add up every window of `starts` ((first row, rows)) for all polygon sets."""
    tmp = Path(tempfile.mkdtemp(dir=tmp_root, prefix="w_"))
    w, gt, nodata = info["width"], info["gt"], info["nodata"]
    sums = {z.name: np.zeros(z.n + 1) for z in zone_sets}
    counts = {z.name: np.zeros(z.n + 1, dtype=np.int64) for z in zone_sets}
    total = 0.0
    try:
        for y0, h in starts:
            val_path = tmp / "val.bin"
            _run(["gdal_translate", "-q", "-of", "ENVI", "-srcwin", "0", str(y0), str(w), str(h), raster, str(val_path)])
            val = _read_envi(val_path)
            valid = _valid_mask(val, nodata)
            total += float(val[valid].sum(dtype=np.float64))

            xmin, ymax = gt[0], gt[3] + y0 * gt[5]
            xmax, ymin = xmin + w * gt[1], ymax + h * gt[5]
            for z in zone_sets:
                zon_path = tmp / f"zon_{z.name}.bin"
                # No "-init 0": with the ENVI driver it can overwrite the burn on small grids (verified
                # on GDAL 3.8). A newly created ENVI file is already zero-filled.
                _run(["gdal_rasterize", "-q", "-of", "ENVI", "-a", "zid", "-ot", "Int32",
                      "-te", repr(xmin), repr(ymin), repr(xmax), repr(ymax),
                      "-ts", str(w), str(h), "-l", z.layer, z.path, str(zon_path)])
                zon = _read_envi(zon_path)
                ok = valid & (zon > 0)
                ids = zon[ok]
                sums[z.name] += np.bincount(ids, weights=val[ok], minlength=z.n + 1)
                counts[z.name] += np.bincount(ids, minlength=z.n + 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"sums": sums, "counts": counts, "total": total, "windows": len(starts)}


def rows_with_zones(info: dict, zone_sets: list[ZoneSet]) -> np.ndarray | None:
    """Boolean per raster row: does any polygon set have an extent that reaches this row?"""
    b = [z.bounds for z in zone_sets if z.bounds]
    if len(b) != len(zone_sets):
        return None
    gt, h = info["gt"], info["height"]
    ymax = max(x[3] for x in b)
    ymin = min(x[1] for x in b)
    r0 = int(np.floor((ymax - gt[3]) / gt[5]))
    r1 = int(np.floor((ymin - gt[3]) / gt[5]))
    mask = np.zeros(h, dtype=bool)
    mask[max(0, min(r0, r1)):min(h, max(r0, r1) + 1)] = True
    return mask


def sample_raster(raster: str | Path, zone_sets: list[ZoneSet], *, workers: int = 4,
                  window_cells: int = DEFAULT_WINDOW_CELLS, work_dir: str | Path | None = None) -> dict:
    """Sum `raster` over every polygon of every set.

    Returns {"sums": {scale: array(n+1)}, "counts": {...}, "raster_total": float,
             "windows": int, "windows_skipped": int}. Index 0 of the arrays is "outside all
    polygons" and is not used for results; zone id i is at index i.
    """
    info = raster_info(raster)
    if info["epsg"] != 4326:
        raise ZonalError(f"{raster}: raster must be EPSG:4326, found {info['epsg']}")
    w, h = info["width"], info["height"]
    rows = max(1, int(window_cells // max(w, 1)))
    windows = [(y0, min(rows, h - y0)) for y0 in range(0, h, rows)]

    mask = rows_with_zones(info, zone_sets)
    if mask is not None:
        keep = [(y0, hh) for y0, hh in windows if mask[y0:y0 + hh].any()]
    else:
        keep = windows
    skipped = len(windows) - len(keep)

    tmp_root = tempfile.mkdtemp(dir=work_dir, prefix="zonal_")
    workers = max(1, min(int(workers), len(keep) or 1))
    try:
        batches = [keep[i::workers] for i in range(workers)]
        batches = [b for b in batches if b]
        if len(batches) <= 1:
            results = [_process_windows(str(raster), info, zone_sets, b, tmp_root) for b in batches]
        else:
            with ProcessPoolExecutor(max_workers=len(batches)) as ex:
                futures = [ex.submit(_process_windows, str(raster), info, zone_sets, b, tmp_root) for b in batches]
                results = [f.result() for f in futures]
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    out = {"sums": {z.name: np.zeros(z.n + 1) for z in zone_sets},
           "counts": {z.name: np.zeros(z.n + 1, dtype=np.int64) for z in zone_sets},
           "raster_total": 0.0, "windows": len(windows), "windows_skipped": skipped, "rows_per_window": rows}
    for r in results:
        out["raster_total"] += r["total"]
        for z in zone_sets:
            out["sums"][z.name] += r["sums"][z.name]
            out["counts"][z.name] += r["counts"][z.name]
    return out
