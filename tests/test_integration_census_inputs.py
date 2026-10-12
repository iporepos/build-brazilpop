"""Stage 2 inputs on the real census database declared in local.test.toml.

Skipped when local.test.toml or the database is absent. Data-independent
invariants only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from stages.census_inputs import load_microdata, load_tracts, match_report  # noqa: E402
from stages.config import expand, load_config, uf_id, year_block  # noqa: E402

CONFIG = REPO_ROOT / "local.test.toml"
YEAR = 2022


@pytest.fixture(scope="module")
def loaded():
    if not CONFIG.is_file():
        pytest.skip("local.test.toml not present")
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(REPO_ROOT)
        cfg = load_config(CONFIG)
        uf = cfg["execution"]["target_ufs"][0]
        b = year_block(cfg, "enrichment", YEAR)
        if not Path(expand(b["tract_path"], cfg, year=YEAR, uf=uf)).is_file():
            pytest.skip("census database not available on this machine")
        tracts = load_tracts(cfg, YEAR, uf)
        micro = load_microdata(cfg, YEAR, uf)
        yield cfg, uf, tracts, micro


def test_tracts_belong_to_uf_and_are_4326(loaded):
    cfg, uf, tracts, _ = loaded
    assert tracts.crs.to_epsg() == 4326
    assert tracts.tract_code.str.startswith(f"{uf_id(cfg, uf):02d}").all()
    assert tracts.tract_code.is_unique
    assert tracts.geometry.is_valid.all()


def test_microdata_relates_to_tracts(loaded):
    _, _, tracts, micro = loaded
    r = match_report(tracts, micro)
    assert r["matched"] > 0
    assert r["p_t_total"] > 0
    assert r["p_t_null_rows"] == 0
