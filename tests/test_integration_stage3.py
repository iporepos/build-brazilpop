"""Stage 3 on the real DF test data declared in local.test.toml (needs Stage 2 output in T2).

Skipped when local.test.toml, the templates or the Stage 2 output are absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from stages.config import load_config  # noqa: E402
from stages.stage_03_additive_rasterize import build_catalog, enriched_path, out_dir, raster_name, run  # noqa: E402

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
        needed = [Path(b["template"]) for b in cfg["resolutions"].values()] + [enriched_path(cfg, YEAR, u) for u in ufs]
        if not all(p.is_file() for p in needed):
            pytest.skip("templates or Stage 2 output not available")
        yield cfg, run(cfg, YEAR, ufs, overwrite=True)


def test_every_catalog_layer_is_written_at_every_resolution(result):
    cfg, rep = result
    layers = build_catalog(cfg, YEAR)
    assert rep["rasters"] == len(layers) * len(cfg["rasterization"]["resolutions"])
    for res in cfg["rasterization"]["resolutions"]:
        for lay in layers:
            f = out_dir(cfg, res, YEAR) / raster_name(cfg, lay, res, YEAR)
            assert f.is_file() and f.with_suffix(".qml").is_file()


def test_consistency_checks_hold(result):
    _, rep = result
    failed = [c for c in rep["checks"] if not c["ok"]]
    assert not failed, failed


def test_outputs_are_cogs_on_the_template_grid(result):
    cfg, rep = result
    for row in rep["layers"]:
        info = json.loads(subprocess.run(["gdalinfo", "-json", row["file"]], capture_output=True, text=True).stdout)
        tpl = json.loads(subprocess.run(["gdalinfo", "-json", cfg["resolutions"][row["resolution"]]["template"]],
                                        capture_output=True, text=True).stdout)
        assert info["metadata"]["IMAGE_STRUCTURE"]["LAYOUT"] == "COG"
        assert info["size"] == tpl["size"] and info["geoTransform"] == tpl["geoTransform"]
