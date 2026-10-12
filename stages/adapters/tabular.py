"""Shared Stage 1 logic for tabular CNEFE sources (delimited text or Parquet).

A vintage adapter (parse_cnefe_2022.py, parse_cnefe_2010.py, ...) only says
which census year it serves; everything else is declared in [ingestion.<year>].

Reads one UF's raw address file in the user's own layout and column names and
writes the standardized point layer: geometry, is_household, cod_especie,
cod_subtipo. Every source row is kept (one address can carry several records).

The source is chosen by file extension:
  .parquet / .pq        read in record batches (no delimiter or encoding needed)
  anything else         delimited text; `delimiter` and `encoding` are declared

Household status is not hard-coded here. It comes from `household = true` in
the year's dictionary in data/cnefe_codes.json.
"""

from __future__ import annotations

import logging
import os
import time
from collections import Counter
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq
from pyogrio import write_dataframe

from stages.config import ConfigError, expand, layer_name, output_dir, year_block

log = logging.getLogger(__name__)

DEFAULT_CHUNK_ROWS = 500_000
PARQUET_EXT = {".parquet", ".pq"}


class UnknownCodeError(Exception):
    """A code in the raw data is missing from [codes.<year>] and fail_on_unknown_codes is set."""


def _dictionary(cfg: dict, year: int) -> tuple[dict[int, dict], dict[int, dict]]:
    codes = cfg["codes"][str(year)]
    especie = {int(k): v for k, v in codes.get("especie", {}).items()}
    subtipo = {int(k): v for k, v in codes.get("subtipo", {}).items()}
    return especie, subtipo


def _require(wanted: list[str], available: list[str], year: int, name: str) -> None:
    missing = [c for c in wanted if c not in available]
    if missing:
        raise ConfigError(
            f"Columns {missing} (from [ingestion.{year}]) not found in {name}. Available: {list(available)}"
        )


def _chunks(path: Path, block: dict, wanted: list[str], chunk_rows: int, year: int):
    """Yield the wanted columns of the source as DataFrames of at most chunk_rows rows."""
    if path.suffix.lower() in PARQUET_EXT:
        pf = pq.ParquetFile(path)
        _require(wanted, pf.schema_arrow.names, year, path.name)
        for batch in pf.iter_batches(batch_size=chunk_rows, columns=wanted):
            yield batch.to_pandas()
        return
    sep, enc = block.get("delimiter", ";"), block.get("encoding", "utf-8")
    header = pd.read_csv(path, sep=sep, encoding=enc, nrows=0, dtype=str)
    _require(wanted, list(header.columns), year, path.name)
    yield from pd.read_csv(path, sep=sep, encoding=enc, dtype=str, usecols=wanted, chunksize=chunk_rows)


def ingest(cfg: dict, uf: str, year: int, *, overwrite: bool | None = None) -> dict:
    """Standardize one UF of census `year`. Returns a report dict."""
    block = year_block(cfg, "ingestion", year)
    especie_dict, subtipo_dict = _dictionary(cfg, year)
    household_codes = {c for c, v in especie_dict.items() if v.get("household")}
    fail_unknown = bool(cfg.get("ingestion", {}).get("fail_on_unknown_codes", False))
    if overwrite is None:
        overwrite = bool(cfg.get("execution", {}).get("overwrite", False))

    src = Path(expand(block["input"], cfg, year=year, uf=uf))
    out_dir = output_dir(cfg, "ingestion", year)
    ing = cfg["ingestion"]
    out_path = out_dir / expand(ing["output_pattern"], cfg, year=year, uf=uf)
    inv_path = out_dir / expand(ing["inventory_pattern"], cfg, year=year, uf=uf)
    layer = layer_name(cfg, "ingestion", year, uf)
    out_dir.mkdir(parents=True, exist_ok=True)

    if out_path.exists() and not overwrite:
        raise FileExistsError(f"{out_path} exists (set execution.overwrite = true to replace)")

    col_lon, col_lat = block["col_lon"], block["col_lat"]
    col_species = block["col_species"]
    col_subtype = block.get("col_subtype") or None
    wanted = [col_lon, col_lat, col_species] + ([col_subtype] if col_subtype else [])
    if not src.is_file():
        raise ConfigError(f"Input file not found for {uf}: {src}")

    chunk_rows = int(block.get("chunk_rows", DEFAULT_CHUNK_ROWS))
    tmp_path = out_path.with_name(out_path.stem + ".tmp.gpkg")
    tmp_path.unlink(missing_ok=True)

    rep = {
        "uf": uf, "input": str(src), "output": str(out_path),
        "rows_read": 0, "rows_written": 0, "dropped_invalid_coords": 0,
        "null_species": 0, "households": 0, "households_without_subtype": 0,
    }
    species_counts: Counter = Counter()
    subtype_counts: Counter = Counter()
    t0 = time.time()
    first = True

    try:
        for chunk in _chunks(src, block, wanted, chunk_rows, year):
            rep["rows_read"] += len(chunk)

            lon = pd.to_numeric(chunk[col_lon], errors="coerce")
            lat = pd.to_numeric(chunk[col_lat], errors="coerce")
            species = pd.to_numeric(chunk[col_species], errors="coerce").astype("Int16")
            if col_subtype:
                subtype = pd.to_numeric(chunk[col_subtype], errors="coerce").astype("Int16")
            else:
                subtype = pd.Series(pd.array([pd.NA] * len(chunk), dtype="Int16"), index=chunk.index)

            # Inventory covers every row read, including rows later dropped.
            species_counts.update(species.value_counts(dropna=False).to_dict())
            subtype_counts.update(subtype.value_counts(dropna=False).to_dict())

            valid = lon.between(-180, 180) & lat.between(-90, 90)
            rep["dropped_invalid_coords"] += int((~valid).sum())
            lon, lat, species, subtype = lon[valid], lat[valid], species[valid], subtype[valid]
            if lon.empty:
                continue

            is_hh = species.isin(household_codes).fillna(False).astype("uint8")
            rep["null_species"] += int(species.isna().sum())
            rep["households"] += int(is_hh.sum())
            if col_subtype:
                rep["households_without_subtype"] += int(((is_hh == 1) & subtype.isna()).sum())

            gdf = gpd.GeoDataFrame(
                {"is_household": is_hh.to_numpy(),
                 # .array, not .to_numpy(): keeps the nullable integer type (NumPy would make floats)
                 "cod_especie": species.array,
                 "cod_subtipo": subtype.array},
                geometry=gpd.points_from_xy(lon.to_numpy(), lat.to_numpy()),
                crs="EPSG:4326",
            )
            write_dataframe(gdf, tmp_path, layer=layer, driver="GPKG", append=not first)
            first = False
            rep["rows_written"] += len(gdf)
            log.info("%s: %s rows read, %s written", uf, f"{rep['rows_read']:,}", f"{rep['rows_written']:,}")

        unknown_species = {int(c): n for c, n in species_counts.items()
                           if not pd.isna(c) and int(c) not in especie_dict}
        unknown_subtype = {int(c): n for c, n in subtype_counts.items()
                           if not pd.isna(c) and subtipo_dict is not None and int(c) not in subtipo_dict}
        rep["unknown_species"], rep["unknown_subtype"] = unknown_species, unknown_subtype
        _write_inventory(inv_path, species_counts, subtype_counts, especie_dict, subtipo_dict)

        for name, unknown in (("species", unknown_species), ("subtype", unknown_subtype)):
            if unknown:
                log.warning("%s: %s codes missing from [codes.%s]: %s", uf, name, year, unknown)
        if fail_unknown and (unknown_species or unknown_subtype):
            raise UnknownCodeError(
                f"{uf}: codes missing from [codes.{year}]: species={unknown_species} subtype={unknown_subtype}"
            )
        if first:
            raise ConfigError(f"{src.name}: no valid rows were written")

        os.replace(tmp_path, out_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    rep["seconds"] = round(time.time() - t0, 1)
    return rep


def _write_inventory(path: Path, species: Counter, subtype: Counter,
                     especie_dict: dict, subtipo_dict: dict) -> None:
    rows = []
    for field, counts, dictionary in (("cod_especie", species, especie_dict),
                                      ("cod_subtipo", subtype, subtipo_dict)):
        for code, n in sorted(counts.items(), key=lambda kv: (pd.isna(kv[0]), kv[0] if not pd.isna(kv[0]) else 0)):
            null = pd.isna(code)
            entry = None if null else dictionary.get(int(code))
            rows.append({
                "field": field,
                "code": "" if null else int(code),
                "count": n,
                "label": "" if entry is None else entry.get("label", ""),
                "in_dictionary": "" if null else int(entry is not None),
            })
    pd.DataFrame(rows).to_csv(path, index=False)
