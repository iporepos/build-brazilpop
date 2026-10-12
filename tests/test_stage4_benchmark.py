"""Stage 4 on a small synthetic country with hand-computable answers."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import pytest
from pyogrio import write_dataframe
from shapely.geometry import box

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from stages.config import ConfigError, load_config  # noqa: E402
from stages.stage_03_additive_rasterize import build_catalog, out_dir as s3_out_dir, raster_name  # noqa: E402
from stages.stage_04_benchmark import benchmark, unit_metrics, validate  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("gdal_rasterize") is None, reason="GDAL command-line tools not installed")

EXAMPLE = REPO_ROOT / "config.example.toml"
T1, T2, T3, T4 = "530010805000001", "530010805000002", "530010806000001", "530010806000002"
D1, D2, M = "530010805", "530010806", "5300108"
P_T = {T1: 100.0, T2: 200.0, T3: 300.0, T4: 400.0}

# fine raster: 0.5 degree cells, x 10..14 (8 cols), y 50..54 (8 rows), values 1..64 row by row
FINE = (np.arange(64, dtype="float64").reshape(8, 8) + 1)
# coarse raster: 1 degree cells, x 10..14 (4 cols), y 50..54 (4 rows)
COARSE = (np.arange(16, dtype="float64").reshape(4, 4) + 1) * 10
EXT = np.full((4, 4), 50.0)


def write_raster(path: Path, arr: np.ndarray, cell: float, nodata=-9999) -> None:
    rows, cols = arr.shape
    path.parent.mkdir(parents=True, exist_ok=True)
    asc = path.with_suffix(".asc")
    asc.write_text(f"ncols {cols}\nnrows {rows}\nxllcorner 10\nyllcorner 50\ncellsize {cell}\nNODATA_value {nodata}\n"
                   + "\n".join(" ".join(str(v) for v in r) for r in arr) + "\n")
    subprocess.run(["gdal_translate", "-q", "-of", "GTiff", "-ot", "Float32", "-a_srs", "EPSG:4326",
                    str(asc), str(path)], check=True)


def block_sum(arr: np.ndarray, rows: slice, cols: slice) -> float:
    return float(arr[rows, cols].sum())


def make_cfg(tmp_path: Path, *, split_tract: bool = False) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "census.gpkg"
    feats = [(T1, box(10, 52, 12, 54)), (T2, box(12, 52, 14, 54)), (T3, box(10, 50, 12, 52)), (T4, box(12, 50, 14, 52))]
    if split_tract:     # T2 stored as two polygons (an island): must still be one tract
        feats[1] = (T2, box(12, 52, 13, 54))
        feats.append((T2, box(13, 52, 14, 54)))
    tracts = gpd.GeoDataFrame({"setor_code": [c for c, _ in feats]}, geometry=[g for _, g in feats], crs="EPSG:4674")
    write_dataframe(tracts, db, layer="setores", driver="GPKG")
    dist = gpd.GeoDataFrame({"distrito_code": [D1, D2], "distrito_name": ["North", "South"]},
                            geometry=[box(10, 52, 14, 54), box(10, 50, 14, 52)], crs="EPSG:4674")
    write_dataframe(dist, db, layer="distritos", driver="GPKG", append=True)
    mun = gpd.GeoDataFrame({"municipio_code": [M], "municipio_name": ["Capital"]},
                           geometry=[box(10, 50, 14, 54)], crs="EPSG:4674")
    write_dataframe(mun, db, layer="municipios", driver="GPKG", append=True)
    micro = pd.DataFrame({"setor_code": list(P_T), "V0001": list(P_T.values()), "V0002": [40, 80, 120, 160]})
    write_dataframe(micro, db, layer="basico", driver="GPKG", append=True)

    cfg = load_config(EXAMPLE)
    cfg["execution"]["overwrite"] = True
    cfg["enrichment"]["2022"] = {
        "tract_path": str(db), "tract_layer": "setores", "tract_code_column": "setor_code",
        "microdata_path": str(db), "microdata_layer": "basico", "microdata_code_column": "setor_code",
        "population_column": "V0001", "household_column": "V0002"}
    cfg["rasterization"]["output_dir"] = str(tmp_path / "rasters")
    cfg["benchmarking"].update(output_dir=str(tmp_path / "out"), workers=1, window_cells=16)
    cfg["benchmarking"]["2022"] = {
        "district_path": str(db), "district_layer": "distritos", "district_code_column": "distrito_code",
        "district_name_column": "distrito_name",
        "municipality_path": str(db), "municipality_layer": "municipios", "municipality_code_column": "municipio_code",
        "municipality_name_column": "municipio_name"}

    pop = next(lay for lay in build_catalog(cfg, 2022) if lay.cls == "population")
    for res, arr, cell in (("030", FINE, 0.5), ("01C", COARSE, 1.0)):
        write_raster(s3_out_dir(cfg, res, 2022) / raster_name(cfg, pop, res, 2022), arr, cell)
    write_raster(tmp_path / "ext_2022.tif", EXT, 1.0)
    cfg["benchmarking"]["products"] = {
        "brazilpop": {"stage3_resolution": "01C", "min_scale": "tract", "resolution_by_scale": {"tract": "030"}},
        "ext": {"raster": str(tmp_path / "ext_{year}.tif"), "min_scale": "municipality", "years": [2022]},
    }
    return cfg


@pytest.fixture()
def result(tmp_path):
    cfg = make_cfg(tmp_path)
    return cfg, benchmark(cfg, 2022, ["DF"])


def test_tracts_are_sampled_from_the_30m_raster(result):
    _, rep = result
    t = rep["_tables"]["tract"].set_index("code")
    # tract T1: x 10..12 -> cols 0..3, y 52..54 -> rows 0..3 (rows count from the top, y=54)
    assert t.loc[T1, "pi_sum_brazilpop"] == block_sum(FINE, slice(0, 4), slice(0, 4))
    assert t.loc[T4, "pi_sum_brazilpop"] == block_sum(FINE, slice(4, 8), slice(4, 8))
    assert t.loc[T1, "pi_count_brazilpop"] == 16
    assert t.loc[T1, "err_brazilpop"] == t.loc[T1, "pi_sum_brazilpop"] - 100.0
    assert t.loc[T1, "pct_err_brazilpop"] == pytest.approx(100 * t.loc[T1, "err_brazilpop"] / 100.0)


def test_districts_and_municipality_use_the_100m_raster_and_state_is_their_sum(result):
    _, rep = result
    d = rep["_tables"]["district"].set_index("code")
    assert d.loc[D1, "pi_sum_brazilpop"] == block_sum(COARSE, slice(0, 2), slice(0, 4))     # y 52..54
    assert d.loc[D2, "pi_sum_brazilpop"] == block_sum(COARSE, slice(2, 4), slice(0, 4))
    assert d.loc[D1, "p_k"] == 300.0 and d.loc[D2, "p_k"] == 700.0                           # P_K = sum of tracts
    assert d.loc[D1, "name"] == "North"
    m = rep["_tables"]["municipality"].set_index("code")
    assert m.loc[M, "pi_sum_brazilpop"] == COARSE.sum() and m.loc[M, "p_k"] == 1000.0
    s = rep["_tables"]["state"].set_index("code")
    assert s.loc["53", "pi_sum_brazilpop"] == m.loc[M, "pi_sum_brazilpop"]
    assert s.loc["53", "name"] == "DF" and s.loc["53", "p_k"] == 1000.0


def test_external_product_is_sampled_at_every_scale_but_scored_from_min_scale(result):
    _, rep = result
    assert rep["_tables"]["tract"].set_index("code").loc[T1, "pi_sum_ext"] == 50.0 * 4      # 4 coarse cells
    met = rep["_metrics"]
    ext = met[met["product"] == "ext"]
    assert sorted(ext["scale"]) == ["municipality", "state"]                                   # min_scale
    assert set(met[met["product"] == "brazilpop"]["scale"]) == {"tract", "district", "municipality", "state"}


def test_report_carries_the_product_labels(result, tmp_path):
    cfg, rep = result
    saved = json.loads((tmp_path / "out" / "benchmark_2022_report.json").read_text())
    assert saved["labels"] == {"brazilpop": "brazilpop", "ext": "ext"}      # no label set: the key
    cfg["benchmarking"]["products"]["ext"]["label"] = "External (2020)"
    rep2 = benchmark(cfg, 2022, ["DF"])
    saved = json.loads((tmp_path / "out" / "benchmark_2022_report.json").read_text())
    assert saved["labels"]["ext"] == "External (2020)"


def test_metrics_report_the_resolution_each_scale_used(result):
    _, rep = result
    m = rep["_metrics"].set_index(["product", "scale"])
    assert m.loc[("brazilpop", "tract"), "resolution"] == "030"
    assert m.loc[("brazilpop", "district"), "resolution"] == "01C"
    assert m.loc[("brazilpop", "state"), "resolution"] == "01C"
    assert m.loc[("ext", "municipality"), "resolution"] == "external"


def test_metric_values_are_computed_from_the_unit_errors(result):
    _, rep = result
    t = rep["_tables"]["tract"]
    err = t["pi_sum_brazilpop"] - t["p_k"]
    m = rep["_metrics"].set_index(["product", "scale"]).loc[("brazilpop", "tract")]
    assert m["units"] == 4
    assert m["bias"] == pytest.approx(err.mean())
    assert m["mae"] == pytest.approx(err.abs().mean())
    assert m["rmse"] == pytest.approx(np.sqrt((err ** 2).mean()))
    assert m["mape"] == pytest.approx((err.abs() / t["p_k"]).mean() * 100)
    assert m["mass_gap"] == pytest.approx(t["pi_sum_brazilpop"].sum() - 1000.0)


def test_output_files_and_master_layer(result, tmp_path):
    cfg, rep = result
    out = tmp_path / "out"
    for name in ("benchmark_2022_tract.csv", "benchmark_2022_district.csv", "benchmark_2022_municipality.csv",
                 "benchmark_2022_state.csv", "benchmark_2022_metrics.csv", "benchmark_2022_report.json",
                 "benchmark_2022_tracts.gpkg"):
        assert (out / name).is_file(), name
    g = pyogrio.read_dataframe(out / "benchmark_2022_tracts.gpkg", layer="tracts")
    assert len(g) == 4 and g.crs.to_epsg() == 4326
    assert {"pi_sum_brazilpop", "pi_sum_ext", "err_brazilpop", "district_code", "municipality_code", "uf_code"} <= set(g.columns)
    assert set(g["district_code"]) == {D1, D2} and set(g["uf_code"]) == {"53"}
    saved = json.loads((out / "benchmark_2022_report.json").read_text())
    assert saved["census_total"] == 1000.0 and saved["polygons"] == {"tract": 4, "district": 2, "municipality": 1}
    assert saved["census_units_without_polygon"] == {"tract": 0, "district": 0, "municipality": 0}


def test_a_tract_stored_as_two_polygons_is_one_row_with_the_same_sum(tmp_path):
    base = benchmark(make_cfg(tmp_path / "a"), 2022, ["DF"])["_tables"]["tract"].set_index("code")
    split = benchmark(make_cfg(tmp_path / "b", split_tract=True), 2022, ["DF"])["_tables"]["tract"].set_index("code")
    assert len(split) == 4
    assert split.loc[T2, "pi_sum_brazilpop"] == base.loc[T2, "pi_sum_brazilpop"]


def test_unit_metrics_ignore_units_without_census_total_and_zero_population_in_mape():
    t = pd.DataFrame({"code": list("abcd"), "p_k": [100.0, 0.0, np.nan, 50.0],
                      "pi_sum_x": [110.0, 5.0, 40.0, 40.0], "pi_count_x": [3, 1, 2, 0]})
    m = unit_metrics(t, "x")
    assert m["units"] == 3 and m["units_with_population"] == 2          # NaN truth is out; zero is in
    assert m["bias"] == pytest.approx((10 + 5 - 10) / 3)
    assert m["mape"] == pytest.approx((10 / 100 + 10 / 50) / 2 * 100)   # the zero-population unit has no percentage
    assert m["units_without_cells"] == 1                                 # d: population but no cell centre
    assert m["mass_gap"] == pytest.approx(155 - 150)


def test_refuses_to_overwrite_and_reports_missing_rasters(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["execution"]["overwrite"] = False
    benchmark(cfg, 2022, ["DF"], overwrite=True)
    with pytest.raises(FileExistsError):
        benchmark(cfg, 2022, ["DF"])

    cfg["benchmarking"]["products"]["ext"]["raster"] = str(tmp_path / "nope_{year}.tif")
    problems = validate(cfg, 2022, ["DF"])
    assert any("raster of 'ext'" in p and "not found" in p for p in problems)
    with pytest.raises(ConfigError, match="Config validation failed"):
        benchmark(cfg, 2022, ["DF"], overwrite=True)


def test_state_needs_municipality_and_products_respect_years(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["benchmarking"]["scales"] = ["tract", "state"]
    assert any("add 'municipality'" in p for p in validate(cfg, 2022, ["DF"]))
    cfg["benchmarking"]["scales"] = ["tract", "district", "municipality", "state"]
    assert validate(cfg, 2022, ["DF"]) == []
    cfg["benchmarking"]["products"]["ext"]["years"] = [2010]           # not for 2022: simply not used
    assert validate(cfg, 2022, ["DF"]) == []


def test_census_units_without_a_polygon_are_listed(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Path(cfg["enrichment"]["2022"]["tract_path"])
    # a census row (tract T1) whose district polygon is absent from the districts layer
    dist = gpd.GeoDataFrame({"distrito_code": [D2], "distrito_name": ["South"]}, geometry=[box(10, 50, 14, 52)], crs="EPSG:4674")
    write_dataframe(dist, db, layer="distritos", driver="GPKG")
    rep = benchmark(cfg, 2022, ["DF"])
    assert rep["census_units_without_polygon"]["district"] == 1
    saved = json.loads((tmp_path / "out" / "benchmark_2022_report.json").read_text())
    assert saved["census_units_without_polygon_codes"]["district"] == [D1]


def test_charts_are_drawn_after_the_tables_and_never_break_the_stage(monkeypatch, tmp_path):
    from stages import stage_04_benchmark as s4
    cfg = {"benchmarking": {"plots": False}}
    assert s4.run_plots(cfg, "x.toml", 2022) == []                      # switched off

    import tools.plot_benchmark as pb
    calls = []
    monkeypatch.setattr(pb, "build", lambda config, year, out, mode, dpi, **kw: calls.append(kw) or [tmp_path / "a.png"])
    cfg = {"benchmarking": {"plot_kinds": ["scatter", "hexbin"], "plot_scales": ["tract", "district"]}}
    assert s4.run_plots(cfg, "x.toml", 2022) == [tmp_path / "a.png"]    # one kind each, the bars once
    assert [c["kind"] for c in calls] == ["scatter", "hexbin"] and calls[0]["scales"] == ("tract", "district")

    def boom(*a, **k):
        raise RuntimeError("no display")
    monkeypatch.setattr(pb, "build", boom)
    assert s4.run_plots(cfg, "x.toml", 2022) == []                      # a failure is reported, not raised
