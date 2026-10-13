"""Stage 4: benchmarking of the population surfaces against the census.

    python stages/stage_04_benchmark.py --config config.local.toml --year 2022
    python stages/stage_04_benchmark.py --config config.local.toml --year 2022 --uf DF

The result is for the whole run (all selected UFs together), not one file per UF.

For every scale (tract, district, municipality, state) and every product (BrazilPop and
the external rasters declared under [benchmarking.products]) the cells of the product's
raster are added up per polygon, by the cell-centre rule (see stages/zonal.py), and
compared with the census total `P_K` of the same unit:

  * tracts, districts and municipalities are sampled from their own polygons;
  * a state is the sum of its municipalities (the 2022 database has no state layer);
  * `P_K` is the sum of the tract microdata over the unit (code prefix).

Columns are named `pi_sum_<product>_<year>`, `err_<product>_<year>` and
`pct_err_<product>_<year>` (error against the census); <year> is the year of the product's data
(its `epoch`, the analysis year unless the product says otherwise).

When the tables are written, the charts of tools/plot_benchmark.py are drawn into
`<output_dir>/plots` (see `plots`, `plot_kinds`, `plot_scales` in [benchmarking]; `--no-plots`
skips them). A chart that fails never costs the results: it is reported and the stage ends OK.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from pyogrio import write_dataframe  # noqa: E402

from stages.census_inputs import BOUNDARY_KEYS, load_boundaries, load_microdata, load_tracts  # noqa: E402
from stages.config import ConfigError, expand, load_config, output_dir, target_ufs, year_block  # noqa: E402
from stages.stage_03_additive_rasterize import build_catalog, out_dir as stage3_out_dir, raster_name  # noqa: E402
from stages.zonal import DEFAULT_WINDOW_CELLS, ZonalError, ZoneSet, raster_info, sample_raster  # noqa: E402

log = logging.getLogger("stage_04")

SCALES = ["tract", "district", "municipality", "state"]       # finest to coarsest
POLYGON_SCALES = ["tract", "district", "municipality"]        # scales with their own polygons
CODE_LEN = {"district": 9, "municipality": 7, "state": 2}     # IBGE hierarchy: prefix of the tract code


# ------------------------------------------------------------------------------ config

def products(cfg: dict, year: int) -> dict[str, dict]:
    """Products that apply to this census (analysis) year, in config order.

    A product is one key and one column set across years. Its fields may be set once, and per
    analysis year in a sub-table named by that year, which overrides them:

        [benchmarking.products.ghsl]               label, min_scale: common to every year
        [benchmarking.products.ghsl.2022]          raster, epoch = 2020: used by the 2022 run

    `epoch` is the nominal year of the product data (default: the analysis year). `{epoch}` may be
    used in `raster` and `label`. A product with year sub-tables applies only to those years;
    `years = [...]` still restricts a product without them. The returned spec is flat and carries
    `epoch`.
    """
    out = {}
    for key, spec in cfg.get("benchmarking", {}).get("products", {}).items():
        entries = {k: v for k, v in spec.items() if k.isdigit() and isinstance(v, dict)}
        flat = {k: v for k, v in spec.items() if k not in entries}
        if entries:
            if str(year) not in entries:
                continue
            flat.update(entries[str(year)])
        years = flat.get("years")
        if years and year not in years:
            continue
        flat["epoch"] = flat.get("epoch", year)
        for field in ("raster", "label"):
            if isinstance(flat.get(field), str):
                flat[field] = flat[field].replace("{epoch}", str(flat["epoch"]))
        out[key] = flat
    return out


def product_rasters(cfg: dict, year: int, key: str, spec: dict, scales: list[str]) -> dict[str, Path]:
    """{scale: raster path}: which raster feeds each scale for this product."""
    if spec.get("raster"):
        p = Path(expand(spec["raster"], cfg, year=year))
        return {s: p for s in scales}
    if not spec.get("stage3_resolution"):
        raise ConfigError(f"[benchmarking.products.{key}] needs `raster` or `stage3_resolution`")
    pop = [lay for lay in build_catalog(cfg, year) if lay.cls == "population"]
    if len(pop) != 1:
        raise ConfigError(f"expected one population layer in the {year} catalog, found {len(pop)}")
    by_scale = spec.get("resolution_by_scale", {})
    result = {}
    for s in scales:
        res = by_scale.get(s, spec["stage3_resolution"])
        result[s] = stage3_out_dir(cfg, res, year) / raster_name(cfg, pop[0], res, year)
    return result


def resolution_used(spec: dict, scale: str) -> str:
    """Label of what was sampled at a scale: the Stage 3 resolution, or 'external'."""
    if spec.get("raster"):
        return "external"
    return spec.get("resolution_by_scale", {}).get(scale, spec.get("stage3_resolution", ""))


def validate(cfg: dict, year: int, ufs: list[str]) -> list[str]:
    problems: list[str] = []
    b = cfg.get("benchmarking", {})
    scales = b.get("scales", SCALES)
    bad = [s for s in scales if s not in SCALES]
    if bad:
        problems.append(f"[benchmarking] scales {bad} are not among {SCALES}")
    if "state" in scales and "municipality" not in scales:
        problems.append("[benchmarking] scale 'state' is the sum of municipalities: add 'municipality'")
    try:
        output_dir(cfg, "benchmarking", year)
    except ConfigError as e:
        problems.append(str(e))

    try:
        yb = year_block(cfg, "benchmarking", year)
    except ConfigError as e:
        return problems + [str(e)]
    for sc in (s for s in scales if s in BOUNDARY_KEYS):
        keys = BOUNDARY_KEYS[sc]
        for k in ("path", "code"):
            if not yb.get(keys[k]):
                problems.append(f"[benchmarking.{year}] missing required key '{keys[k]}'")
        if yb.get(keys["path"]):
            for uf in ufs:
                f = Path(expand(yb[keys["path"]], cfg, year=year, uf=uf))
                if not f.is_file():
                    problems.append(f"{keys['path']} not found for {uf}: {f}")

    prods = products(cfg, year)
    if not prods:
        problems.append(f"no product in [benchmarking.products] applies to {year}")
    for key, spec in prods.items():
        if spec.get("min_scale", "tract") not in SCALES:
            problems.append(f"[benchmarking.products.{key}] min_scale must be one of {SCALES}")
        try:
            for scale, path in product_rasters(cfg, year, key, spec, scales).items():
                if not path.is_file():
                    problems.append(f"raster of '{key}' for scale {scale} not found: {path}")
        except ConfigError as e:
            problems.append(str(e))
    return problems


# ------------------------------------------------------------------------------ inputs

def load_polygons(cfg: dict, year: int, ufs: list[str], scales: list[str]) -> dict[str, gpd.GeoDataFrame]:
    """{scale: polygons with columns `code` (and `name`) in EPSG:4326}, for the selected UFs."""
    out = {}
    for scale in (s for s in POLYGON_SCALES if s in scales):
        parts = []
        for uf in ufs:
            g = load_tracts(cfg, year, uf).rename(columns={"tract_code": "code"}) if scale == "tract" \
                else load_boundaries(cfg, year, scale, uf)
            parts.append(g[[c for c in ("code", "name", "geometry") if c in g.columns]])
        gdf = pd.concat(parts, ignore_index=True)
        out[scale] = gpd.GeoDataFrame(gdf, geometry="geometry", crs="EPSG:4326")
    return out


def load_truth(cfg: dict, year: int, ufs: list[str]) -> pd.DataFrame:
    """Tract microdata of the selected UFs: tract_code, p_t, h_t."""
    return pd.concat([load_microdata(cfg, year, uf) for uf in ufs], ignore_index=True)


def truth_by_scale(truth: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """{scale: DataFrame(code, p_k, h_k, n_tracts)}: the census total of every unit."""
    out = {"tract": truth.rename(columns={"tract_code": "code", "p_t": "p_k", "h_t": "h_k"})
           .assign(n_tracts=1)[["code", "p_k", "h_k", "n_tracts"]]}
    for scale, n in CODE_LEN.items():
        g = truth.assign(code=truth["tract_code"].str[:n]).groupby("code", as_index=False).agg(
            p_k=("p_t", "sum"), h_k=("h_t", "sum"), n_tracts=("tract_code", "size"))
        out[scale] = g
    return out


def make_zone_set(gdf: gpd.GeoDataFrame, scale: str, work: Path) -> tuple[ZoneSet, np.ndarray]:
    """Number the polygons 1..n by code, write them, and return the set and the code per id."""
    g = gdf.sort_values("code").reset_index(drop=True)
    g["zid"] = np.arange(1, len(g) + 1, dtype="int32")
    path = work / f"zones_{scale}.gpkg"
    write_dataframe(g[["zid", "code", "geometry"]], path, layer="z", driver="GPKG")
    zs = ZoneSet(scale, str(path), "z", len(g), tuple(float(v) for v in g.total_bounds))
    return zs, g["code"].to_numpy(dtype=object)


# ----------------------------------------------------------------------------- tables

def column(prefix: str, key: str, epoch) -> str:
    """Name of a product column: `<prefix>_<product>_<year of the product data>`."""
    return f"{prefix}_{key}_{epoch}"


def add_product(table: pd.DataFrame, key: str, sums: pd.Series, epoch) -> pd.DataFrame:
    """Add pi_sum / err / pct_err columns (suffixed with the product's year) for one product, aligned on `code`."""
    t = table.copy()
    s = column("pi_sum", key, epoch)
    t[s] = t["code"].map(sums).astype("float64")
    err = t[s] - t["p_k"]
    t[column("err", key, epoch)] = err
    t[column("pct_err", key, epoch)] = np.where(t["p_k"] > 0, 100.0 * err / t["p_k"].where(t["p_k"] > 0), np.nan)
    return t


def unit_metrics(table: pd.DataFrame, key: str, epoch) -> dict:
    """Accuracy of one product at one scale, over the units that have a census total."""
    s = column("pi_sum", key, epoch)
    d = table[table["p_k"].notna() & table[s].notna()]
    err = d[s] - d["p_k"]
    pos = d["p_k"] > 0
    n = len(d)
    population_product, population_census = float(d[s].sum()), float(d["p_k"].sum())
    return {
        "units": n,
        "units_with_population": int(pos.sum()),
        "bias": float(err.mean()) if n else np.nan,
        "mae": float(err.abs().mean()) if n else np.nan,
        "rmse": float(np.sqrt((err ** 2).mean())) if n else np.nan,
        "mape": float((err.abs()[pos] / d["p_k"][pos]).mean() * 100) if pos.any() else np.nan,
        "population_census": population_census,
        "population_product": population_product,
        "population_difference": population_product - population_census,       # product - census
        "ratio_product_to_census": population_product / population_census if population_census else np.nan,
    }


# ------------------------------------------------------------------------------- driver

def benchmark(cfg: dict, year: int, ufs: list[str], overwrite: bool | None = None) -> dict:
    t0 = time.time()
    if overwrite is None:
        overwrite = bool(cfg.get("execution", {}).get("overwrite", False))
    problems = validate(cfg, year, ufs)
    if problems:
        raise ConfigError("Config validation failed:\n  - " + "\n  - ".join(problems))

    b = cfg["benchmarking"]
    scales = [s for s in SCALES if s in b.get("scales", SCALES)]
    prods = products(cfg, year)
    out = output_dir(cfg, "benchmarking", year)
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "master": out / expand(b.get("master_pattern", "benchmark_{year}_tracts.gpkg"), cfg, year=year),
        "metrics": out / expand(b.get("metrics_pattern", "benchmark_{year}_metrics.csv"), cfg, year=year),
        "report": out / expand(b.get("report_pattern", "benchmark_{year}_report.json"), cfg, year=year),
    }
    tables_path = {s: out / b.get("table_pattern", "benchmark_{year}_{scale}.csv").format(year=year, scale=s)
                   for s in scales}
    for p in [paths["master"], paths["metrics"], *tables_path.values()]:
        if p.exists() and not overwrite:
            raise FileExistsError(f"{p} exists (set execution.overwrite = true to replace)")

    # --- inputs -------------------------------------------------------------------
    polygons = load_polygons(cfg, year, ufs, scales)
    truth = truth_by_scale(load_truth(cfg, year, ufs))
    log.info("polygons: %s", {s: len(g) for s, g in polygons.items()})

    work = Path(tempfile.mkdtemp(dir=out, prefix=".work_"))
    try:
        zone_sets, codes = {}, {}
        for scale, gdf in polygons.items():
            zone_sets[scale], codes[scale] = make_zone_set(gdf, scale, work)

        # --- sampling: one pass per distinct raster, all of its scales together -------
        sums: dict[str, dict[str, pd.Series]] = {k: {} for k in prods}
        raster_reports = []
        for key, spec in prods.items():
            per_scale = product_rasters(cfg, year, key, spec, [s for s in scales if s in zone_sets])
            by_raster: dict[Path, list[str]] = {}
            for scale, path in per_scale.items():
                by_raster.setdefault(path, []).append(scale)
            for path, sc in by_raster.items():
                ts = time.time()
                r = sample_raster(path, [zone_sets[s] for s in sc], workers=int(b.get("workers", 4)),
                                  window_cells=int(b.get("window_cells", DEFAULT_WINDOW_CELLS)), work_dir=work)
                for s in sc:
                    sums[key][s] = pd.Series(r["sums"][s][1:], index=codes[s])
                raster_reports.append({
                    "product": key, "raster": str(path), "scales": sc,
                    # total of the windows that were read (windows without polygons are skipped)
                    "raster_total_in_windows": r["raster_total"],
                    "inside_polygons": {s: float(r["sums"][s][1:].sum()) for s in sc},
                    "windows": r["windows"], "windows_skipped": r["windows_skipped"],
                    "seconds": round(time.time() - ts, 1)})
                log.info("%s <- %s (%s): %.1fs", key, path.name, ",".join(sc), time.time() - ts)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # --- state = sum of municipalities --------------------------------------------
    if "state" in scales:
        for key in prods:
            m_sum = sums[key]["municipality"]
            sums[key]["state"] = m_sum.groupby(m_sum.index.str[:2]).sum()

    # --- tables -------------------------------------------------------------------
    alias = {f"{int(v):02d}": k for k, v in cfg["ufs"].items()}
    tables: dict[str, pd.DataFrame] = {}
    for scale in scales:
        if scale == "state":
            codes_s = sorted(set(truth["state"]["code"]) | set(sums[next(iter(prods))]["state"].index))
            base = pd.DataFrame({"code": codes_s}).merge(truth["state"], on="code", how="left")
            base.insert(1, "name", base["code"].map(alias))
        else:
            g = polygons[scale]
            base = g[["code"] + (["name"] if "name" in g.columns else [])].copy()
            base = base.drop_duplicates("code").merge(truth[scale], on="code", how="left")
        for key in prods:
            base = add_product(base, key, sums[key][scale], prods[key]["epoch"])
        tables[scale] = base

    # --- metrics: a product enters a scale from its min_scale up --------------------
    rows = []
    for scale in scales:
        for key, spec in prods.items():
            if SCALES.index(scale) < SCALES.index(spec.get("min_scale", "tract")):
                continue
            rows.append({"scale": scale, "product": key, "product_year": spec["epoch"],
                         "resolution": resolution_used(spec, scale), **unit_metrics(tables[scale], key, spec["epoch"])})
    metrics = pd.DataFrame(rows)

    # --- outputs ------------------------------------------------------------------
    for scale, tbl in tables.items():
        tbl.to_csv(tables_path[scale], index=False)
    metrics.to_csv(paths["metrics"], index=False)
    if "tract" in tables:
        master = polygons["tract"][["code", "geometry"]].drop_duplicates("code").merge(
            tables["tract"], on="code", how="left").rename(columns={"code": "tract_code"})
        master["district_code"] = master["tract_code"].str[:9]
        master["municipality_code"] = master["tract_code"].str[:7]
        master["uf_code"] = master["tract_code"].str[:2]
        paths["master"].unlink(missing_ok=True)
        write_dataframe(gpd.GeoDataFrame(master, geometry="geometry", crs="EPSG:4326"), paths["master"],
                        layer="tracts", driver="GPKG")

    # --- report -------------------------------------------------------------------
    p_total = float(truth["tract"]["p_k"].sum())
    without_polygon = {s: sorted(set(truth[s]["code"]) - set(tables[s]["code"]))
                       for s in scales if s in truth and s != "state"}
    report = {
        "year": year, "ufs": ufs, "scales": scales, "products": list(prods),
        "labels": {k: spec.get("label", k) for k, spec in prods.items()},
        "epochs": {k: spec["epoch"] for k, spec in prods.items()},
        "polygons": {s: len(g) for s, g in polygons.items()},
        "units_without_census_total": {s: int(t["p_k"].isna().sum()) for s, t in tables.items()},
        "census_units_without_polygon": {s: len(c) for s, c in without_polygon.items()},
        # codes that have a census total but no polygon (water bodies, for instance), first 100
        "census_units_without_polygon_codes": {s: c[:100] for s, c in without_polygon.items()},
        "census_total": p_total,
        "rasters": raster_reports,
        "metrics_rows": len(metrics),
        "outputs": {"master": str(paths["master"]), "metrics": str(paths["metrics"]),
                    "tables": {s: str(p) for s, p in tables_path.items()}},
        "seconds": round(time.time() - t0, 1),
    }
    paths["report"].write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(paths["report"])
    report["_tables"], report["_metrics"] = tables, metrics
    return report


def plot_settings(cfg: dict, year: int) -> dict:
    """The settings of tools/plot_benchmark.py (see tools/plot_config.py) for the tables of this run."""
    b = cfg.get("benchmarking", {})
    return {
        "input_dir": str(output_dir(cfg, "benchmarking", year)),
        "year": year,
        "version": str(cfg.get("project", {}).get("version", "")),
        "kinds": list(b.get("plot_kinds", ["scatter", "hexbin"])),
        "scales": list(b.get("plot_scales", ["tract", "district", "municipality"])),
        "mode": b.get("plot_mode", "light"),
        "dpi": int(b.get("plot_dpi", 150)),
        "products": {k: {f: spec[f] for f in ("label", "min_scale") if spec.get(f)}
                     for k, spec in products(cfg, year).items()} or None,
        "table_pattern": b.get("table_pattern", "benchmark_{year}_{scale}.csv"),
        "metrics_pattern": b.get("metrics_pattern", "benchmark_{year}_metrics.csv"),
        "report_pattern": b.get("report_pattern", "benchmark_{year}_report.json"),
    }


def run_plots(cfg: dict, year: int) -> list[Path]:
    """Draw the charts of tools/plot_benchmark.py from the tables just written. Never raises:
    the tables are the result of the stage and a missing font or backend must not discard them."""
    if not cfg.get("benchmarking", {}).get("plots", True):
        return []
    try:
        from tools.plot_benchmark import build
        written = build(plot_settings(cfg, year))
        log.info("charts: %d files in %s", len(written), written[0].parent if written else "-")
        return written
    except Exception as e:                                   # noqa: BLE001 - see the docstring
        log.error("the tables are written but the charts failed (%s: %s); run tools/plot_benchmark.py "
                  "--config <plot json> to retry", type(e).__name__, e)
        return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--year", type=int, help="census year (default: execution.target_year)")
    ap.add_argument("--uf", action="append", help="UF acronym, repeatable (default: all UFs found)")
    ap.add_argument("--overwrite", action="store_true", help="replace existing outputs")
    ap.add_argument("--no-plots", action="store_true", help="do not draw the charts after the tables")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
        logging.basicConfig(level=cfg.get("execution", {}).get("log_level", "INFO"),
                            format="%(asctime)s %(levelname)s %(message)s")
        year = args.year or cfg["execution"]["target_year"]
        ufs = target_ufs(cfg, year, args.uf)
        if not ufs:
            raise ConfigError("No UFs to process")
        rep = benchmark(cfg, year, ufs, overwrite=True if args.overwrite else None)
        log.info("done in %ss: %s", rep["seconds"], rep["report_path"])
        if not args.no_plots:
            run_plots(cfg, year)
    except (ConfigError, FileExistsError, ZonalError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
