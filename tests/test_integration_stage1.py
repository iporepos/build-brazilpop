"""Stage 1 on the real test data declared in local.test.toml.

Skipped when local.test.toml or its test input is absent (e.g. a fresh clone).
Only data-independent invariants are asserted, so the test keeps working if
the test file is swapped for another UF.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pyogrio
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from stages.config import expand, layer_name, load_config, output_dir, year_block  # noqa: E402
from stages.stage_01_ingest_manager import run  # noqa: E402

CONFIG = REPO_ROOT / "local.test.toml"


@pytest.fixture(scope="module", autouse=True)
def _from_repo_root():
    """Paths in local.test.toml are relative to the repository root."""
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(REPO_ROOT)
        yield


@pytest.fixture(scope="module", params=[2022, 2010], ids=["2022", "2010"])
def year(request):
    return request.param


@pytest.fixture(scope="module")
def result(_from_repo_root, year):
    if not CONFIG.is_file():
        pytest.skip("local.test.toml not present")
    cfg = load_config(CONFIG)
    ufs = cfg["execution"]["target_ufs"]
    src = Path(expand(year_block(cfg, "ingestion", year)["input"], cfg, year=year, uf=ufs[0]))
    if not src.is_file():
        pytest.skip(f"{year} test input missing: {src}")
    return cfg, run(cfg, year, ufs, overwrite=True)


def test_no_row_is_lost(result):
    _, reports = result
    for rep in reports:
        assert rep["rows_written"] + rep["dropped_invalid_coords"] == rep["rows_read"]


def test_output_matches_report(result, year):
    cfg, reports = result
    for rep in reports:
        info = pyogrio.read_info(rep["output"], layer=layer_name(cfg, "ingestion", year, rep["uf"]))
        assert info["features"] == rep["rows_written"]
        assert info["crs"] == "EPSG:4326"
        assert list(info["fields"]) == ["is_household", "cod_especie", "cod_subtipo"]


def test_inventory_accounts_for_every_row(result, year):
    cfg, reports = result
    out = output_dir(cfg, "ingestion", year)
    for rep in reports:
        inv = pd.read_csv(out / f"cnefe_{year}_{rep['uf']}_code_inventory.csv")
        for field in ("cod_especie", "cod_subtipo"):
            assert inv[inv.field == field]["count"].sum() == rep["rows_read"]


def test_households_equal_dictionary_household_species(result, year):
    cfg, reports = result
    hh = {int(c) for c, v in cfg["codes"][str(year)]["especie"].items() if v.get("household")}
    out = output_dir(cfg, "ingestion", year)
    for rep in reports:
        inv = pd.read_csv(out / f"cnefe_{year}_{rep['uf']}_code_inventory.csv")
        sp = inv[(inv.field == "cod_especie") & inv.code.isin(hh)]
        assert rep["households"] == sp["count"].sum()
