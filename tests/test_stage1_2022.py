"""Stage 1, 2022 adapter, on a tiny synthetic CNEFE file (no real data needed)."""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stages.adapters import parse_cnefe_2022 as adapter  # noqa: E402
from stages.config import ConfigError, expand, validate_stage1  # noqa: E402

HEADER = "LONGITUDE;LATITUDE;COD_ESPECIE;COD_TIPO_ESPECI;OTHER"
ROWS = [
    "-47.9;-15.8;1;101;a",   # household, house
    "-47.9;-15.8;1;103;b",   # same point, second record: kept
    "-47.8;-15.7;2;104;c",   # collective household
    "-47.7;-15.6;4;;d",      # education, no subtype
    "-47.6;-15.5;9;;e",      # species not in dictionary
    "-47.5;-15.4;;;f",       # missing species
    ";-15.3;1;101;g",        # missing longitude: dropped
    "999;-15.2;1;101;h",     # out of range: dropped
]


def make_cfg(tmp_path: Path, *, fail_unknown: bool = False) -> dict:
    src = tmp_path / "raw" / "53_DF.csv"
    src.parent.mkdir()
    src.write_text("\n".join([HEADER, *ROWS]) + "\n", encoding="utf-8")
    return {
        "ufs": {"DF": 53},
        "execution": {"overwrite": False},
        "ingestion": {
            "output_dir": str(tmp_path / "out"),
            "output_pattern": "cnefe_{year}_{uf}_standardized.gpkg",
            "inventory_pattern": "cnefe_{year}_{uf}_code_inventory.csv",
            "layer_name": "addresses",
            "fail_on_unknown_codes": fail_unknown,
            "2022": {
                "adapter": "stages/adapters/parse_cnefe_2022.py",
                "input": str(tmp_path / "raw" / "{uf_id}_{uf}.csv"),
                "encoding": "utf-8", "delimiter": ";", "chunk_rows": 3,
                "col_lon": "LONGITUDE", "col_lat": "LATITUDE",
                "col_species": "COD_ESPECIE", "col_subtype": "COD_TIPO_ESPECI",
            },
        },
        "codes": {"2022": {
            "especie": {
                "1": {"label": "particular", "household": True},
                "2": {"label": "coletivo", "household": True},
                "4": {"label": "ensino", "household": False},
            },
            "subtipo": {"101": {"label": "casa"}, "103": {"label": "apto"}, "104": {"label": "outro"}},
        }},
    }


def test_expand_resolves_uf_id(tmp_path):
    cfg = make_cfg(tmp_path)
    assert expand("{uf_id}_{uf}_{year}", cfg, year=2022, uf="DF") == "53_DF_2022"
    with pytest.raises(ConfigError):
        expand("{uf}", cfg, year=2022)


def test_validate_reports_missing_input(tmp_path):
    cfg = make_cfg(tmp_path)
    (tmp_path / "raw" / "53_DF.csv").unlink()
    assert any("not found" in p for p in validate_stage1(cfg, 2022, ["DF"]))


def test_ingest_keeps_every_valid_row_and_contract(tmp_path):
    cfg = make_cfg(tmp_path)
    rep = adapter.ingest(cfg, "DF")

    assert rep["rows_read"] == 8
    assert rep["dropped_invalid_coords"] == 2
    assert rep["rows_written"] == 6          # repeated point is kept
    assert rep["households"] == 3
    assert rep["null_species"] == 1
    assert rep["unknown_species"] == {9: 1}

    gdf = gpd.read_file(Path(rep["output"]), layer="addresses")
    assert list(gdf.columns) == ["is_household", "cod_especie", "cod_subtipo", "geometry"]
    assert gdf.crs.to_epsg() == 4326
    fields = pyogrio.read_info(Path(rep["output"]), layer="addresses")
    assert dict(zip(fields["fields"], fields["dtypes"])) == {          # integers stay integers, even with NULLs
        "is_household": "int16", "cod_especie": "int16", "cod_subtipo": "int16"}
    assert gdf.is_household.tolist() == [1, 1, 1, 0, 0, 0]   # unknown/missing species are not households
    assert gdf.cod_especie.isna().sum() == 1
    assert gdf.cod_subtipo.dropna().astype(int).tolist() == [101, 103, 104]


def test_inventory_flags_unknown_codes(tmp_path):
    cfg = make_cfg(tmp_path)
    adapter.ingest(cfg, "DF")
    inv = pd.read_csv(tmp_path / "out" / "cnefe_2022_DF_code_inventory.csv")
    row = inv[(inv.field == "cod_especie") & (inv.code == 9)].iloc[0]
    assert row["count"] == 1 and row["in_dictionary"] == 0


def test_fail_on_unknown_codes_leaves_no_output(tmp_path):
    cfg = make_cfg(tmp_path, fail_unknown=True)
    with pytest.raises(adapter.UnknownCodeError):
        adapter.ingest(cfg, "DF")
    assert not list((tmp_path / "out").glob("*.gpkg"))


def test_refuses_to_overwrite(tmp_path):
    cfg = make_cfg(tmp_path)
    adapter.ingest(cfg, "DF")
    with pytest.raises(FileExistsError):
        adapter.ingest(cfg, "DF")
    adapter.ingest(cfg, "DF", overwrite=True)


def test_wrong_column_name_is_a_clear_error(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["ingestion"]["2022"]["col_lon"] = "NOPE"
    with pytest.raises(ConfigError, match="NOPE"):
        adapter.ingest(cfg, "DF")


def test_stage1_layer_name_accepts_placeholders(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["ingestion"]["layer_name"] = "addr_{uf_id}"
    rep = adapter.ingest(cfg, "DF")
    assert [str(n) for n, _ in pyogrio.list_layers(Path(rep["output"]))] == ["addr_53"]
