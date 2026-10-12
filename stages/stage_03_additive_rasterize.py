"""Stage 3: direct additive rasterization with GDAL.

    python stages/stage_03_additive_rasterize.py --config config.local.toml
    python stages/stage_03_additive_rasterize.py --config config.local.toml --year 2022 --uf DF --uf RS

For every resolution and every layer of the year's catalog:

  1. gdal_create builds a zeroed raster on the grid of the resolution's template
  2. gdal_rasterize -add burns the enriched points of every selected UF into it
       counts      burn 1 per point matching the layer filter
       population  burn p_i, reading only the points with p_i > 0
  3. gdal_translate writes the final COG (and the QML style sidecar is copied)

Each resolution is rasterized on its own; nothing is resampled and the
resolutions are never compared with each other. All UFs of a run are burned into
the same rasters, so run every UF you want in one invocation.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from stages.config import ConfigError, expand, layer_name, load_config, output_dir, target_ufs  # noqa: E402

log = logging.getLogger("stage_03")

GDAL_TYPES = {"uint8": "Byte", "uint16": "UInt16", "uint32": "UInt32", "float32": "Float32", "float64": "Float64"}
TOOLS = ("gdal_create", "gdal_rasterize", "gdal_translate", "gdalinfo")


# ------------------------------------------------------------------------------ GDAL

def _run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed (exit {r.returncode}): {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def _info(path: Path, stats: bool = False) -> dict:
    return json.loads(_run(["gdalinfo", "-json", *(["-stats"] if stats else []), str(path)]))


def _epsg(info: dict) -> int | None:
    return info.get("stac", {}).get("proj:epsg")


def raster_total(path: Path) -> tuple[float, float]:
    """(sum of all cell values, maximum) of a raster without NoData cells."""
    info = _info(path, stats=True)
    band = info["bands"][0]
    w, h = info["size"]
    # The JSON "mean" is rounded; the metadata string keeps full precision.
    md = band.get("metadata", {}).get("", {})
    mean = float(md.get("STATISTICS_MEAN", band["mean"]))
    maximum = float(md.get("STATISTICS_MAXIMUM", band["maximum"]))
    return mean * w * h, maximum


def _clip_counts(work: Path, ceiling: int) -> Path:
    """Return a copy of a UInt32 count raster with every cell clipped to `ceiling`.

    GDAL's gdal_translate -scale does not clamp, so the (rare) clipping is done
    on the raw ENVI export, in place and by memory map.
    """
    import numpy as np

    raw = work.with_suffix(".clip.bin")
    _run(["gdal_translate", "-q", "-of", "ENVI", str(work), str(raw)])
    hdr = raw.with_suffix(".hdr").read_text()
    order = ">" if "byte order = 1" in hdr else "<"
    cells = np.memmap(raw, dtype=np.dtype(f"{order}u4"), mode="r+")
    np.minimum(cells, ceiling, out=cells)
    cells.flush()
    del cells
    return raw


# ---------------------------------------------------------------------------- catalog

@dataclass(frozen=True)
class Layer:
    variable: str
    variant: str
    spec: str | None
    cls: str                       # "count" or "population"
    flag: str | None = None
    species: tuple[int, ...] = ()
    subtype: tuple[int, ...] = ()
    source: str | None = None      # attribute burned instead of 1

    @property
    def key(self) -> str:
        return f"{self.variable}_{self.variant}" + (f"-{self.spec}" if self.spec else "")

    def where(self) -> str | None:
        conds = []
        if self.source:                         # population: p_i is 0 for non-households, so p_i > 0
            conds.append(f"{self.source} > 0")  # reads only the points that add anything (NULL is excluded)
        if self.flag:
            conds.append(f"{self.flag} = 1")
        if self.species:
            conds.append(f"cod_especie IN ({', '.join(map(str, self.species))})")
        if self.subtype:
            conds.append(f"cod_subtipo IN ({', '.join(map(str, self.subtype))})")
        return " AND ".join(dict.fromkeys(conds)) or None


def build_catalog(cfg: dict, year: int) -> list[Layer]:
    cat = cfg["rasterization"]["catalog"]
    entries = list(cat.get("core", {}).get("layers", [])) + list(cat.get(str(year), {}).get("layers", []))
    layers = [Layer(
        variable=e["variable"], variant=e["variant"], spec=e.get("spec"), cls=e["class"],
        flag=e.get("flag"), species=tuple(e.get("species", ())), subtype=tuple(e.get("subtype", ())),
        source=e.get("source"),
    ) for e in entries]

    codes = cfg.get("codes", {}).get(str(year), {})
    especie, subtipo = codes.get("especie", {}), codes.get("subtipo", {})
    problems = []
    for lay in layers:
        problems += [f"{lay.key}: species {c} not in [codes.{year}.especie]" for c in lay.species if str(c) not in especie]
        problems += [f"{lay.key}: subtype {c} not in [codes.{year}.subtipo]" for c in lay.subtype if str(c) not in subtipo]
        if lay.cls not in ("count", "population"):
            problems.append(f"{lay.key}: unknown class '{lay.cls}'")
    keys = [lay.key for lay in layers]
    problems += [f"duplicate layer {k}" for k in sorted({k for k in keys if keys.count(k) > 1})]
    if problems:
        raise ConfigError("Layer catalog is invalid:\n  - " + "\n  - ".join(problems))
    return layers


def raster_name(cfg: dict, lay: Layer, res: str, year: int) -> str:
    p, r = cfg["project"], cfg["rasterization"]
    pattern = r["name_pattern_spec"] if lay.spec else r["name_pattern_nospec"]
    return pattern.format(
        provider=p["provider"], version=p["version"], variable=lay.variable, variant=lay.variant,
        spec=lay.spec, resolution=res, extension=p["extension"], year=year,
    )


def out_dir(cfg: dict, res: str, year: int) -> Path:
    return output_dir(cfg, "rasterization", year) / res


def style_file(cfg: dict, lay: Layer, res: str) -> Path:
    """QML template by convention: {address|population}_{resolution}.qml in data/styles."""
    cls = "population" if lay.cls == "population" else "address"
    return Path(cfg["data"]["styles"]) / f"{cls}_{res}.qml"


def enriched_path(cfg: dict, year: int, uf: str) -> Path:
    return output_dir(cfg, "enrichment", year) / expand(cfg["enrichment"]["output_pattern"], cfg, year=year, uf=uf)


# -------------------------------------------------------------------------- validation

TEMPLATE_TYPE = "UInt16"
TEMPLATE_EPSG = 4326


def _check_template(res: str, path: Path) -> list[str]:
    """A template must be a UInt16 raster in EPSG:4326 (only its grid is used)."""
    info = _info(path)
    problems = []
    if not info.get("bands"):
        return [f"template for {res} has no band: {path}"]
    found = info["bands"][0].get("type")
    if found != TEMPLATE_TYPE:
        problems.append(f"template for {res} must be {TEMPLATE_TYPE}, found {found}: {path}")
    epsg = _epsg(info)
    if epsg != TEMPLATE_EPSG:
        problems.append(f"template for {res} must be EPSG:{TEMPLATE_EPSG}, found {epsg}: {path}")
    return problems

def validate(cfg: dict, year: int, ufs: list[str]) -> list[str]:
    problems = [f"GDAL tool not found on PATH: {t}" for t in TOOLS if shutil.which(t) is None]
    r = cfg.get("rasterization", {})
    for res in r.get("resolutions", []):
        block = cfg.get("resolutions", {}).get(res)
        if not block or not block.get("template"):
            problems.append(f"[resolutions.{res}] needs a template path")
            continue
        t = Path(block["template"])
        if not t.is_file():
            problems.append(f"template for {res} not found: {t}")
        elif shutil.which("gdalinfo"):
            problems += _check_template(res, t)
    for uf in ufs:
        f = enriched_path(cfg, year, uf)
        if not f.is_file():
            problems.append(f"Stage 2 output not found for {uf}: {f} (run Stage 2 first)")
    p_i = cfg.get("enrichment", {}).get("out_p_i", "p_i")
    for e in list(r.get("catalog", {}).get("core", {}).get("layers", [])) + \
            list(r.get("catalog", {}).get(str(year), {}).get("layers", [])):
        if e.get("source") and e["source"] != p_i:
            problems.append(f"catalog layer {e.get('variant')}: source '{e['source']}' is not the address "
                            f"population column [enrichment].out_p_i = '{p_i}'")
    for key in ("counts", "population"):
        if r.get("encoding", {}).get(key, {}).get("dtype") not in GDAL_TYPES:
            problems.append(f"[rasterization.encoding.{key}] dtype must be one of {sorted(GDAL_TYPES)}")
    for res in r.get("resolutions", []):
        for cls in ("address", "population"):
            f = Path(cfg["data"]["styles"]) / f"{cls}_{res}.qml"
            if not f.is_file():
                problems.append(f"style template not found: {f}")
    try:
        output_dir(cfg, "rasterization", year)
    except ConfigError as e:
        problems.append(str(e))
    return problems


# ------------------------------------------------------------------------------ build

def build_raster(cfg: dict, year: int, ufs: list[str], res: str, lay: Layer, work_dir: Path,
                 overwrite: bool) -> dict:
    t0 = time.time()
    r = cfg["rasterization"]
    enc = r["encoding"]["population" if lay.cls == "population" else "counts"]
    prof = r["geotiff_profile"]
    final_type = GDAL_TYPES[enc["dtype"]]
    is_float = final_type.startswith("Float")

    final = out_dir(cfg, res, year) / raster_name(cfg, lay, res, year)
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists() and not overwrite:
        raise FileExistsError(f"{final} exists (set execution.overwrite = true to replace)")

    work = work_dir / f"{res}_{lay.key}.tif"
    # Wide working type: counts cannot wrap (UInt32) and population does not drift (Float64).
    work_type = "Float64" if is_float else "UInt32"
    # The template only supplies the grid. Whatever else it carries is NOT inherited:
    #   -bands 1    one band, even if the template has several (e.g. an RGB Byte raster);
    #   -a_nodata   an explicit value that a cell can never hold. Without it the work raster
    #               inherits the template's NoData (255 for a Byte template), and a cell that
    #               holds exactly that count would be invisible to the statistics, the
    #               consistency checks and the saturation test.
    sentinel = str(enc["nodata"]) if is_float else str(2 ** 32 - 1)
    _run(["gdal_create", "-q", "-if", cfg["resolutions"][res]["template"], "-bands", "1", "-ot", work_type,
          "-burn", str(enc.get("fill_value", 0)), "-a_nodata", sentinel, "-co", "TILED=YES",
          "-co", "SPARSE_OK=TRUE", "-co", "BIGTIFF=IF_SAFER", str(work)])

    where = lay.where()
    for uf in ufs:
        cmd = ["gdal_rasterize", "-q", "-add", "-l", layer_name(cfg, "enrichment", year, uf)]
        cmd += ["-a", lay.source] if lay.source else ["-burn", "1"]
        if where:
            cmd += ["-where", where]
        _run(cmd + [str(enriched_path(cfg, year, uf)), str(work)])

    total, maximum = raster_total(work)
    if not is_float:
        total = float(round(total))          # integer counts: drop floating-point noise from the mean
    ceiling = int(enc["nodata"]) - 1 if not is_float else None
    clipped = bool(ceiling is not None and maximum > ceiling)
    if clipped and enc.get("warn_on_saturation", True):
        log.warning("%s %s: maximum %d exceeds %d; clipped to the ceiling", res, lay.key, maximum, ceiling)

    source = work
    if clipped:
        source = _clip_counts(work, ceiling)
    tmp = final.with_name(final.stem + ".tmp.tif")
    cmd = ["gdal_translate", "-q", "-of", "COG", "-ot", final_type, "-a_nodata", str(enc["nodata"])]
    cmd += ["-co", f"COMPRESS={prof.get('compress', 'ZSTD')}",
            "-co", f"PREDICTOR={prof['predictor_float'] if is_float else prof['predictor_integer']}",
            "-co", f"BLOCKSIZE={prof.get('blocksize', 512)}", "-co", "BIGTIFF=IF_SAFER"]
    if prof.get("build_overviews"):
        cmd += ["-co", f"OVERVIEW_RESAMPLING={prof.get('overview_resampling', 'AVERAGE')}"]
    else:
        cmd += ["-co", "OVERVIEWS=NONE"]
    try:
        _run(cmd + [str(source), str(tmp)])
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
        work.unlink(missing_ok=True)
        if source != work:
            for f in (source, source.with_suffix(".hdr"), Path(str(source) + ".aux.xml")):
                f.unlink(missing_ok=True)

    shutil.copyfile(style_file(cfg, lay, res), final.with_suffix(".qml"))
    log.info("%s %-28s total %s  max %s%s", res, lay.key, f"{total:,.0f}", f"{maximum:,.0f}",
             "  CLIPPED" if clipped else "")
    return {"resolution": res, "layer": lay.key, "variable": lay.variable, "variant": lay.variant,
            "spec": lay.spec or "", "class": lay.cls, "file": str(final), "status": "produced",
            "total": total, "maximum": maximum, "clipped": clipped, "seconds": round(time.time() - t0, 1)}


# ------------------------------------------------------------------------------ checks

def run_checks(cfg: dict, results: list[dict], stage2: dict[str, float]) -> list[dict]:
    flags = cfg["rasterization"].get("checks", {})
    tol = float(flags.get("population_relative_tolerance", 1e-5))
    checks: list[dict] = []

    def add(res, name, expected, actual, rel_tol=0.0, note=""):
        ok = abs(actual - expected) <= max(rel_tol * abs(expected), 0.5 if rel_tol == 0 else 0)
        checks.append({"resolution": res, "check": name, "expected": expected, "actual": actual,
                       "difference": actual - expected, "ok": bool(ok), "note": note})

    for res in sorted({r["resolution"] for r in results}):
        tot = {r["layer"]: r["total"] for r in results if r["resolution"] == res}
        if flags.get("counts_vs_stage2_points", True) and stage2:
            if "CNEFE_ADDRESS-00" in tot:
                add(res, "ADDRESS-00 total == Stage 2 points", stage2["points"], tot["CNEFE_ADDRESS-00"],
                    note="a gap means points outside the template extent")
            if "CNEFE_HOUSEHOLD-000" in tot:
                add(res, "HOUSEHOLD-000 total == Stage 2 households", stage2["households"],
                    tot["CNEFE_HOUSEHOLD-000"], note="a gap means points outside the template extent")
        if flags.get("household_total_equals_species_1_plus_2", True) and \
                {"CNEFE_HOUSEHOLD-000", "CNEFE_ADDRESS-01", "CNEFE_ADDRESS-02"} <= tot.keys():
            add(res, "HOUSEHOLD-000 == ADDRESS-01 + ADDRESS-02", tot["CNEFE_ADDRESS-01"] + tot["CNEFE_ADDRESS-02"],
                tot["CNEFE_HOUSEHOLD-000"])
        subs = [v for k, v in tot.items() if k.startswith("CNEFE_HOUSEHOLD-1")]
        if flags.get("subtype_sum_vs_household_total", True) and subs and "CNEFE_HOUSEHOLD-000" in tot:
            add(res, "sum(HOUSEHOLD-1xx) == HOUSEHOLD-000", tot["CNEFE_HOUSEHOLD-000"], sum(subs),
                note="a residual means households with an unknown subtype")
        if flags.get("population_total_vs_microdata", True) and stage2 and "POPULATION_RESIDENT" in tot:
            add(res, "POPULATION total == Stage 2 allocated P_T", stage2["p_allocated"],
                tot["POPULATION_RESIDENT"], rel_tol=tol)
    return checks


def stage2_totals(cfg: dict, year: int, ufs: list[str]) -> dict[str, float]:
    """Points, households and allocated population summed over the Stage 2 reports of the UFs."""
    pattern = cfg["enrichment"].get("report_pattern", "cnefe_{year}_{uf}_stage2_report.json")
    tot = {"points": 0.0, "households": 0.0, "p_allocated": 0.0}
    for uf in ufs:
        f = output_dir(cfg, "enrichment", year) / expand(pattern, cfg, year=year, uf=uf)
        if not f.is_file():
            log.warning("Stage 2 report not found for %s: consistency checks against Stage 2 are skipped", uf)
            return {}
        rep = json.loads(f.read_text(encoding="utf-8"))
        for k in tot:
            tot[k] += rep[k]
    return tot


# ------------------------------------------------------------------------------ driver

def run(cfg: dict, year: int, ufs: list[str], overwrite: bool | None = None) -> dict:
    if overwrite is None:
        overwrite = bool(cfg.get("execution", {}).get("overwrite", False))
    problems = validate(cfg, year, ufs)
    if problems:
        raise ConfigError("Config validation failed:\n  - " + "\n  - ".join(problems))

    r = cfg["rasterization"]
    layers = build_catalog(cfg, year)
    resolutions = list(r["resolutions"])
    tasks = [(res, lay) for res in resolutions for lay in layers]
    for res, lay in tasks:
        f = out_dir(cfg, res, year) / raster_name(cfg, lay, res, year)
        if f.exists() and not overwrite:
            raise FileExistsError(f"{f} exists (set execution.overwrite = true to replace)")

    t0 = time.time()
    work_root = output_dir(cfg, "rasterization", year)
    work_root.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix=".work_", dir=work_root))
    try:
        with ThreadPoolExecutor(max_workers=int(r.get("workers", 1))) as pool:
            futures = [pool.submit(build_raster, cfg, year, ufs, res, lay, work_dir, overwrite) for res, lay in tasks]
            results = [f.result() for f in futures]
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    checks = run_checks(cfg, results, stage2_totals(cfg, year, ufs))
    for c in checks:
        (log.info if c["ok"] else log.warning)("check %s %s: expected %s actual %s %s", c["resolution"],
                                              c["check"], f"{c['expected']:,.2f}", f"{c['actual']:,.2f}",
                                              "OK" if c["ok"] else f"MISMATCH ({c['note']})")

    report = {"year": year, "ufs": ufs, "rasters": len(results), "seconds": round(time.time() - t0, 1),
              "all_checks_ok": all(c["ok"] for c in checks), "checks": checks, "layers": results}
    m = r.get("manifest", {})
    if m.get("write", True):
        base = Path(m["pattern"].format(output_dir=output_dir(cfg, "rasterization", year), year=year))
        base.parent.mkdir(parents=True, exist_ok=True)
        cols = ["resolution", "layer", "variable", "variant", "spec", "class", "status",
                "total", "maximum", "clipped", "seconds", "file"]
        with open(base, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for row in results:
                w.writerow({c: row[c] for c in cols})
        if m.get("report_pattern"):
            Path(m["report_pattern"].format(output_dir=output_dir(cfg, "rasterization", year), year=year)).write_text(
                json.dumps(report, indent=2), encoding="utf-8")
        report["manifest"] = str(base)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--year", type=int, help="census year (default: execution.target_year)")
    ap.add_argument("--uf", action="append", help="UF acronym, repeatable (default: execution.target_ufs)")
    ap.add_argument("--overwrite", action="store_true", help="replace existing outputs")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
        logging.basicConfig(level=cfg.get("execution", {}).get("log_level", "INFO"),
                            format="%(asctime)s %(levelname)s %(message)s")
        year = args.year or cfg["execution"]["target_year"]
        ufs = target_ufs(cfg, year, args.uf)
        if not ufs:
            raise ConfigError("No UFs to process")
        rep = run(cfg, year, ufs, overwrite=True if args.overwrite else None)
        log.info("%s rasters in %ss, all checks ok: %s", rep["rasters"], rep["seconds"], rep["all_checks_ok"])
    except (ConfigError, FileExistsError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
