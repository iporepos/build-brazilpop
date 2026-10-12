"""Stage 2 inputs (tracts + microdata) on a tiny synthetic census GeoPackage."""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from pyogrio import write_dataframe
from shapely.geometry import Polygon, box

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stages.census_inputs import load_microdata, load_tracts, match_report  # noqa: E402
from stages.config import ConfigError, validate_stage2  # noqa: E402

BOWTIE = Polygon([(-47.8, -16), (-47.7, -15.9), (-47.7, -16), (-47.8, -15.9)])  # self-intersecting: invalid


def make_cfg(tmp_path: Path, *, parquet: bool = False) -> dict:
    db = tmp_path / "census.gpkg"
    tracts = gpd.GeoDataFrame(
        {"setor_code": ["530000000000001", "530000000000002", "530000000000003", "110000000000001"]},
        geometry=[box(-48, -16, -47.9, -15.9), box(-47.9, -16, -47.8, -15.9), BOWTIE,
                  box(-63, -9, -62.9, -8.9)],
        crs="EPSG:4674",
    )
    write_dataframe(tracts, db, layer="setores", driver="GPKG")
    micro = pd.DataFrame({
        "setor_code": ["530000000000001", "530000000000002", "530000000000099", "110000000000001"],
        "V0001": [100, 0, 50, 70],
        "V0002": [40, 0, 20, 30],
    })
    block = {
        "tract_path": str(db), "tract_layer": "setores", "tract_code_column": "setor_code",
        "microdata_code_column": "setor_code", "population_column": "V0001", "household_column": "V0002",
    }
    if parquet:
        pq = tmp_path / "micro.parquet"
        micro.to_parquet(pq)
        block["microdata_path"] = str(pq)
    else:
        write_dataframe(micro, db, layer="basico", driver="GPKG")
        block.update(microdata_path=str(db), microdata_layer="basico")
    std = tmp_path / "std"
    std.mkdir(exist_ok=True)
    for uf in ("DF", "RO"):
        (std / f"cnefe_2022_{uf}_standardized.gpkg").touch()
    return {
        "ufs": {"DF": 53, "RO": 11},
        "ingestion": {"output_dir": str(std), "output_pattern": "cnefe_{year}_{uf}_standardized.gpkg"},
        "enrichment": {"output_dir": str(tmp_path / "enriched"), "2022": block},
    }


def test_tracts_filtered_by_uf_and_transformed(tmp_path):
    t = load_tracts(make_cfg(tmp_path), 2022, "DF")
    assert sorted(t.tract_code) == ["530000000000001", "530000000000002", "530000000000003"]
    assert t.crs.to_epsg() == 4326
    assert t.attrs["report"]["source_crs"] == "EPSG:4674"
    assert t.geometry.is_valid.all()
    assert t.attrs["report"]["invalid_geometry_repaired"] == 1


def test_microdata_filtered_by_uf(tmp_path):
    m = load_microdata(make_cfg(tmp_path), 2022, "DF")
    assert sorted(m.tract_code) == ["530000000000001", "530000000000002", "530000000000099"]
    assert m.p_t.sum() == 150


def test_microdata_parquet_equals_gpkg_layer(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = load_microdata(make_cfg(tmp_path / "a"), 2022, "DF")
    b = load_microdata(make_cfg(tmp_path / "b", parquet=True), 2022, "DF")
    pd.testing.assert_frame_equal(a.sort_values("tract_code").reset_index(drop=True),
                                  b.sort_values("tract_code").reset_index(drop=True))


def test_match_report_lists_both_directions(tmp_path):
    cfg = make_cfg(tmp_path)
    r = match_report(load_tracts(cfg, 2022, "DF"), load_microdata(cfg, 2022, "DF"))
    assert r["matched"] == 2
    assert r["tracts_without_microdata"] == ["530000000000003"]
    assert r["microdata_without_tract"] == ["530000000000099"]
    assert r["p_t_total_without_tract"] == 50


def test_tract_code_stored_as_several_polygons_is_merged_not_rejected(tmp_path):
    """Islands, lakes and other fragments: one tract, several polygons, one code."""
    cfg = make_cfg(tmp_path)
    db = Path(cfg["enrichment"]["2022"]["tract_path"])
    parts = gpd.GeoDataFrame(
        {"setor_code": ["530000000000001"] * 3 + ["530000000000002"]},
        geometry=[box(0, 0, 1, 1), box(3, 0, 4, 1), box(6, 0, 7, 1), box(0, 5, 1, 6)], crs="EPSG:4674")
    write_dataframe(parts, db, layer="setores", driver="GPKG")

    t = load_tracts(cfg, 2022, "DF")
    assert sorted(t.tract_code) == ["530000000000001", "530000000000002"]      # unique again
    merged = t[t.tract_code == "530000000000001"].geometry.iloc[0]
    assert merged.geom_type == "MultiPolygon" and len(merged.geoms) == 3        # the parts stay separate
    assert merged.area == pytest.approx(3.0, rel=1e-6)                           # nothing lost, nothing doubled
    rep = t.attrs["report"]
    assert (rep["duplicate_code_groups"], rep["duplicate_code_features"]) == (1, 3)
    assert t.attrs["duplicates"].to_dict("records") == [{"tract_code": "530000000000001", "parts": 3}]


def test_codes_without_duplicates_report_zero(tmp_path):
    t = load_tracts(make_cfg(tmp_path), 2022, "DF")
    assert t.attrs["report"]["duplicate_code_groups"] == 0
    assert len(t.attrs["duplicates"]) == 0


def test_a_polygon_without_a_code_cannot_belong_to_a_uf_and_is_ignored(tmp_path):
    """The UF is chosen by the code prefix, so a polygon with no code is never selected."""
    cfg = make_cfg(tmp_path)
    db = Path(cfg["enrichment"]["2022"]["tract_path"])
    bad = gpd.GeoDataFrame({"setor_code": ["530000000000001", None]},
                           geometry=[box(0, 0, 1, 1), box(2, 0, 3, 1)], crs="EPSG:4674")
    write_dataframe(bad, db, layer="setores", driver="GPKG")
    assert load_tracts(cfg, 2022, "DF").tract_code.tolist() == ["530000000000001"]


def test_text_microdata_needs_declared_delimiter_and_matches_other_formats(tmp_path):
    cfg = make_cfg(tmp_path)
    ref = load_microdata(cfg, 2022, "DF")
    csv = tmp_path / "micro.csv"
    csv.write_text("setor_code;V0001;V0002\n"
                   "530000000000001;100;40\n530000000000002;0;0\n530000000000099;50;20\n110000000000001;70;30\n",
                   encoding="utf-8")
    cfg["enrichment"]["2022"].update(microdata_path=str(csv))
    cfg["enrichment"]["2022"].pop("microdata_layer")
    assert any("microdata_delimiter" in p for p in validate_stage2(cfg, 2022, ["DF"]))
    with pytest.raises(ConfigError, match="microdata_delimiter"):
        load_microdata(cfg, 2022, "DF")

    cfg["enrichment"]["2022"]["microdata_delimiter"] = ";"
    assert validate_stage2(cfg, 2022, ["DF"]) == []
    out = load_microdata(cfg, 2022, "DF")
    pd.testing.assert_frame_equal(out.sort_values("tract_code").reset_index(drop=True),
                                  ref.sort_values("tract_code").reset_index(drop=True))


def test_csv_decimal_and_encoding_are_declared(tmp_path):
    cfg = make_cfg(tmp_path)
    csv = tmp_path / "micro.txt"
    # latin-1 accent in a header we do not use, decimal comma in the value we do
    csv.write_bytes("setor_code|Município|V0001|V0002\n530000000000001|x|1234,5|40\n".encode("latin-1"))
    cfg["enrichment"]["2022"].update(microdata_path=str(csv), microdata_delimiter="|",
                                     microdata_encoding="latin-1", microdata_decimal=",")
    cfg["enrichment"]["2022"].pop("microdata_layer")
    out = load_microdata(cfg, 2022, "DF")
    assert out.p_t.tolist() == [1234.5]

    cfg["enrichment"]["2022"]["microdata_encoding"] = "utf-8"
    with pytest.raises(UnicodeDecodeError):
        load_microdata(cfg, 2022, "DF")


def test_tracts_from_a_different_format_than_microdata(tmp_path):
    cfg = make_cfg(tmp_path)
    shp = tmp_path / "tracts.geojson"
    src = gpd.read_file(cfg["enrichment"]["2022"]["tract_path"], layer="setores")
    src.to_file(shp, driver="GeoJSON")
    cfg["enrichment"]["2022"].update(tract_path=str(shp))
    cfg["enrichment"]["2022"].pop("tract_layer")
    t = load_tracts(cfg, 2022, "DF")
    assert len(t) == 3 and t.crs.to_epsg() == 4326


def test_multilayer_file_requires_layer_and_lists_options(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["enrichment"]["2022"].pop("tract_layer")
    with pytest.raises(ConfigError, match="set the layer key") as e:
        load_tracts(cfg, 2022, "DF")
    assert "setores" in str(e.value) and "basico" in str(e.value)


def test_wrong_code_column_lists_available_columns(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["enrichment"]["2022"]["microdata_code_column"] = "NOPE"
    with pytest.raises(ConfigError, match="NOPE") as e:
        load_microdata(cfg, 2022, "DF")
    assert "setor_code" in str(e.value)


def test_validate_stage2_flags_missing_key_and_file(tmp_path):
    cfg = make_cfg(tmp_path)
    assert validate_stage2(cfg, 2022, ["DF"]) == []
    del cfg["enrichment"]["2022"]["microdata_code_column"]
    assert any("microdata_code_column" in p for p in validate_stage2(cfg, 2022, ["DF"]))
    cfg["enrichment"]["2022"]["tract_path"] = str(tmp_path / "nope.gpkg")
    assert any("not found" in p for p in validate_stage2(cfg, 2022, ["DF"]))
