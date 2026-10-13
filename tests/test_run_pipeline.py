"""run_pipeline.py: picks the stage and forwards the options."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import run_pipeline  # noqa: E402


@pytest.mark.parametrize("stage,module", [(n, m) for n, (m, _) in run_pipeline.STAGES.items()])
def test_each_stage_is_called_with_the_forwarded_options(monkeypatch, stage, module):
    calls = []
    monkeypatch.setattr(importlib.import_module(module), "main", lambda argv: calls.append(argv) or 0)
    rc = run_pipeline.main(["--config", "c.toml", "--stage", str(stage), "--year", "2010", "--overwrite",
                            "--uf", "DF"])
    assert rc == 0
    assert calls == [["--config", "c.toml", "--year", "2010", "--overwrite", "--uf", "DF"]]


def test_year_and_overwrite_are_optional_and_the_exit_code_is_the_stages(monkeypatch):
    import stages.stage_04_benchmark as s4
    calls = []
    monkeypatch.setattr(s4, "main", lambda argv: calls.append(argv) or 1)
    assert run_pipeline.main(["--config", "c.toml", "--stage", "4"]) == 1
    assert calls == [["--config", "c.toml"]]


@pytest.mark.parametrize("argv", [["--config", "c.toml"], ["--stage", "4"], ["--config", "c.toml", "--stage", "5"]])
def test_config_and_a_valid_stage_are_mandatory(argv):
    with pytest.raises(SystemExit) as e:
        run_pipeline.main(argv)
    assert e.value.code == 2
