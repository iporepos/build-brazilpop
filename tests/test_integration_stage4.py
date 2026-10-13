"""Stage 4 on the real DF test data declared in local.test.toml.

Needs the Stage 3 rasters, the census database and the external rasters. Skipped when
local.test.toml or any of them is absent. Data-independent invariants only.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from stages.config import load_config  # noqa: E402
from stages.stage_04_benchmark import benchmark, validate  # noqa: E402

CONFIG = REPO_ROOT / "local.test.toml"
YEAR = 2022


@pytest.fixture(scope="module")
def result():
    if not CONFIG.is_file() or shutil.which("gdal_rasterize") is None:
        pytest.skip("local.test.toml or GDAL not available")
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(REPO_ROOT)
        cfg = load_config(CONFIG)
        ufs = cfg["execution"]["target_ufs"]
        problems = validate(cfg, YEAR, ufs)
        if problems:
            pytest.skip("Stage 4 inputs not available: " + "; ".join(problems[:2]))
        yield cfg, benchmark(cfg, YEAR, ufs, overwrite=True)


def test_census_total_is_the_microdata_total(result):
    _, rep = result
    t = rep["_tables"]["tract"]
    assert t["p_k"].sum() == pytest.approx(rep["census_total"])
    assert rep["units_without_census_total"]["tract"] == 0


def test_no_mass_is_created_by_the_sampling(result):
    """Every cell belongs to at most one polygon, so the sum over the polygons cannot exceed
    the raster total, and for BrazilPop it is within 1% of it."""
    _, rep = result
    for r in rep["rasters"]:
        for scale, inside in r["inside_polygons"].items():
            assert inside <= r["raster_total_in_windows"] * (1 + 1e-9), (r["product"], scale)
    bp = [r for r in rep["rasters"] if r["product"] == "brazilpop"]
    for r in bp:
        for inside in r["inside_polygons"].values():
            assert inside >= 0.99 * r["raster_total_in_windows"]


def test_scales_agree_on_the_state_total(result):
    _, rep = result
    t = rep["_tables"]
    for key in ("brazilpop", "landscan"):
        assert t["state"][f"pi_sum_{key}_2022"].sum() == pytest.approx(t["municipality"][f"pi_sum_{key}_2022"].sum())
        assert t["state"]["p_k"].sum() == pytest.approx(t["municipality"]["p_k"].sum())


def test_brazilpop_is_close_to_the_census_in_total(result):
    _, rep = result
    m = rep["_metrics"].set_index(["product", "scale"])
    for scale in ("tract", "district", "municipality", "state"):
        assert 0.97 < m.loc[("brazilpop", scale), "ratio_product_to_census"] < 1.03
