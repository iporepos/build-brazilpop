"""Stage 2 on a tiny synthetic UF that contains every tracked case."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import pytest
from pyogrio import write_dataframe
from shapely.geometry import Point, box

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stages.config import ConfigError  # noqa: E402
from stages.stage_02_vector_enrichment import enrich, run  # noqa: E402

A, B, C, D, E, GHOST = ("530000000000001", "530000000000002", "530000000000003",
                        "530000000000004", "530000000000005", "530000000000099")
F = "530000000000006"      # stored as two disjoint polygons (an island): must be merged


def make_cfg(tmp_path: Path) -> dict:
    # Tracts A..E side by side (A and B share the edge x = 1); census CRS is 4674.
    tracts = gpd.GeoDataFrame(
        {"setor_code": [B, A, C, D, E, F, F]},   # deliberately unsorted: the stage must sort by code
        geometry=[box(1, 0, 2, 1), box(0, 0, 1, 1), box(3, 0, 4, 1), box(5, 0, 6, 1), box(7, 0, 8, 1),
                  box(11, 0, 12, 1), box(13, 0, 14, 1)],
        crs="EPSG:4674",
    )
    micro = pd.DataFrame({
        "setor_code": [A, B, C, E, GHOST, F],         # D has no microdata row, GHOST has no tract
        "V0001": [100, 60, 40, 0, 50, 90],
        "V0002": [4, 2, 3, 0, 1, 3],
    })
    census = tmp_path / "census.gpkg"
    write_dataframe(tracts, census, layer="setores", driver="GPKG")
    write_dataframe(micro, census, layer="basico", driver="GPKG", append=True)

    rows = (
        [(0.2, 0.2, 1, 1, 101)] * 3 + [(0.4, 0.4, 6, None, None)]   # A: 3 households + 1 other
        + [(1.5, 0.5, 1, 1, 103)] * 2                              # B: 2 households
        + [(1.0, 0.5, 1, 1, 101)]                                  # on the A/B edge: goes to A
        + [(3.5, 0.5, 4, None, None)]                              # C: only a non-household
        + [(5.5, 0.5, 1, 1, 101)]                                  # D: household, no microdata
        + [(10.0, 10.0, 1, 1, 101)]                                # outside every tract
        + [(11.5, 0.5, 1, 1, 101)] * 2 + [(13.5, 0.5, 1, 1, 103), (13.6, 0.6, 4, None, None)]   # F: both parts
    )
    std = pd.DataFrame(rows, columns=["lon", "lat", "esp", "hh", "sub"])
    pts = gpd.GeoDataFrame(
        {"is_household": (std.esp.isin([1, 2])).astype("int16"),
         "cod_especie": std.esp.astype("Int16"), "cod_subtipo": std["sub"].astype("Int16")},
        geometry=[Point(x, y) for x, y in zip(std.lon, std.lat)], crs="EPSG:4326",
    )
    std_dir = tmp_path / "std"
    std_dir.mkdir()
    write_dataframe(pts, std_dir / "cnefe_2022_DF_standardized.gpkg", layer="addresses", driver="GPKG")

    return {
        "ufs": {"DF": 53},
        "execution": {"overwrite": False},
        "ingestion": {"output_dir": str(std_dir), "output_pattern": "cnefe_{year}_{uf}_standardized.gpkg",
                      "layer_name": "addresses"},
        "enrichment": {
            "output_dir": str(tmp_path / "enriched"),
            "output_pattern": "cnefe_{year}_{uf}_enriched.gpkg",
            "balance_pattern": "cnefe_{year}_{uf}_balance.gpkg",
            "report_pattern": "cnefe_{year}_{uf}_stage2_report.json",
            "chunk_rows": 4,    # forces several chunks
            "2022": {
                "tract_path": str(census), "tract_layer": "setores", "tract_code_column": "setor_code",
                "microdata_path": str(census), "microdata_layer": "basico",
                "microdata_code_column": "setor_code",
                "population_column": "V0001", "household_column": "V0002",
            },
        },
    }


@pytest.fixture()
def result(tmp_path):
    cfg = make_cfg(tmp_path)
    rep = enrich(cfg, 2022, "DF")
    out = Path(cfg["enrichment"]["output_dir"])
    enriched = pyogrio.read_dataframe(out / "cnefe_2022_DF_enriched.gpkg", layer="addresses")
    balance = pyogrio.read_dataframe(out / "cnefe_2022_DF_balance.gpkg", layer="tract_balance",
                                     read_geometry=False).set_index("tract_code")
    return cfg, rep, enriched, balance, out


def test_points_are_assigned_spatially_and_boundary_goes_to_lowest_code(result):
    _, rep, e, _, _ = result
    a = e[e.cd_setor == A]
    assert len(a) == 5                      # 3 + 1 non-household + the point on the A/B edge
    assert (e.cd_setor == B).sum() == 2
    assert rep["boundary_points"] == 1


def test_delta_is_pt_over_physical_household_points(result):
    _, _, e, bal, _ = result
    assert bal.loc[A, "h_ti"] == 4 and bal.loc[A, "delta_t"] == 25.0   # P 100 / 4 points, nominal H was 4
    assert bal.loc[B, "h_ti"] == 2 and bal.loc[B, "delta_t"] == 30.0
    a = e[e.cd_setor == A]
    assert (a[a.is_household == 1].p_i == 25.0).all()              # household points carry the tract density δ_T
    assert (a[a.is_household == 0].p_i == 0.0).all()               # non-household points carry 0
    hh = e[e.is_household == 1]
    assert hh.groupby("cd_setor").p_i.sum().loc[[A, B]].tolist() == [100.0, 60.0]


def test_tract_with_population_and_no_household_points_gets_zero_and_is_tracked(result):
    _, rep, e, bal, _ = result
    assert bal.loc[C, "status"] == "no_household_points"
    assert bal.loc[C, "unallocated_p"] == 40
    assert (e[e.cd_setor == C].p_i == 0.0).all()       # its only point is a non-household: weight 0
    assert (e[e.cd_setor == C].is_household == 0).all()
    assert rep["p_unallocated_no_household_points"] == 40


def test_tract_without_microdata_and_empty_tract(result):
    _, _, e, bal, _ = result
    assert bal.loc[D, "status"] == "no_microdata"
    d = e[e.cd_setor == D]
    assert (d.is_household == 1).all() and d.p_i.isna().all()     # household with an undefined density: NULL
    assert bal.loc[E, "status"] == "empty"


def test_non_households_carry_zero_and_households_are_null_only_when_undefined(result):
    _, _, e, bal, _ = result
    assert (e[e.is_household == 0].p_i == 0.0).all()
    undefined = e[(e.is_household == 1) & e.p_i.isna()]
    assert set(undefined.cd_setor.fillna("orphan")) == {D, "orphan"}   # no microdata, or outside every tract


def test_orphan_point_has_no_tract_and_no_population(result):
    cfg, rep, e, _, out = result
    o = e[e.orphan == 1]
    assert len(o) == 1 and o.cd_setor.isna().all() and o.p_i.isna().all() and o.h_ti.isna().all()
    assert rep["orphan_points"] == 1 and rep["orphan_households"] == 1
    assert len(pyogrio.read_dataframe(out / "cnefe_2022_DF_balance.gpkg", layer="orphan_points")) == 1


def test_split_tract_is_one_tract_and_listed_in_the_sidecar(result):
    _, rep, e, bal, out = result
    f = e[e.cd_setor == F]
    assert len(f) == 4                                          # points of BOTH parts carry the same code
    assert bal.loc[F, "h_ti"] == 3 and bal.loc[F, "delta_t"] == 30.0 and bal.loc[F, "status"] == "ok"
    assert f[f.is_household == 1].p_i.tolist() == [30.0, 30.0, 30.0]
    assert f[f.is_household == 0].p_i.tolist() == [0.0]
    assert len(bal) == 6                                        # A, B, C, D, E, F: one row per code
    dup = pyogrio.read_dataframe(out / "cnefe_2022_DF_balance.gpkg", layer="duplicate_tracts", read_geometry=False)
    assert dup.to_dict("records") == [{"tract_code": F, "parts": 2}]
    assert (rep["duplicate_tract_codes_merged"], rep["duplicate_tract_features_merged"]) == (1, 2)


def test_microdata_without_tract_is_tracked(result):
    _, rep, _, _, out = result
    um = pyogrio.read_dataframe(out / "cnefe_2022_DF_balance.gpkg", layer="microdata_unmatched",
                                read_geometry=False)
    assert um.tract_code.tolist() == [GHOST]
    assert rep["p_unallocated_no_tract"] == 50


def test_mass_is_conserved_and_every_point_is_written(result):
    cfg, rep, e, _, out = result
    assert len(e) == rep["points"] == 14
    assert rep["p_total"] == 340
    assert rep["p_allocated"] == 250
    assert rep["p_allocated"] + rep["p_unallocated_no_household_points"] + rep["p_unallocated_no_tract"] == 340
    assert rep["conservation_gap"] == 0 and rep["conserved"] is True
    saved = json.loads((out / "cnefe_2022_DF_stage2_report.json").read_text())
    assert saved["p_unallocated_share"] == pytest.approx(90 / 340)


def test_enriched_keeps_stage1_fields_and_geometry(result):
    _, _, e, _, _ = result
    assert list(e.columns) == ["is_household", "cod_especie", "cod_subtipo", "cd_setor", "h_ti",
                               "p_i", "orphan", "geometry"]
    assert e.crs.to_epsg() == 4326


def test_integer_fields_are_stored_as_integers(result):
    _, rep, _, _, _ = result
    info = pyogrio.read_info(rep["outputs"]["enriched"], layer="addresses")
    assert dict(zip(info["fields"], info["dtypes"])) == {
        "is_household": "int16", "cod_especie": "int16", "cod_subtipo": "int16",
        "cd_setor": "object", "h_ti": "int32", "p_i": "float64", "orphan": "int16"}


def test_refuses_overwrite_and_missing_stage1(tmp_path):
    cfg = make_cfg(tmp_path)
    enrich(cfg, 2022, "DF")
    with pytest.raises(FileExistsError):
        enrich(cfg, 2022, "DF")
    enrich(cfg, 2022, "DF", overwrite=True)
    (Path(cfg["ingestion"]["output_dir"]) / "cnefe_2022_DF_standardized.gpkg").unlink()
    with pytest.raises(ConfigError, match="Stage 1 output"):
        run(cfg, 2022, ["DF"], overwrite=True)


def test_layer_name_is_configurable_and_accepts_placeholders(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["enrichment"]["layer_name"] = "enriched_{uf_id}_{uf}_{year}"
    rep = enrich(cfg, 2022, "DF")
    assert rep["enriched_layer"] == "enriched_53_DF_2022"
    layers = [str(n) for n, _ in pyogrio.list_layers(rep["outputs"]["enriched"])]
    assert layers == ["enriched_53_DF_2022"]
    assert len(pyogrio.read_dataframe(rep["outputs"]["enriched"], layer="enriched_53_DF_2022")) == 14


def test_stage1_layer_name_is_what_stage2_reads(tmp_path):
    cfg = make_cfg(tmp_path)
    src = Path(cfg["ingestion"]["output_dir"]) / "cnefe_2022_DF_standardized.gpkg"
    pts = pyogrio.read_dataframe(src, layer="addresses")
    src.unlink()
    write_dataframe(pts, src, layer="stage1_layer_53", driver="GPKG")
    cfg["ingestion"]["layer_name"] = "stage1_layer_{uf_id}"
    cfg["enrichment"]["layer_name"] = "stage2_layer"
    rep = enrich(cfg, 2022, "DF")
    assert rep["points"] == 14 and rep["enriched_layer"] == "stage2_layer"
