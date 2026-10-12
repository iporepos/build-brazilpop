"""Stage 1, 2010 adapter, on a tiny synthetic Parquet table (species 1-7, no typology)."""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stages.adapters import parse_cnefe_2010 as adapter  # noqa: E402
from stages.config import ConfigError, load_config  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def make_cfg(tmp_path: Path, *, drop_column: str | None = None) -> dict:
    df = pd.DataFrame({
        "lon": [-47.9, -47.9, -47.8, -47.7, -47.6, float("nan")],
        "lat": [-15.8, -15.8, -15.7, -15.6, -15.5, -15.4],
        "especie": pd.array([1, 1, 2, 4, 8, 1], dtype="int8"),              # 8 does not exist in 2010
        "especie_2022": pd.array([1, 1, 2, 4, 8, 1], dtype="int8"),         # other columns are ignored
        "tipo_domicilio_2022": pd.array([101, 103, 104, None, None, 101], dtype="Int16"),
        "metodo": ["face"] * 6,
    })
    if drop_column:
        df = df.drop(columns=[drop_column])
    src = tmp_path / "raw" / "53.parquet"
    src.parent.mkdir()
    df.to_parquet(src)
    cfg = load_config(REPO_ROOT / "config.example.toml")     # real dictionaries and catalog
    cfg["ingestion"].update(output_dir=str(tmp_path / "out"))
    cfg["ingestion"]["2010"] = {
        "adapter": "stages/adapters/parse_cnefe_2010.py",
        "input": str(tmp_path / "raw" / "{uf_id}.parquet"),
        "col_lon": "lon", "col_lat": "lat",
        "col_species": "especie", "col_subtype": "",
        "chunk_rows": 4,                      # several batches
    }
    cfg["execution"]["overwrite"] = False
    return cfg


def test_parquet_is_read_and_mapped_to_the_canonical_contract(tmp_path):
    cfg = make_cfg(tmp_path)
    rep = adapter.ingest(cfg, "DF")

    assert rep["rows_read"] == 6
    assert rep["dropped_invalid_coords"] == 1          # the NaN longitude
    assert rep["rows_written"] == 5                    # the repeated point is kept
    assert rep["households"] == 3                      # species 1, 1, 2
    assert rep["unknown_species"] == {8: 1}            # religious does not exist in 2010: flagged, never a household
    assert rep["households_without_subtype"] == 0

    out = Path(rep["output"])
    assert out.name == "cnefe_2010_DF_standardized.gpkg"
    gdf = gpd.read_file(out, layer="addresses")
    assert list(gdf.columns) == ["is_household", "cod_especie", "cod_subtipo", "geometry"]
    assert gdf.is_household.tolist() == [1, 1, 1, 0, 0]
    assert gdf.cod_subtipo.isna().all()                # 2010 has no household typology
    info = pyogrio.read_info(out, layer="addresses")
    assert dict(zip(info["fields"], info["dtypes"])) == {
        "is_household": "int16", "cod_especie": "int16", "cod_subtipo": "int16"}


def test_2010_dictionary_has_species_1_to_7_and_no_subtypes(tmp_path):
    cfg = make_cfg(tmp_path)
    assert sorted(cfg["codes"]["2010"]["especie"], key=int) == ["1", "2", "3", "4", "5", "6", "7"]
    assert cfg["codes"]["2010"]["subtipo"] == {}
    specs = [lay["spec"] for lay in cfg["rasterization"]["catalog"]["2010"]["layers"]]
    assert specs == [f"{i:02d}" for i in range(1, 8)]          # ADDRESS-01..07 only: no 08, no HOUSEHOLD-1xx

    adapter.ingest(cfg, "DF")
    inv = pd.read_csv(tmp_path / "out" / "cnefe_2010_DF_code_inventory.csv")
    assert inv[(inv.field == "cod_especie") & (inv.code == 8)].in_dictionary.iloc[0] == 0


def test_missing_column_lists_the_available_ones(tmp_path):
    cfg = make_cfg(tmp_path, drop_column="especie")
    with pytest.raises(ConfigError, match="especie") as e:
        adapter.ingest(cfg, "DF")
    assert "lon" in str(e.value)
