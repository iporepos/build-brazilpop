"""Stage 2: vector enrichment and density allocation.

    python stages/stage_02_vector_enrichment.py --config config.local.toml
    python stages/stage_02_vector_enrichment.py --config config.local.toml --year 2022 --uf DF

Reads the Stage 1 points and the census tracts and microdata of the UF.

  1. Spatial join (point-in-polygon, no key): every point gets its tract. A point
     on a boundary shared by tracts goes to the lowest tract code.
  2. Relational join: tract code -> P_T and H_T from the microdata.
  3. delta_T = P_T / H_T,i, where H_T,i is the number of household points that
     fell in the tract. Each address point i gets p_i, its population: delta_T
     for a household point, 0 for a non-household point. Stage 3 burns p_i.

Nothing is redistributed. A tract with P_T > 0 and no household points, a point
outside every tract, and microdata without a tract get no population. All three
are tracked in the balance file and totalled in the report.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import shapely  # noqa: E402
from pyogrio import read_dataframe, write_dataframe  # noqa: E402

from stages.census_inputs import load_microdata, load_tracts, match_report  # noqa: E402
from stages.config import (  # noqa: E402
    ConfigError, expand, layer_name, load_config, output_dir, target_ufs, validate_stage2,
)

log = logging.getLogger("stage_02")

DEFAULT_CHUNK_ROWS = 500_000
STD_FIELDS = ["is_household", "cod_especie", "cod_subtipo"]

# tract_balance.status
OK = "ok"
NO_HOUSEHOLD_POINTS = "no_household_points"      # P_T > 0 and H_T,i = 0: mass unallocated
EMPTY = "empty"                                  # P_T = 0 and H_T,i = 0
NO_MICRODATA = "no_microdata"                    # tract has no microdata row


def _paths(cfg: dict, year: int, uf: str) -> dict:
    enr, ing = cfg["enrichment"], cfg["ingestion"]
    std_dir, out_dir = output_dir(cfg, "ingestion", year), output_dir(cfg, "enrichment", year)
    return {
        "std": std_dir / expand(ing["output_pattern"], cfg, year=year, uf=uf),
        "layer": layer_name(cfg, "ingestion", year, uf),          # the Stage 1 layer (input)
        "out_layer": layer_name(cfg, "enrichment", year, uf),     # this stage's layer (output)
        "enriched": out_dir / expand(enr["output_pattern"], cfg, year=year, uf=uf),
        "balance": out_dir / expand(enr["balance_pattern"], cfg, year=year, uf=uf),
        "report": out_dir / expand(enr.get("report_pattern", "cnefe_{year}_{uf}_stage2_report.json"),
                                   cfg, year=year, uf=uf),
    }


def _chunks(path: Path, layer: str, chunk_rows: int):
    """Yield the point layer in fid ranges (index seeks, not offsets)."""
    max_fid = int(read_dataframe(
        path, sql=f'SELECT MAX(fid) AS m FROM "{layer}"', sql_dialect="SQLite", read_geometry=False,
    )["m"].iloc[0])
    lo = 0
    while lo < max_fid:
        gdf = read_dataframe(path, layer=layer, where=f"fid > {lo} AND fid <= {lo + chunk_rows}")
        if len(gdf):
            yield gdf.reset_index(drop=True)
        lo += chunk_rows


def _assign(points: gpd.GeoSeries, tree: shapely.STRtree, n_tracts: int) -> tuple[np.ndarray, int]:
    """Tract index per point (-1 = orphan) by point-in-polygon.

    Tracts are indexed in tract-code order, so the minimum index of the tracts
    touching a point is the lowest code. Returns the indices and the number of
    points that touched more than one tract (boundary points).
    """
    n = len(points)
    hit_point, hit_tract = tree.query(points.to_numpy(), predicate="intersects")
    best = np.full(n, n_tracts, dtype=np.int64)
    np.minimum.at(best, hit_point, hit_tract)
    n_boundary = int((np.bincount(hit_point, minlength=n) > 1).sum())
    best[best == n_tracts] = -1
    return best.astype(np.int32), n_boundary


def enrich(cfg: dict, year: int, uf: str, *, overwrite: bool | None = None) -> dict:
    t0 = time.time()
    if overwrite is None:
        overwrite = bool(cfg.get("execution", {}).get("overwrite", False))
    enr = cfg["enrichment"]
    p = _paths(cfg, year, uf)
    chunk_rows = int(enr.get("chunk_rows", DEFAULT_CHUNK_ROWS))
    out_code, out_h, out_p, out_orphan = (
        enr.get("out_tract_code", "cd_setor"), enr.get("out_h_ti", "h_ti"),
        enr.get("out_p_i", "p_i"), enr.get("out_orphan", "orphan"),
    )

    for key in ("enriched", "balance"):
        if p[key].exists() and not overwrite:
            raise FileExistsError(f"{p[key]} exists (set execution.overwrite = true to replace)")
    if not p["std"].is_file():
        raise ConfigError(f"Stage 1 output not found for {uf}: {p['std']}")

    # --- inputs ------------------------------------------------------------------
    tracts = load_tracts(cfg, year, uf).sort_values("tract_code").reset_index(drop=True)
    micro = load_microdata(cfg, year, uf)
    relation = match_report(tracts, micro)
    n_tracts = len(tracts)
    tree = shapely.STRtree(tracts.geometry.to_numpy())

    # --- pass 1: spatial join + household count per tract ------------------------
    assigned: list[np.ndarray] = []
    h_ti = np.zeros(n_tracts, dtype=np.int64)
    n_points = n_households = n_orphans = n_orphan_households = n_boundary = 0
    for gdf in _chunks(p["std"], p["layer"], chunk_rows):
        idx, nb = _assign(gdf.geometry, tree, n_tracts)
        hh = gdf["is_household"].to_numpy() == 1
        placed = idx >= 0
        h_ti += np.bincount(idx[placed & hh], minlength=n_tracts)
        assigned.append(idx)
        n_points += len(gdf)
        n_households += int(hh.sum())
        n_orphans += int((~placed).sum())
        n_orphan_households += int((~placed & hh).sum())
        n_boundary += nb
        log.info("%s pass 1: %s points", uf, f"{n_points:,}")

    # --- relational join and delta_T ---------------------------------------------
    bal = tracts[["tract_code"]].merge(micro, on="tract_code", how="left")
    bal["h_ti"] = h_ti
    p_t = bal["p_t"].to_numpy(dtype="float64")
    has_micro = ~np.isnan(p_t)
    delta = np.full(n_tracts, np.nan)
    ok = has_micro & (h_ti > 0)
    delta[ok] = p_t[ok] / h_ti[ok]
    bal["delta_t"] = delta
    status = np.full(n_tracts, OK, dtype=object)
    status[~has_micro] = NO_MICRODATA
    status[has_micro & (h_ti == 0) & (p_t > 0)] = NO_HOUSEHOLD_POINTS
    status[has_micro & (h_ti == 0) & (p_t == 0)] = EMPTY
    bal["status"] = status
    bal["unallocated_p"] = np.where(status == NO_HOUSEHOLD_POINTS, p_t, 0.0)
    bal["h_diff"] = bal["h_ti"].astype("float64") - bal["h_t"].astype("float64")

    # --- pass 2: write the enriched layer ----------------------------------------
    tmp_enriched = p["enriched"].with_name(p["enriched"].stem + ".tmp.gpkg")
    tmp_balance = p["balance"].with_name(p["balance"].stem + ".tmp.gpkg")
    p["enriched"].parent.mkdir(parents=True, exist_ok=True)
    codes = tracts["tract_code"].to_numpy(dtype=object)
    allocated = 0.0
    orphan_frames: list[gpd.GeoDataFrame] = []
    try:
        for f in (tmp_enriched, tmp_balance):
            f.unlink(missing_ok=True)
        first = True
        for gdf, idx in zip(_chunks(p["std"], p["layer"], chunk_rows), assigned):
            placed = idx >= 0
            safe = np.where(placed, idx, 0)
            hh = gdf["is_household"].to_numpy() == 1
            # p_i is the population of the address: the tract density for a household point
            # (NULL where it is undefined), 0 for a non-household point.
            d = np.where(hh, np.where(placed, delta[safe], np.nan), 0.0)
            out = gpd.GeoDataFrame(
                {
                    "is_household": gdf["is_household"].astype("int16").to_numpy(),
                    "cod_especie": gdf["cod_especie"].astype("Int16").array,   # .array keeps integers
                    "cod_subtipo": gdf["cod_subtipo"].astype("Int16").array,
                    out_code: np.where(placed, codes[safe], None),
                    out_h: pd.array(np.where(placed, h_ti[safe], 0), dtype="Int32"),
                    out_p: d,
                    out_orphan: (~placed).astype("int16"),
                },
                geometry=gdf.geometry.to_numpy(), crs=gdf.crs,
            )
            out.loc[~placed, out_h] = pd.NA
            allocated += float(np.nansum(d))
            if (~placed).any():
                orphan_frames.append(out.loc[~placed, STD_FIELDS + ["geometry"]])
            write_dataframe(out, tmp_enriched, layer=p["out_layer"], driver="GPKG", append=not first)
            first = False

        # --- balance file --------------------------------------------------------
        tb = gpd.GeoDataFrame(bal.assign(h_t=bal["h_t"].astype("float64")),
                              geometry=tracts.geometry.to_numpy(), crs=tracts.crs)
        write_dataframe(tb, tmp_balance, layer="tract_balance", driver="GPKG")
        orphans = (pd.concat(orphan_frames, ignore_index=True) if orphan_frames
                   else gpd.GeoDataFrame({c: pd.Series(dtype="int16") for c in STD_FIELDS},
                                         geometry=[], crs=tracts.crs))
        write_dataframe(gpd.GeoDataFrame(orphans, crs=tracts.crs), tmp_balance,
                        layer="orphan_points", driver="GPKG", append=True, geometry_type="Point")
        duplicates = tracts.attrs.get("duplicates")
        if duplicates is not None and len(duplicates):
            # sidecar: tract codes that were stored as several polygons and merged by code
            write_dataframe(duplicates, tmp_balance, layer="duplicate_tracts", driver="GPKG", append=True)
        unmatched = micro[micro["tract_code"].isin(relation["microdata_without_tract"])]
        if len(unmatched):
            write_dataframe(unmatched.assign(h_t=unmatched["h_t"].astype("float64")), tmp_balance,
                            layer="microdata_unmatched", driver="GPKG", append=True)

        os.replace(tmp_enriched, p["enriched"])
        os.replace(tmp_balance, p["balance"])
    except BaseException:
        for f in (tmp_enriched, tmp_balance):
            f.unlink(missing_ok=True)
        raise

    # --- report ------------------------------------------------------------------
    p_total = relation["p_t_total"]
    unalloc_no_hh = float(bal["unallocated_p"].sum())
    unalloc_no_tract = relation["p_t_total_without_tract"]
    gap = p_total - (allocated + unalloc_no_hh + unalloc_no_tract)
    h_known = has_micro
    rep = {
        "uf": uf, "year": year,
        "points": n_points, "households": n_households,
        "orphan_points": n_orphans, "orphan_households": n_orphan_households,
        "boundary_points": n_boundary,
        "tracts": n_tracts,
        "duplicate_tract_codes_merged": int(tracts.attrs.get("report", {}).get("duplicate_code_groups", 0)),
        "duplicate_tract_features_merged": int(tracts.attrs.get("report", {}).get("duplicate_code_features", 0)),
        "tracts_by_status": {k: int(v) for k, v in pd.Series(status).value_counts().items()},
        "tracts_without_microdata": len(relation["tracts_without_microdata"]),
        "microdata_without_tract": len(relation["microdata_without_tract"]),
        "tracts_h_ti_differs_from_h_t": int((bal["h_diff"].to_numpy()[h_known] != 0).sum()),
        "h_ti_total": int(h_ti.sum()),
        "h_t_total": float(bal["h_t"].astype("float64").sum()),
        "p_total": p_total,
        "p_allocated": allocated,
        "p_unallocated_no_household_points": unalloc_no_hh,
        "p_unallocated_no_tract": unalloc_no_tract,
        "p_unallocated_share": (unalloc_no_hh + unalloc_no_tract) / p_total if p_total else 0.0,
        "conservation_gap": gap,
        "conserved": bool(abs(gap) <= max(1e-6 * p_total, 1e-6)),
        "outputs": {"enriched": str(p["enriched"]), "balance": str(p["balance"])},
        "enriched_layer": p["out_layer"],
        "tract_report": tracts.attrs.get("report", {}),
        "seconds": round(time.time() - t0, 1),
    }
    p["report"].write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    rep["report_path"] = str(p["report"])
    return rep


def run(cfg: dict, year: int, ufs: list[str], overwrite: bool | None = None) -> list[dict]:
    problems = validate_stage2(cfg, year, ufs)
    if problems:
        raise ConfigError("Config validation failed:\n  - " + "\n  - ".join(problems))
    reports = []
    for uf in ufs:
        log.info("Enriching %s %s", year, uf)
        rep = enrich(cfg, year, uf, overwrite=overwrite)
        log.info("%s done in %ss: %s points, %s orphans, P allocated %s of %s (%.4f%% unallocated), conserved=%s",
                 uf, rep["seconds"], f"{rep['points']:,}", f"{rep['orphan_points']:,}",
                 f"{rep['p_allocated']:,.0f}", f"{rep['p_total']:,.0f}",
                 100 * rep["p_unallocated_share"], rep["conserved"])
        reports.append(rep)
    return reports


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
        run(cfg, year, ufs, overwrite=True if args.overwrite else None)
    except (ConfigError, FileExistsError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
