"""Reference data in data/*.json and the config layout."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from stages.config import ConfigError, load_config, output_dir  # noqa: E402

EXAMPLE = REPO_ROOT / "config.example.toml"


def test_example_config_loads_with_reference_data():
    cfg = load_config(EXAMPLE)
    assert cfg["ufs"]["DF"] == 53 and len(cfg["ufs"]) == 27
    assert cfg["codes"]["2022"]["especie"]["1"]["household"] is True
    assert [e["spec"] for e in cfg["rasterization"]["catalog"]["core"]["layers"] if "spec" in e] == ["00", "000"]
    assert Path(cfg["data"]["styles"]).is_dir()


def test_every_stage_declares_its_own_output_dir():
    cfg = load_config(EXAMPLE)
    dirs = [output_dir(cfg, s, 2022) for s in ("ingestion", "enrichment", "rasterization", "benchmarking")]
    assert len(set(dirs)) == 4


def test_missing_output_dir_is_a_clear_error():
    cfg = load_config(EXAMPLE)
    del cfg["enrichment"]["output_dir"]
    with pytest.raises(ConfigError, match=r"\[enrichment\] needs an explicit `output_dir`"):
        output_dir(cfg, "enrichment")


def test_output_dir_year_placeholder_separates_census_years():
    cfg = load_config(EXAMPLE)
    cfg["ingestion"]["output_dir"] = "/data/{year}/02_standardized"
    assert output_dir(cfg, "ingestion", 2010) == Path("/data/2010/02_standardized")
    assert output_dir(cfg, "ingestion", 2022) == Path("/data/2022/02_standardized")
    with pytest.raises(ConfigError, match="no year was given"):
        output_dir(cfg, "ingestion")
    cfg["ingestion"]["output_dir"] = "/data/02_standardized"       # no placeholder: years share the folder
    assert output_dir(cfg, "ingestion", 2010) == output_dir(cfg, "ingestion", 2022)


def test_reference_data_is_consistent():
    """Every catalog code exists in the dictionary; every UF has a unique 2-digit code."""
    cfg = load_config(EXAMPLE)
    assert len(set(cfg["ufs"].values())) == len(cfg["ufs"])
    assert all(10 <= v <= 99 for v in cfg["ufs"].values())
    for year, block in cfg["rasterization"]["catalog"].items():
        if year == "core":
            continue
        for lay in block["layers"]:
            for c in lay.get("species", []):
                assert str(c) in cfg["codes"][year]["especie"], (year, lay)
            for c in lay.get("subtype", []):
                assert str(c) in cfg["codes"][year]["subtipo"], (year, lay)


@pytest.mark.parametrize("extra, message", [
    ('[ufs]\nDF = 53\n', r"\[ufs\] is now fixed reference data"),
    ('[codes.2022.especie]\n1 = { label = "x", household = true }\n', r"\[codes\] is now fixed reference data"),
    ('[paths]\nrasters = "x"\n', r"\[paths\] and \[rasterization.styles\] are gone"),
])
def test_outdated_layout_is_rejected_with_a_pointer(tmp_path, extra, message):
    cfg = tmp_path / "old.toml"
    cfg.write_text(EXAMPLE.read_text() + "\n" + extra, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(cfg)


def test_data_dir_can_be_replaced_for_tests(tmp_path):
    for name in ("ufs.json", "cnefe_codes.json", "layer_catalog.json"):
        (tmp_path / name).write_text((REPO_ROOT / "data" / name).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "ufs.json").write_text(json.dumps({"XX": 99}), encoding="utf-8")
    cfg = load_config(EXAMPLE, data_dir=tmp_path)
    assert cfg["ufs"] == {"XX": 99}


def test_missing_reference_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="ufs.json"):
        load_config(EXAMPLE, data_dir=tmp_path)


LOCAL_TEST = REPO_ROOT / "local.test.toml"


def _key_paths(d: dict, prefix: tuple = ()) -> set[tuple]:
    out = set()
    for k, v in d.items():
        out |= _key_paths(v, prefix + (k,)) if isinstance(v, dict) else {prefix + (k,)}
    return out


def test_local_test_config_has_the_same_keys_as_the_example():
    """The two files differ only in values. A key added to one must be added to the other."""
    if not LOCAL_TEST.is_file():
        pytest.skip("local.test.toml not present")
    import tomllib
    ex = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    lt = tomllib.loads(LOCAL_TEST.read_text(encoding="utf-8"))
    assert _key_paths(ex) == _key_paths(lt), sorted(_key_paths(ex) ^ _key_paths(lt))


def test_every_key_of_the_example_is_read_somewhere_in_the_code():
    """Guards against dead keys. A key name must appear as a quoted string in stages/*.py."""
    import re
    import tomllib
    src = "\n".join(p.read_text(encoding="utf-8") for p in (REPO_ROOT / "stages").rglob("*.py"))
    dead = sorted(".".join(p) for p in _key_paths(tomllib.loads(EXAMPLE.read_text(encoding="utf-8")))
                  if not re.search(r"""["']%s["']""" % re.escape(p[-1]), src))
    assert not dead, f"config keys that no code reads: {dead}"
