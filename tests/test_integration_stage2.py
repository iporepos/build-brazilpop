"""Stage 2 on the real DF test data declared in local.test.toml (needs Stage 1 output in T1).

Skipped when local.test.toml, the census database or the Stage 1 output is absent.
Data-independent invariants only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pyogrio
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from stages.config import expand, load_config, output_dir, year_block  # noqa: E402
from stages.stage_02_vector_enrichment import enrich  # noqa: E402

CONFIG = REPO_ROOT / "local.test.toml"
YEAR = 2022


@pytest.fixture(scope="module")
def result():
    if not CONFIG.is_file():
        pytest.skip("local.test.toml not present")
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(REPO_ROOT)
        cfg = load_config(CONFIG)
        uf = cfg["execution"]["target_ufs"][0]
        census = Path(expand(year_block(cfg, "enrichment", YEAR)["tract_path"], cfg, year=YEAR, uf=uf))
        std = output_dir(cfg, "ingestion", YEAR) / expand(cfg["ingestion"]["output_pattern"], cfg, year=YEAR, uf=uf)
        if not census.is_file() or not std.is_file():
            pytest.skip("census database or Stage 1 output not available")
        rep = enrich(cfg, YEAR, uf, overwrite=True)
        yield cfg, rep


def test_every_point_is_written(result):
    _, rep = result
    info = pyogrio.read_info(rep["outputs"]["enriched"], layer=rep["enriched_layer"])
    assert info["features"] == rep["points"]
    assert info["crs"] == "EPSG:4326"


def test_mass_is_conserved(result):
    _, rep = result
    assert rep["conserved"], rep["conservation_gap"]
    total = rep["p_allocated"] + rep["p_unallocated_no_household_points"] + rep["p_unallocated_no_tract"]
    assert total == pytest.approx(rep["p_total"], rel=1e-9)


def test_tract_mass_is_exact_for_ok_tracts(result):
    _, rep = result
    e = pyogrio.read_dataframe(rep["outputs"]["enriched"], layer=rep["enriched_layer"], read_geometry=False,
                               columns=["is_household", "cd_setor", "p_i"])
    b = pyogrio.read_dataframe(rep["outputs"]["balance"], layer="tract_balance",
                               read_geometry=False).set_index("tract_code")
    ok = b[b.status == "ok"]
    per = e[e.is_household == 1].groupby("cd_setor").p_i.sum().reindex(ok.index)
    assert np.allclose(per.to_numpy(), ok.p_t.to_numpy(), atol=1e-6)


def test_non_household_points_carry_zero(result):
    _, rep = result
    e = pyogrio.read_dataframe(rep["outputs"]["enriched"], layer=rep["enriched_layer"], read_geometry=False,
                               columns=["is_household", "p_i"])
    assert (e[e.is_household == 0].p_i == 0.0).all()
    assert e[e.is_household == 1].p_i.notna().sum() > 0
