"""Stage 2 inputs: census tracts and tract-level microdata for one UF.

Both sources are national, in the user's own layout and format, and are
declared separately in [enrichment.<year>]:

  tracts      any OGR-readable polygon file (GeoPackage, Shapefile, GeoJSON,
              FlatGeobuf, ...) or a GeoParquet file
  microdata   a table in an OGR-readable file (GeoPackage layer, DBF, ...),
              a Parquet file, or delimited text (.csv / .txt / .tsv)

Nothing is guessed: the code columns, the value columns, and for delimited
text the delimiter, are declared in the config. Only the rows of the requested
UF are kept, selected by the leading digits (UF code) of the tract code.

Tracts are always transformed to EPSG:4326. Microdata has no geometry and is
related to the tracts by the tract code (a relational join, done later).
"""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq
import shapely
from pyogrio import list_layers, read_dataframe, read_info

from stages.config import ConfigError, expand, uf_id, year_block

log = logging.getLogger(__name__)

TARGET_CRS = "EPSG:4326"
PARQUET_EXT = {".parquet", ".pq", ".geoparquet"}
TEXT_EXT = {".csv", ".txt", ".tsv"}
SQL_FILTER_EXT = {".gpkg"}  # formats where the UF filter is pushed down to the file


def _uf_prefix(cfg: dict, uf: str) -> str:
    return f"{uf_id(cfg, uf):02d}"


def _clean_code(s: pd.Series) -> pd.Series:
    return s.astype("string").str.strip()


def _require_columns(wanted: list[str], available: list[str], where: str) -> None:
    missing = [c for c in wanted if c not in available]
    if missing:
        raise ConfigError(f"{where}: columns {missing} not found. Available: {list(available)}")


def _check_code_dtype(s: pd.Series, where: str) -> None:
    if pd.api.types.is_float_dtype(s):
        raise ConfigError(
            f"{where}: the tract code column is floating point, which cannot hold a "
            f"15-digit code safely. Store it as text or integer."
        )


def _ogr_layer(path: Path, layer: str | None, where: str) -> str:
    layers = [str(n) for n, _ in list_layers(path)]
    if layer:
        if layer not in layers:
            raise ConfigError(f"{where}: layer '{layer}' not in {path.name}. Available: {layers}")
        return layer
    if len(layers) == 1:
        return layers[0]
    raise ConfigError(f"{where}: {path.name} has {len(layers)} layers; set the layer key. Available: {layers}")


def _uf_where(col: str, prefix: str) -> str:
    return f"substr(\"{col}\", 1, 2) = '{prefix}'"


def _filter_prefix(df: pd.DataFrame, col: str, prefix: str) -> pd.DataFrame:
    return df[_clean_code(df[col]).str.startswith(prefix, na=False)]


# ----------------------------------------------------------------------------- tracts

def _read_polygons(path: Path, layer: str | None, col: str, prefix: str, what: str, uf: str,
                   out_col: str, name_col: str | None = None) -> gpd.GeoDataFrame:
    """Polygons of one UF (code prefix) in EPSG:4326 with a string code column `out_col`.

    Shared by tracts, districts and municipalities. Polygons that share a code (islands,
    river and lake fragments) are merged into one multi-part geometry; they are listed in
    `gdf.attrs["duplicates"]`. `gdf.attrs["report"]` holds what was found and repaired.
    If `name_col` is given, a `name` column is kept (the first name of a merged code).
    """
    where = f"{what} ({path.name})"
    suffix = path.suffix.lower()
    wanted = [col] + ([name_col] if name_col else [])

    if suffix in PARQUET_EXT:
        layer = None
        gdf = gpd.read_parquet(path)
        _require_columns(wanted, list(gdf.columns), where)
        gdf = _filter_prefix(gdf, col, prefix)[wanted + [gdf.geometry.name]]
        gdf = gdf.set_geometry(gdf.geometry.name)
    else:
        layer = _ogr_layer(path, layer, where)
        _require_columns(wanted, list(read_info(path, layer=layer)["fields"]), f"{where} layer '{layer}'")
        if suffix in SQL_FILTER_EXT:
            gdf = read_dataframe(path, layer=layer, columns=wanted, where=_uf_where(col, prefix))
        else:
            gdf = _filter_prefix(read_dataframe(path, layer=layer, columns=wanted), col, prefix)

    if gdf.crs is None:
        raise ConfigError(f"{where}: the geometry has no CRS")
    _check_code_dtype(gdf[col], where)

    src_crs = gdf.crs.to_string()
    gdf = gdf.rename(columns={col: out_col})
    if name_col:
        gdf = gdf.rename(columns={name_col: "name"})
    gdf[out_col] = _clean_code(gdf[out_col])

    rep = {
        "uf": uf, "source": path.name, "layer": layer,
        "source_crs": src_crs, "target_crs": TARGET_CRS,
        "features": len(gdf),
        "null_code": int(gdf[out_col].isna().sum()),
        "null_geometry": int(gdf.geometry.isna().sum() + gdf.geometry.is_empty.sum()),
    }
    if len(gdf) == 0:
        raise ConfigError(f"{where}: no polygons with code prefix {prefix}")
    if rep["null_code"]:
        raise ConfigError(f"{where}: {rep['null_code']} polygons have no code")

    gdf = gdf[~(gdf.geometry.isna() | gdf.geometry.is_empty)].copy()

    invalid = ~gdf.geometry.is_valid
    rep["invalid_geometry_repaired"] = int(invalid.sum())
    if invalid.any():
        gdf.loc[invalid, "geometry"] = shapely.make_valid(gdf.loc[invalid, "geometry"].to_numpy())

    # One code stored as several polygons: one unit. Merge the parts so the code stays unique
    # and the microdata (one row per code) is related once.
    keep = [out_col] + (["name"] if name_col else []) + ["geometry"]
    dup = gdf[out_col].duplicated(keep=False)
    parts = pd.DataFrame({out_col: pd.Series(dtype="string"), "parts": pd.Series(dtype="int64")})
    if dup.any():
        parts = (gdf[dup].groupby(out_col).size().rename("parts").reset_index()
                 .sort_values(out_col).reset_index(drop=True))
        merged = gdf[dup].dissolve(by=out_col, as_index=False, aggfunc="first")[keep]
        gdf = pd.concat([gdf[~dup][keep], merged], ignore_index=True)
        gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=merged.crs)
        gdf[out_col] = gdf[out_col].astype("string")
        log.warning("%s: %s %s codes were stored as several polygons (%s features); merged by code",
                    uf, f"{len(parts):,}", what, f"{int(dup.sum()):,}")
    rep["duplicate_code_groups"] = len(parts)
    rep["duplicate_code_features"] = int(dup.sum())
    rep["polygons"] = len(gdf)

    gdf = gdf[keep].to_crs(TARGET_CRS)  # always transform, even when the source is already 4326
    gdf = gdf.reset_index(drop=True)
    gdf.attrs["report"] = rep
    gdf.attrs["duplicates"] = parts
    log.info("%s: %s %s read (%s -> %s)", uf, f"{len(gdf):,}", what, src_crs, TARGET_CRS)
    return gdf


def load_tracts(cfg: dict, year: int, uf: str) -> gpd.GeoDataFrame:
    """Tract polygons of one UF in EPSG:4326, with a string `tract_code` column.

    Tract codes stored as several polygons are merged (listed in `gdf.attrs["duplicates"]`);
    `gdf.attrs["report"]` holds what was found and repaired.
    """
    b = year_block(cfg, "enrichment", year)
    path = Path(expand(b["tract_path"], cfg, year=year, uf=uf))
    gdf = _read_polygons(path, b.get("tract_layer"), b["tract_code_column"], _uf_prefix(cfg, uf),
                         "tracts", uf, out_col="tract_code")
    rep = gdf.attrs["report"]
    # names kept from before the refactor
    rep.update(tract_source=rep["source"], tract_layer=rep["layer"], tracts=rep["polygons"])
    return gdf


# Config keys of the district and municipality polygons in [benchmarking.<year>].
BOUNDARY_KEYS = {
    "district": {"path": "district_path", "layer": "district_layer",
                 "code": "district_code_column", "name": "district_name_column"},
    "municipality": {"path": "municipality_path", "layer": "municipality_layer",
                     "code": "municipality_code_column", "name": "municipality_name_column"},
}


def load_boundaries(cfg: dict, year: int, scale: str, uf: str) -> gpd.GeoDataFrame:
    """District or municipality polygons of one UF in EPSG:4326: columns `code`, `name`, geometry.

    The keys are listed in BOUNDARY_KEYS; the name column is optional.
    """
    if scale not in BOUNDARY_KEYS:
        raise ConfigError(f"no boundary layer for scale '{scale}' (expected one of {sorted(BOUNDARY_KEYS)})")
    keys = BOUNDARY_KEYS[scale]
    b = year_block(cfg, "benchmarking", year)
    for k in ("path", "code"):
        if not b.get(keys[k]):
            raise ConfigError(f"[benchmarking.{year}] missing required key '{keys[k]}'")
    path = Path(expand(b[keys["path"]], cfg, year=year, uf=uf))
    return _read_polygons(path, b.get(keys["layer"]), b[keys["code"]], _uf_prefix(cfg, uf),
                          scale, uf, out_col="code", name_col=b.get(keys["name"]) or None)


# ------------------------------------------------------------------------- microdata

def _read_microdata_table(b: dict, path: Path, prefix: str, wanted: list[str], code: str) -> pd.DataFrame:
    where = f"microdata ({path.name})"
    suffix = path.suffix.lower()

    if suffix in PARQUET_EXT:
        _require_columns(wanted, pq.read_schema(path).names, where)
        return _filter_prefix(pd.read_parquet(path, columns=wanted), code, prefix)

    if suffix in TEXT_EXT:
        delimiter = b.get("microdata_delimiter")
        if not delimiter:
            raise ConfigError(f"{where}: delimited text needs microdata_delimiter in the config")
        enc = b.get("microdata_encoding", "utf-8")
        dec = b.get("microdata_decimal", ".")
        header = pd.read_csv(path, sep=delimiter, encoding=enc, nrows=0, dtype=str)
        _require_columns(wanted, list(header.columns), where)
        df = pd.read_csv(path, sep=delimiter, encoding=enc, decimal=dec, usecols=wanted, dtype={code: str})
        return _filter_prefix(df, code, prefix)

    # anything else is read through OGR (GeoPackage layer, DBF, ...)
    layer = _ogr_layer(path, b.get("microdata_layer"), where)
    _require_columns(wanted, list(read_info(path, layer=layer)["fields"]), f"{where} layer '{layer}'")
    if suffix in SQL_FILTER_EXT:
        return read_dataframe(path, layer=layer, columns=wanted, read_geometry=False,
                              where=_uf_where(code, prefix))
    return _filter_prefix(read_dataframe(path, layer=layer, columns=wanted, read_geometry=False), code, prefix)


def load_microdata(cfg: dict, year: int, uf: str) -> pd.DataFrame:
    """Tract-level P_T and H_T of one UF: columns tract_code, p_t, h_t."""
    b = year_block(cfg, "enrichment", year)
    path = Path(expand(b["microdata_path"], cfg, year=year, uf=uf))
    code, pop, hh = b["microdata_code_column"], b["population_column"], b["household_column"]
    prefix = _uf_prefix(cfg, uf)

    df = _read_microdata_table(b, path, prefix, [code, pop, hh], code)
    _check_code_dtype(df[code], f"microdata ({path.name})")

    out = pd.DataFrame({
        "tract_code": _clean_code(df[code]),
        "p_t": pd.to_numeric(df[pop], errors="coerce").astype("float64"),
        "h_t": pd.to_numeric(df[hh], errors="coerce").astype("Float64"),
    }).reset_index(drop=True)

    dup = int(out["tract_code"].duplicated(keep=False).sum())
    if dup:
        raise ConfigError(f"microdata ({path.name}): tract codes must be unique: {dup} rows in duplicate groups")
    if out.empty:
        raise ConfigError(f"microdata ({path.name}): no rows with code prefix {prefix}")
    log.info("%s: %s microdata rows read", uf, f"{len(out):,}")
    return out


def match_report(tracts: gpd.GeoDataFrame, micro: pd.DataFrame) -> dict:
    """How the microdata relates to the tract geometry (relational, by code)."""
    t, m = set(tracts["tract_code"]), set(micro["tract_code"])
    return {
        "tracts": len(t),
        "microdata_rows": len(m),
        "matched": len(t & m),
        "tracts_without_microdata": sorted(t - m),
        "microdata_without_tract": sorted(m - t),
        "p_t_null_rows": int(micro["p_t"].isna().sum()),
        "p_t_total": float(micro["p_t"].sum()),
        "p_t_total_without_tract": float(micro.loc[micro["tract_code"].isin(m - t), "p_t"].sum()),
    }
