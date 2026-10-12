"""Stage 3: direct additive rasterization with GDAL, in the manner of the legacy script.

    python stages/stage_03_additive_rasterize.py --config config.local.toml
    python stages/stage_03_additive_rasterize.py --config config.local.toml --year 2022 --uf DF --uf RS

For every resolution and every layer of the year's catalog:

  1. the template (a blank UInt16 raster) is copied to the output name; for the population
     layer a compressed Float32 raster is created on the same grid;
  2. `gdal_rasterize -add` burns the enriched points of ONE UF at a time straight into it,
     in a loop, so every UF is a visible step in the log:
       counts      burn 1 per point matching the layer filter
       population  burn p_i of the points that carry weight
  3. totals and the saturation check read the raster once; the QML style is copied.

There are no work copies and no conversion unless `write_cog` is set. Each resolution is
rasterized on its own; nothing is resampled and the resolutions are never compared with
each other. All UFs of a run are burned into the same rasters, so run every UF you want in
one invocation.
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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        if r.get("encoding", {}).get(key, {}).get("fill_value", 0) != 0:
            problems.append(f"[rasterization.encoding.{key}] fill_value must be 0 (the work rasters are sparse)")
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

def raster_stats(path: Path, tmp_dir: Path) -> tuple[float, float]:
    """(sum of all cells, maximum), with the NoData value switched OFF.

    A saturated count (65535) is the NoData value, so ordinary statistics would skip it and
    hide the saturation. A throw-away VRT that carries no NoData sees every cell.
    """
    vrt = tmp_dir / f"{path.stem}.nodata_off.vrt"
    _run(["gdal_translate", "-q", "-of", "VRT", "-a_nodata", "none", str(path), str(vrt)])
    try:
        info = _info(vrt, stats=True)
    finally:
        vrt.unlink(missing_ok=True)
        Path(str(vrt) + ".aux.xml").unlink(missing_ok=True)
    band = info["bands"][0]
    md = band.get("metadata", {}).get("", {})
    w, h = info["size"]
    mean = float(md.get("STATISTICS_MEAN", band["mean"]))
    return mean * w * h, float(md.get("STATISTICS_MAXIMUM", band["maximum"]))


def check_template(res: str, template: Path, nodata: int, tmp_dir: Path) -> None:
    """The template IS the output raster, so it must be blank and carry the config NoData."""
    info = _info(template)
    if len(info["bands"]) != 1:
        raise ConfigError(f"template for {res} has {len(info['bands'])} bands; it must have exactly one: {template}")
    found = info["bands"][0].get("noDataValue")
    if found is not None and found != nodata:
        raise ConfigError(f"template for {res} has NoData {found}; it must be {nodata} (as in "
                          f"[rasterization.encoding.counts]) or none: the output is a copy of it: {template}")
    _total, maximum = raster_stats(template, tmp_dir)
    if maximum != 0:
        raise ConfigError(f"template for {res} is not blank (largest cell {maximum:g}): points would be "
                          f"added on top of its values: {template}")


def prepare_raster(cfg: dict, res: str, is_float: bool, target: Path) -> None:
    """Counts: a copy of the template. Population: a compressed Float32 raster on its grid."""
    template = Path(cfg["resolutions"][res]["template"])
    if not is_float:
        shutil.copyfile(template, target)
        return
    enc = cfg["rasterization"]["encoding"]["population"]
    _run(["gdal_create", "-q", "-if", str(template), "-bands", "1", "-ot", GDAL_TYPES[enc["dtype"]],
          "-burn", str(enc.get("fill_value", 0)), "-a_nodata", str(enc["nodata"]),
          "-co", "TILED=YES", "-co", "COMPRESS=ZSTD", "-co", "ZSTD_LEVEL=1", "-co", "PREDICTOR=3",
          "-co", "BIGTIFF=YES", str(target)])


def build_raster(cfg: dict, year: int, ufs: list[str], res: str, lay: Layer, work_dir: Path,
                 overwrite: bool) -> dict:
    t0 = time.time()
    r = cfg["rasterization"]
    enc = r["encoding"]["population" if lay.cls == "population" else "counts"]
    prof = r["geotiff_profile"]
    final_type = GDAL_TYPES[enc["dtype"]]
    is_float = final_type.startswith("Float")
    tag = f"{res} {lay.key:<24}"

    final = out_dir(cfg, res, year) / raster_name(cfg, lay, res, year)
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists() and not overwrite:
        raise FileExistsError(f"{final} exists (set execution.overwrite = true to replace)")

    # The raster is built under a temporary name and renamed when complete, so an interrupted
    # run never leaves a half-burned file under the final name.
    tmp = final.with_name(final.stem + ".part.tif")
    tmp.unlink(missing_ok=True)
    log.info("%s preparing the raster (%s)", tag, "new Float32 on the template grid" if is_float else "copy of the template")
    prepare_raster(cfg, res, is_float, tmp)

    where = lay.where()
    try:
        for i, uf in enumerate(ufs, 1):
            t_uf = time.time()
            log.info("%s [%2d/%d] %s burning", tag, i, len(ufs), uf)
            cmd = ["gdal_rasterize", "-q", "-add", "-l", layer_name(cfg, "enrichment", year, uf)]
            cmd += ["-a", lay.source] if lay.source else ["-burn", "1"]
            if where:
                cmd += ["-where", where]
            _run(cmd + [str(enriched_path(cfg, year, uf)), str(tmp)])
            log.info("%s [%2d/%d] %s done in %.0fs", tag, i, len(ufs), uf, time.time() - t_uf)

        log.info("%s reading the raster once for the totals", tag)
        total, maximum = raster_stats(tmp, work_dir)
        if not is_float:
            total = float(round(total))          # integer counts: drop floating-point noise from the mean
        ceiling = {"UInt8": 255, "Byte": 255, "UInt16": 65535, "UInt32": 4294967295}.get(final_type)
        saturated = bool(not is_float and ceiling is not None and maximum >= ceiling)
        if saturated and enc.get("warn_on_saturation", True):
            log.warning("%s a cell reached %d: counts saturate there and are lost (adds stop at the "
                        "type's maximum)", tag, ceiling)

        if prof.get("write_cog"):
            log.info("%s writing the Cloud Optimized GeoTIFF", tag)
            cog = final.with_name(final.stem + ".cog.tif")
            cmd = ["gdal_translate", "-q", "-of", "COG", "-ot", final_type,
                   "-co", f"COMPRESS={prof.get('compress', 'ZSTD')}",
                   "-co", f"PREDICTOR={prof['predictor_float'] if is_float else prof['predictor_integer']}",
                   "-co", f"BLOCKSIZE={prof.get('blocksize', 512)}", "-co", "BIGTIFF=IF_SAFER",
                   "-co", "NUM_THREADS=ALL_CPUS"]
            cmd += (["-co", f"OVERVIEW_RESAMPLING={prof.get('overview_resampling', 'AVERAGE')}"]
                    if prof.get("build_overviews") else ["-co", "OVERVIEWS=NONE"])
            try:
                _run(cmd + [str(tmp), str(cog)])
                os.replace(cog, tmp)
            finally:
                cog.unlink(missing_ok=True)
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)

    shutil.copyfile(style_file(cfg, lay, res), final.with_suffix(".qml"))
    log.info("%s finished in %.0f min: total %s, max %s%s", tag, (time.time() - t0) / 60, f"{total:,.0f}",
             f"{maximum:,.0f}", "  SATURATED" if saturated else "")
    return {"resolution": res, "layer": lay.key, "variable": lay.variable, "variant": lay.variant,
            "spec": lay.spec or "", "class": lay.cls, "file": str(final), "status": "produced",
            "total": total, "maximum": maximum, "saturated": saturated, "seconds": round(time.time() - t0, 1)}


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
    workers = int(r.get("workers", 1))
    try:
        for res in resolutions:                      # the template is the output: check it once
            log.info("checking that the %s template is blank (reads it once)", res)
            check_template(res, Path(cfg["resolutions"][res]["template"]),
                           int(r["encoding"]["counts"]["nodata"]), work_dir)
        log.info("%d rasters (%d layers x %d resolutions), each burned UF by UF over %d UFs, %d at a time",
                 len(tasks), len(layers), len(resolutions), len(ufs), workers)
        log.info("layers per resolution (processed in this order, %d at a time): %s", workers,
                 ", ".join(lay.key for lay in layers))
        results = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(build_raster, cfg, year, ufs, res, lay, work_dir, overwrite)
                       for res, lay in tasks]
            for done, fut in enumerate(as_completed(futures), 1):
                results.append(fut.result())
                el = time.time() - t0
                log.info("[%d/%d rasters finished]  %.0f min elapsed, about %.0f min left", done, len(tasks),
                         el / 60, el / done * (len(tasks) - done) / 60)
        order = {(res, lay.key): i for i, (res, lay) in enumerate(tasks)}
        results.sort(key=lambda x: order[(x["resolution"], x["layer"])])
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
                "total", "maximum", "saturated", "seconds", "file"]
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
