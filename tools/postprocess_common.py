"""Shared engine of the raster post-processing tools.

tools/recompress_rasters.py, tools/add_overviews.py and tools/post_process_rasters.py are three
thin entry points over `process_file`; they differ only in which steps they switch on.
Settings come from one JSON file (tools/postprocess_config.py, tools/post_process.example.json).

Steps for one raster, in this fixed order (each one works on a working copy beside the
destination, and the original is replaced only after the result is verified):

    compress  ->  overviews  ->  COG layout

A compressed rewrite drops any overviews, so they are built after it; the COG rewrite goes
last because gdaladdo appends overviews at the end of the file, which is not a COG layout.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SUFFIX = ".recompress.tif"
SKIP_MARKERS = (".part.", ".recompress.", ".cogstep.")

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def _run(cmd: list[str]) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{done.stderr.strip()}")
    return done.stdout


def mb(n: int) -> str:
    return f"{n / 1e6:,.0f} MB"


def minutes(seconds: float) -> str:
    return f"{seconds:.0f}s" if seconds < 90 else f"{seconds / 60:.1f} min"


# ------------------------------------------------------------------------------ inspection

def inspect(path: Path, checksum: bool = False) -> dict:
    """Identity (size, geotransform, type, NoData, optional checksum) plus overview count and COG flag."""
    info = json.loads(_run(["gdalinfo", "-json"] + (["-checksum"] if checksum else []) + [str(path)]))
    band = info["bands"][0]
    out = {"size": info["size"], "gt": [round(v, 9) for v in info["geoTransform"]],
           "type": band["type"], "nodata": band.get("noDataValue")}
    if checksum:
        out["checksum"] = band["checksum"]
    layout = info.get("metadata", {}).get("IMAGE_STRUCTURE", {}).get("LAYOUT")
    return {"identity": out, "overviews": len(band.get("overviews", [])), "cog": layout == "COG"}


def find_rasters(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.tif") if not any(m in p.name for m in SKIP_MARKERS))


# ------------------------------------------------------------------------------ GDAL steps

def predictor_opts(is_float: bool, comp: dict) -> list[str]:
    """`-co PREDICTOR=n`, left out when n is 1 (none): the COG driver warns about an explicit 1."""
    p = comp["predictor_float" if is_float else "predictor_integer"]
    return [] if p == 1 else ["-co", f"PREDICTOR={p}"]


def compress_to(src: Path, dst: Path, is_float: bool, comp: dict, threads: str, cog: bool = False) -> None:
    """Rewrite src compressed and tiled. cog=True writes the COG layout directly (no overviews)."""
    common = ["-co", "COMPRESS=ZSTD", "-co", f"ZSTD_LEVEL={comp['zstd_level']}"] + predictor_opts(is_float, comp) + [
              "-co", "BIGTIFF=IF_SAFER", "-co", f"NUM_THREADS={threads}"]
    bs = comp["blocksize"]
    if cog:
        cmd = ["gdal_translate", "-q", "-of", "COG", "-co", f"BLOCKSIZE={bs}", "-co", "OVERVIEWS=NONE"] + common
    else:
        cmd = ["gdal_translate", "-q", "-of", "GTiff", "-co", "TILED=YES", "-co", f"BLOCKXSIZE={bs}",
               "-co", f"BLOCKYSIZE={bs}"] + common
    _run(cmd + [str(src), str(dst)])


def auto_levels(width: int, height: int, min_side: int, first: int = 2) -> list[int]:
    """first, 2*first, ... until the short side of the overview would fall under min_side."""
    levels, f = [], first
    while min(width, height) / f >= min_side:
        levels.append(f)
        f *= 2
    return levels


def add_overviews_inplace(path: Path, ov: dict, comp: dict, is_float: bool, size: list[int],
                          threads: str) -> list[int]:
    """Build compressed internal overviews in `path` itself. Callers pass a working copy WITHOUT overviews
    (GDAL cannot re-add internal overviews to a file that already has them)."""
    factors = ov["levels"] or auto_levels(size[0], size[1], ov["min_side"], ov["first_level"])
    if not factors:
        return []
    r = ov["resampling"].lower()
    pred = comp["predictor_float" if is_float else "predictor_integer"]
    _run(["gdaladdo", "-q", "-r", r,
          "--config", "COMPRESS_OVERVIEW", "ZSTD", "--config", "ZSTD_LEVEL_OVERVIEW", str(ov["zstd_level"]),
          *(["--config", "PREDICTOR_OVERVIEW", str(pred)] if pred != 1 else []),
          "--config", "GDAL_TIFF_OVR_BLOCKSIZE", str(ov["blocksize"]), "--config", "BIGTIFF_OVERVIEW", "IF_SAFER",
          "--config", "GDAL_NUM_THREADS", threads, str(path), *map(str, factors)])
    return factors


def to_cog(src: Path, dst: Path, is_float: bool, comp: dict, threads: str, has_overviews: bool) -> None:
    """Rewrite src in the COG layout, keeping the overviews it already has (none are invented)."""
    _run(["gdal_translate", "-q", "-of", "COG", "-co", "COMPRESS=ZSTD",
          *predictor_opts(is_float, comp), "-co", f"BLOCKSIZE={comp['blocksize']}",
          "-co", "BIGTIFF=IF_SAFER", "-co", f"NUM_THREADS={threads}",
          "-co", "OVERVIEWS=FORCE_USE_EXISTING" if has_overviews else "OVERVIEWS=NONE", str(src), str(dst)])


# ------------------------------------------------------------------------------ destinations

def destination(cfg: dict, path: Path) -> Path:
    """Where the result goes: the file itself (in place) or the same relative path under output_dir."""
    if not cfg["output_dir"]:
        return path
    return Path(cfg["output_dir"]) / path.relative_to(cfg["input_dir"])


def already_done(path: Path, dest: Path, cfg: dict) -> dict | None:
    """With an output_dir, a file that is already there is left alone unless overwrite is true."""
    if dest != path and dest.exists() and not cfg["overwrite"]:
        return {"before": path.stat().st_size, "after": dest.stat().st_size,
                "status": "exists in output_dir, skipped"}
    return None


def prepare_dest(path: Path, dest: Path, suffix: str = SUFFIX) -> Path:
    """A working name beside the destination (same filesystem, so the final rename is atomic)."""
    if dest != path:
        dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + suffix)
    tmp.unlink(missing_ok=True)
    return tmp


def keep_original(path: Path, dest: Path) -> None:
    """Nothing to gain: in place the file stays; with an output_dir the original is copied across."""
    if dest != path:
        tmp = prepare_dest(path, dest)
        try:
            shutil.copyfile(path, tmp)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)


def copy_style(path: Path, dest: Path) -> None:
    """The .qml sits beside the raster with the same stem; it follows the raster to output_dir."""
    qml = path.with_suffix(".qml")
    if dest != path and qml.exists():
        shutil.copyfile(qml, dest.with_suffix(".qml"))


# ------------------------------------------------------------------------------ one raster

def process_file(path: Path, dest: Path, cfg: dict, threads: str, tag: str,
                 do_compress: bool, do_overviews: bool) -> dict:
    comp, ov, verify, want_cog = cfg["compress"], cfg["overviews"], cfg["verify"], cfg["cog"]
    before = path.stat().st_size
    src = inspect(path, verify)
    is_float = src["identity"]["type"].startswith("Float")

    # a compressed rewrite drops overviews, so with do_overviews they are always rebuilt after it
    need_ov = do_overviews and (do_compress or ov["force"] or src["overviews"] == 0)
    need_cog = want_cog and (do_compress or need_ov or not src["cog"])
    if not do_compress and not need_ov and not need_cog:
        keep_original(path, dest)
        copy_style(path, dest)
        return {"before": before, "after": before, "status": "nothing to do"}

    step_a = prepare_dest(path, dest)                       # compress / overviews work file
    step_b = prepare_dest(path, dest, ".cogstep.tif")      # COG rewrite
    steps: list[str] = []
    try:
        cur, cog_written, factors = path, False, []
        if do_compress:
            direct_cog = need_cog and not need_ov
            log(f"{tag}  compressing" + (" (COG layout)" if direct_cog else ""))
            compress_to(cur, step_a, is_float, comp, threads, cog=direct_cog)
            cur, cog_written = step_a, direct_cog
            steps.append("compressed")
        if need_ov:
            if cur is path:
                if src["overviews"]:                  # force: start again from a base without overviews
                    compress_to(path, step_a, is_float, comp, threads)
                else:
                    shutil.copyfile(path, step_a)
                cur = step_a
            log(f"{tag}  building overviews ({ov['resampling']})")
            factors = add_overviews_inplace(cur, ov, comp, is_float, src["identity"]["size"], threads)
            steps.append(f"overviews {factors}" if factors else "no overviews (raster too small)")
        if need_cog and not cog_written:
            has_ov = bool(factors) or (cur is path and src["overviews"] > 0 and not do_compress)
            log(f"{tag}  writing the COG layout")
            to_cog(cur, step_b, is_float, comp, threads, has_ov)
            cur = step_b
            steps.append("COG")

        log(f"{tag}  verifying")
        out = inspect(cur, verify)
        if out["identity"] != src["identity"]:
            raise RuntimeError("the new file differs from the original (size, grid, type, NoData"
                               + (" or checksum" if verify else "") + "): original kept")
        after = cur.stat().st_size
        rebuilt = ov["force"] and bool(factors) and src["overviews"] > 0     # asked for explicitly
        gained = (out["overviews"] > 0 and src["overviews"] == 0) or (out["cog"] and not src["cog"]) or rebuilt
        too_big = after > before * (1 + cfg["max_growth_pct"] / 100)
        replaced = (after < before or gained) and not too_big
        if replaced:
            os.replace(cur, dest)
        else:
            keep_original(path, dest)
        copy_style(path, dest)
    finally:
        for leftover in (step_a, step_b, step_a.with_name(step_a.name + ".ovr")):
            leftover.unlink(missing_ok=True)
    return {"before": before, "after": after if replaced else before,
            "status": ", ".join(steps) if replaced else (
                f"KEPT ORIGINAL: result would be {100 * (after / before - 1):+.0f}% "
                f"(limit +{cfg['max_growth_pct']:g}%)" if too_big else
                "kept (no gain)" if dest == path else "copied unchanged (no gain)")}


# ------------------------------------------------------------------------------ command line

def add_cli_args(ap: argparse.ArgumentParser) -> None:
    """The arguments every post-processing tool takes: all settings live in the JSON file."""
    ap.add_argument("--config", required=True, help="JSON settings file (see tools/post_process.example.json)")
    ap.add_argument("--dry-run", action="store_true", help="list the files and sizes, change nothing")


def load_settings(ap: argparse.ArgumentParser, path: str) -> dict:
    from tools.postprocess_config import PostProcessConfigError, load
    try:
        return load(path)
    except PostProcessConfigError as exc:
        ap.error(str(exc))


def run(ap: argparse.ArgumentParser, argv: list[str] | None, do_compress: bool | None,
        do_overviews: bool | None) -> int:
    """Entry point shared by the three tools. None = take the switch from the config file."""
    add_cli_args(ap)
    args = ap.parse_args(argv)
    cfg = load_settings(ap, args.config)
    if do_compress is None:
        do_compress = cfg["compress"]["enabled"]
    if do_overviews is None:
        do_overviews = cfg["overviews"]["enabled"]
    if not do_compress and not do_overviews and not cfg["cog"]:
        ap.error("compress, overviews and cog are all off: nothing to do")

    root = Path(cfg["input_dir"])
    files = find_rasters(root)
    plan = [s for s, on in (("compress", do_compress), ("overviews", do_overviews), ("COG layout", cfg["cog"])) if on]
    log(f"{len(files)} rasters under {root} -> "
        f"{cfg['output_dir'] if cfg['output_dir'] else 'in place'}; steps: {' > '.join(plan)}; workers {cfg['workers']}")
    if args.dry_run or not files:
        for p in files:
            i = inspect(p)
            print(f"  {mb(p.stat().st_size):>10}  {i['overviews']} overviews  "
                  f"{'COG' if i['cog'] else 'not COG'}  {p.relative_to(root)}")
        return 0

    workers = cfg["workers"]
    threads = str(max(1, (os.cpu_count() or 2) // workers))
    n, started, finished, failed = len(files), 0, 0, 0
    total_before = total_after = 0
    t_all = time.time()
    counter = threading.Lock()

    def work(p: Path) -> dict:
        nonlocal started
        tag = str(p.relative_to(root))
        dest = destination(cfg, p)
        skipped = already_done(p, dest, cfg)
        with counter:
            started += 1
            idx = started
        log(f"[{idx}/{n}] START {tag}  ({mb(p.stat().st_size)})")
        t0 = time.time()
        r = skipped or process_file(p, dest, cfg, threads, f"        {tag}", do_compress, do_overviews)
        r["seconds"], r["tag"], r["idx"] = time.time() - t0, tag, idx
        return r

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, p): p for p in files}
        for fut in as_completed(futures):
            p = futures[fut]
            finished += 1
            try:
                r = fut.result()
            except Exception as exc:                      # the other files are independent: keep going
                failed += 1
                log(f"[{finished}/{n}] FAILED {p.relative_to(root)}: {exc}")
                continue
            total_before += r["before"]
            total_after += r["after"]
            gain = f" ({100 * (r['after'] / r['before'] - 1):+.0f}%)" if r["before"] else ""
            log(f"[{finished}/{n}] DONE  {r['tag']}  {mb(r['before'])} -> {mb(r['after'])}{gain}  "
                f"{r['status']}  in {minutes(r['seconds'])}")
    saved = f" ({100 * (total_after / total_before - 1):+.0f}%)" if total_before else ""
    log(f"finished {n - failed}/{n} rasters in {minutes(time.time() - t_all)}: "
        f"{mb(total_before)} -> {mb(total_after)}{saved}" + (f", {failed} FAILED" if failed else ""))
    return 1 if failed else 0
