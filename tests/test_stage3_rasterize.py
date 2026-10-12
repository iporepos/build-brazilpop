"""Stage 3 on a tiny synthetic UF, using the real GDAL command-line tools."""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from pyogrio import write_dataframe
from shapely.geometry import Point

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stages.config import ConfigError  # noqa: E402
from stages.stage_03_additive_rasterize import build_catalog, run, validate  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("gdal_rasterize") is None, reason="GDAL command-line tools not installed")

# 4 x 3 cells of 1 degree, origin (10, 53): cell (col, row) covers lon 10+col..11+col, lat 53-row..52-row
ORIGIN_X, ORIGIN_Y = 10.0, 53.0


def cell_centre(col: int, row: int) -> tuple[float, float]:
    return ORIGIN_X + col + 0.5, ORIGIN_Y - row - 0.5


def read_cells(path: Path) -> dict[tuple[int, int], float]:
    """Raster values by (col, row), through gdal_translate -of XYZ (no Python GDAL needed)."""
    out = subprocess.run(["gdal_translate", "-q", "-of", "XYZ", str(path), "/vsistdout/"],
                         capture_output=True, text=True, check=True).stdout
    cells = {}
    for line in out.splitlines():
        x, y, v = line.split()
        cells[(int(float(x) - ORIGIN_X), int(ORIGIN_Y - float(y)))] = float(v)
    return cells


def make_cfg(tmp_path: Path, *, nodata: int = 65535, epsg: int = 4326, template_type: str = "UInt16",
             template_nodata: int | None = 65535, template_bands: int = 1, extra_points: int = 0,
             layer: str = "addresses") -> dict:
    tpl = tmp_path / "tpl.tif"
    cmd = ["gdal_create", "-q", "-outsize", "4", "3", "-ot", template_type, "-burn", "0", "-bands", str(template_bands),
           "-a_srs", f"EPSG:{epsg}", "-a_ullr", f"{ORIGIN_X}", f"{ORIGIN_Y}", f"{ORIGIN_X + 4}", f"{ORIGIN_Y - 3}"]
    if template_nodata is not None:
        cmd += ["-a_nodata", str(template_nodata)]
    subprocess.run(cmd + [str(tpl)], check=True)

    c00, c10, c20 = cell_centre(0, 0), cell_centre(1, 0), cell_centre(2, 1)
    rows = [  # x, y, household, species, subtype, p_i
        (*c00, 1, 1, 101, 10.0), (*c00, 1, 1, 103, 20.0), (*c00, 0, 4, None, None),   # cell (0,0)
        (*c10, 1, 1, 101, None),                                                      # orphan household
        (*c20, 1, 2, 104, 5.0), (*c20, 1, 2, 104, 5.0), (*c20, 1, 2, 104, 5.0),        # cell (2,1): 3 points
        (50.0, 50.0, 1, 1, 101, 1.0),                                                 # outside the template
    ]
    rows += [(*cell_centre(3, 2), 1, 1, 101, 0.1)] * extra_points                     # cell (3,2): a big count
    df = pd.DataFrame(rows, columns=["x", "y", "is_household", "cod_especie", "cod_subtipo", "p_i"])
    pts = gpd.GeoDataFrame(
        {"is_household": df.is_household.astype("int16"), "cod_especie": df.cod_especie.astype("Int16"),
         "cod_subtipo": df.cod_subtipo.astype("Int16"), "p_i": df.p_i.astype("float64")},
        geometry=[Point(x, y) for x, y in zip(df.x, df.y)], crs="EPSG:4326")
    enriched = tmp_path / "enriched"
    enriched.mkdir()
    write_dataframe(pts, enriched / "cnefe_2022_DF_enriched.gpkg", layer=layer, driver="GPKG")
    (enriched / "cnefe_2022_DF_stage2_report.json").write_text(
        json.dumps({"points": 8 + extra_points, "households": 7 + extra_points,
                                                        "p_allocated": 46.0 + 0.1 * extra_points}))

    styles = tmp_path / "styles"
    styles.mkdir()
    for n in ("address_030.qml", "population_030.qml"):
        (styles / n).write_text("<qgis/>")

    core = [
        {"variable": "CNEFE", "variant": "ADDRESS", "spec": "00", "class": "count"},
        {"variable": "CNEFE", "variant": "HOUSEHOLD", "spec": "000", "class": "count", "flag": "is_household"},
        {"variable": "POPULATION", "variant": "RESIDENT", "class": "population", "source": "p_i"},
    ]
    y2022 = [
        {"variable": "CNEFE", "variant": "ADDRESS", "spec": "01", "class": "count", "species": [1]},
        {"variable": "CNEFE", "variant": "ADDRESS", "spec": "04", "class": "count", "species": [4]},
        {"variable": "CNEFE", "variant": "HOUSEHOLD", "spec": "101", "class": "count",
         "flag": "is_household", "subtype": [101]},
    ]
    return {
        "ufs": {"DF": 53},
        "project": {"provider": "BRAZILPOP", "version": "6", "extension": "TEST"},
        "data": {"styles": str(styles)},
        "execution": {"overwrite": False},
        "enrichment": {"output_dir": str(enriched), "output_pattern": "cnefe_{year}_{uf}_enriched.gpkg",
                       "layer_name": layer,
                       "report_pattern": "cnefe_{year}_{uf}_stage2_report.json"},
        "resolutions": {"030": {"nominal": "30m", "template": str(tpl)}},
        "codes": {"2022": {
            "especie": {str(i): {"label": str(i)} for i in range(1, 9)},
            "subtipo": {str(i): {"label": str(i)} for i in (101, 102, 103, 104)}}},
        "rasterization": {
            "output_dir": str(tmp_path / "rasters"),
            "resolutions": ["030"], "workers": 2,
            "name_pattern_spec": "{provider}_V{version}_{variable}_{variant}-{spec}_{resolution}_{extension}_{year}.tif",
            "name_pattern_nospec": "{provider}_V{version}_{variable}_{variant}_{resolution}_{extension}_{year}.tif",
            "encoding": {"counts": {"dtype": "uint16", "nodata": nodata, "fill_value": 0, "warn_on_saturation": True},
                         "population": {"dtype": "float32", "nodata": -9999.0, "fill_value": 0.0}},
            "geotiff_profile": {"blocksize": 256, "compress": "ZSTD", "predictor_integer": 2,
                                "predictor_float": 3, "build_overviews": False},
            "catalog": {"core": {"layers": core}, "2022": {"layers": y2022}},
            "manifest": {"write": True, "pattern": "{output_dir}/manifest_{year}.csv",
                         "report_pattern": "{output_dir}/stage3_report_{year}.json"},
            "checks": {"population_relative_tolerance": 1e-6},
        },
    }


def raster(cfg: dict, name: str) -> Path:
    return Path(cfg["rasterization"]["output_dir"]) / "030" / f"BRAZILPOP_V6_{name}_030_TEST_2022.tif"


@pytest.fixture()
def built(tmp_path):
    cfg = make_cfg(tmp_path)
    return cfg, run(cfg, 2022, ["DF"])


def test_counts_land_in_the_cell_that_contains_the_point(built):
    cfg, _ = built
    a00 = read_cells(raster(cfg, "CNEFE_ADDRESS-00"))
    assert a00[(0, 0)] == 3 and a00[(1, 0)] == 1 and a00[(2, 1)] == 3
    assert sum(a00.values()) == 7                      # the point outside the template is not burned
    assert read_cells(raster(cfg, "CNEFE_HOUSEHOLD-000"))[(0, 0)] == 2
    assert read_cells(raster(cfg, "CNEFE_ADDRESS-04"))[(0, 0)] == 1
    assert read_cells(raster(cfg, "CNEFE_HOUSEHOLD-101"))[(0, 0)] == 1


def test_population_burns_delta_of_household_points_only(built):
    cfg, _ = built
    pop = read_cells(raster(cfg, "POPULATION_RESIDENT"))
    assert pop[(0, 0)] == pytest.approx(30.0)          # 10 + 20; the non-household adds nothing
    assert pop[(1, 0)] == 0.0                          # orphan household: delta is NULL
    assert pop[(2, 1)] == pytest.approx(15.0)


def test_output_encoding_and_sidecar(built):
    cfg, _ = built
    f = raster(cfg, "CNEFE_ADDRESS-00")
    info = json.loads(subprocess.run(["gdalinfo", "-json", str(f)], capture_output=True, text=True).stdout)
    band = info["bands"][0]
    assert band["type"] == "UInt16" and band["noDataValue"] == 65535
    assert info["size"] == [4, 3]                        # a copy of the template: its own layout
    assert not f.with_name(f.stem + ".part.tif").exists()
    pinfo = json.loads(subprocess.run(["gdalinfo", "-json", str(raster(cfg, "POPULATION_RESIDENT"))],
                                      capture_output=True, text=True).stdout)
    assert pinfo["bands"][0]["type"] == "Float32" and pinfo["bands"][0]["noDataValue"] == -9999
    assert f.with_suffix(".qml").read_text() == "<qgis/>"


def test_checks_flag_points_outside_the_template(built):
    _, rep = built
    by = {c["check"]: c for c in rep["checks"]}
    assert by["ADDRESS-00 total == Stage 2 points"]["ok"] is False          # 7 burned of 8 points
    assert by["ADDRESS-00 total == Stage 2 points"]["difference"] == -1
    assert by["HOUSEHOLD-000 total == Stage 2 households"]["ok"] is False   # 6 of 7
    assert by["sum(HOUSEHOLD-1xx) == HOUSEHOLD-000"]["ok"] is False        # only subtype 101 is in this catalog
    pop = by["POPULATION total == Stage 2 allocated P_T"]
    assert pop["actual"] == pytest.approx(45.0) and pop["expected"] == 46.0 and pop["ok"] is False
    assert rep["all_checks_ok"] is False


def test_manifest_lists_every_layer(built):
    cfg, rep = built
    with open(Path(cfg["rasterization"]["output_dir"]) / "manifest_2022.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert {r["layer"] for r in rows} == {"CNEFE_ADDRESS-00", "CNEFE_HOUSEHOLD-000", "POPULATION_RESIDENT",
                                          "CNEFE_ADDRESS-01", "CNEFE_ADDRESS-04", "CNEFE_HOUSEHOLD-101"}
    assert all(r["status"] == "produced" for r in rows)
    assert rep["rasters"] == 6


def test_saturated_counts_stop_at_the_type_maximum_and_are_reported(tmp_path):
    cfg = make_cfg(tmp_path, extra_points=70000)        # 70,000 points in one UInt16 cell
    rep = run(cfg, 2022, ["DF"])
    assert read_cells(raster(cfg, "CNEFE_ADDRESS-00"))[(3, 2)] == 65535       # stops, never wraps to 4464
    layer = next(r for r in rep["layers"] if r["layer"] == "CNEFE_ADDRESS-00")
    assert layer["saturated"] and layer["maximum"] == 65535


def test_population_burn_reads_only_points_with_weight():
    from stages.stage_03_additive_rasterize import Layer
    pop = Layer("POPULATION", "RESIDENT", None, "population", source="p_i")
    assert pop.where() == "p_i > 0"
    assert Layer("CNEFE", "HOUSEHOLD", "000", "count", flag="is_household").where() == "is_household = 1"
    assert Layer("CNEFE", "ADDRESS", "00", "count").where() is None


def test_population_source_must_be_the_p_i_column(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["enrichment"]["out_p_i"] = "weight"
    assert any("out_p_i" in p for p in validate(cfg, 2022, ["DF"]))


@pytest.mark.parametrize("template_type", ["Byte", "Int16", "UInt32", "Float32"])
def test_template_must_be_uint16(tmp_path, template_type):
    cfg = make_cfg(tmp_path, template_type=template_type)
    problems = validate(cfg, 2022, ["DF"])
    assert any("must be UInt16" in p and template_type in p for p in problems), problems
    with pytest.raises(ConfigError, match="must be UInt16"):
        run(cfg, 2022, ["DF"])


def test_a_bad_template_is_reported_even_when_other_problems_exist(tmp_path):
    """Each template is checked on its own: one missing file must not hide another template's CRS."""
    cfg = make_cfg(tmp_path, epsg=4674)
    cfg["rasterization"]["resolutions"] = ["030", "01C"]
    cfg["resolutions"]["01C"] = {"template": str(tmp_path / "missing.tif")}
    problems = validate(cfg, 2022, ["DF"])
    assert any("not found" in p for p in problems) and any("EPSG:4326" in p for p in problems)


@pytest.mark.parametrize("kind, message", [
    ({"template_nodata": 255}, "has NoData 255"),
    ({"template_bands": 3}, "exactly one"),
], ids=["wrong-nodata", "3-bands"])
def test_template_is_the_output_so_it_must_be_one_band_with_the_configured_nodata(tmp_path, kind, message):
    cfg = make_cfg(tmp_path, **kind)
    with pytest.raises(ConfigError, match=message):
        run(cfg, 2022, ["DF"])


def test_template_with_the_configured_nodata_is_kept_and_counts_are_exact(tmp_path):
    cfg = make_cfg(tmp_path, template_nodata=65535, extra_points=255)
    rep = run(cfg, 2022, ["DF"])
    assert read_cells(raster(cfg, "CNEFE_ADDRESS-00"))[(3, 2)] == 255
    layer = next(r for r in rep["layers"] if r["layer"] == "CNEFE_ADDRESS-00")
    assert layer["total"] == 7 + 255 and not layer["saturated"]
    info = json.loads(subprocess.run(["gdalinfo", "-json", str(raster(cfg, "POPULATION_RESIDENT"))],
                                     capture_output=True, text=True).stdout)
    assert info["bands"][0]["type"] == "Float32" and info["bands"][0]["noDataValue"] == -9999


def test_a_template_that_is_not_blank_is_refused(tmp_path):
    cfg = make_cfg(tmp_path)
    subprocess.run(["gdal_rasterize", "-q", "-burn", "7", "-where", "p_i = 10",
                    str(Path(cfg["enrichment"]["output_dir"]) / "cnefe_2022_DF_enriched.gpkg"), cfg["resolutions"]["030"]["template"]],
                   check=True)
    with pytest.raises(ConfigError, match="not blank"):
        run(cfg, 2022, ["DF"])


def test_stage3_reads_the_enriched_layer_name_from_the_config(tmp_path):
    cfg = make_cfg(tmp_path, layer="points_53")                 # the file holds a layer called points_53
    cfg["enrichment"]["layer_name"] = "points_{uf_id}"          # the config says it with a placeholder
    run(cfg, 2022, ["DF"])
    assert read_cells(raster(cfg, "CNEFE_ADDRESS-00"))[(0, 0)] == 3


def test_refuses_to_overwrite(built):
    cfg, _ = built
    with pytest.raises(FileExistsError):
        run(cfg, 2022, ["DF"])
    run(cfg, 2022, ["DF"], overwrite=True)


def test_template_must_be_4326(tmp_path):
    cfg = make_cfg(tmp_path, epsg=4674)
    assert any("EPSG:4326" in p for p in validate(cfg, 2022, ["DF"]))


def test_catalog_code_must_be_in_dictionary(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["rasterization"]["catalog"]["2022"]["layers"].append(
        {"variable": "CNEFE", "variant": "ADDRESS", "spec": "09", "class": "count", "species": [9]})
    with pytest.raises(ConfigError, match="species 9"):
        build_catalog(cfg, 2022)


def _allocated(path: Path) -> int:
    return path.stat().st_blocks * 512


def test_two_ufs_are_added_into_the_same_raster(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["ufs"]["RO"] = 11
    enriched = Path(cfg["enrichment"]["output_dir"])
    shutil.copy(enriched / "cnefe_2022_DF_enriched.gpkg", enriched / "cnefe_2022_RO_enriched.gpkg")
    shutil.copy(enriched / "cnefe_2022_DF_stage2_report.json", enriched / "cnefe_2022_RO_stage2_report.json")
    run(cfg, 2022, ["DF", "RO"])
    assert read_cells(raster(cfg, "CNEFE_ADDRESS-00"))[(0, 0)] == 6                       # 3 + 3

def test_nonzero_fill_value_is_rejected_at_startup(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["rasterization"]["encoding"]["counts"]["fill_value"] = 5
    assert any("fill_value must be 0" in p for p in validate(cfg, 2022, ["DF"]))


def test_write_cog_rewrites_the_rasters_as_cloud_optimized_geotiffs(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["rasterization"]["geotiff_profile"]["write_cog"] = True
    run(cfg, 2022, ["DF"])
    f = raster(cfg, "CNEFE_ADDRESS-00")
    info = json.loads(subprocess.run(["gdalinfo", "-json", str(f)], capture_output=True, text=True).stdout)
    assert info["metadata"]["IMAGE_STRUCTURE"]["LAYOUT"] == "COG"
    assert info["metadata"]["IMAGE_STRUCTURE"]["COMPRESSION"] == "ZSTD"
    assert read_cells(f)[(0, 0)] == 3


def test_progress_is_logged_per_uf(tmp_path, caplog):
    import logging
    cfg = make_cfg(tmp_path)
    with caplog.at_level(logging.INFO, logger="stage_03"):
        run(cfg, 2022, ["DF"])
    text = caplog.text
    assert "[ 1/1] DF burning" in text and "[ 1/1] DF done" in text and "rasters finished]" in text
