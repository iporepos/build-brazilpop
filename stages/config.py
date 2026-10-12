"""Config loading, placeholder expansion and path validation.

Two kinds of information, kept apart:

  * the user's config file (a TOML passed with --config): where the user's
    inputs are, which columns they use, where each stage writes;
  * fixed reference data shipped in the repository's data/ folder, which does
    not depend on the user: ufs.json (UF lookup), cnefe_codes.json (code
    dictionaries), layer_catalog.json (raster layers) and styles/ (QGIS styles).

load_config merges the reference data into the config under the keys the stages
read (`ufs`, `codes`, `rasterization.catalog`, `data.styles`), so stage code
does not care where a value came from.

Inputs are declared by the user with full paths (see config.example.toml).
This module never assumes a folder layout or file naming.
"""

from __future__ import annotations

import json
import string
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

# Sections that now live in data/*.json. A config defining them would be a second
# source of truth, so it is rejected.
MOVED_TO_DATA = {
    "ufs": "data/ufs.json",
    "codes": "data/cnefe_codes.json",
}


class ConfigError(Exception):
    """The config file is incomplete or inconsistent."""


def _read_json(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"Reference data file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} is not valid JSON: {e}") from None


def load_config(path: str | Path, data_dir: str | Path | None = None) -> dict:
    """Read the user's TOML and merge the repository's reference data into it.

    `data_dir` exists for tests; normal runs always use the repository's data/.
    """
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    with open(path, "rb") as f:
        cfg = tomllib.load(f)

    problems = [f"[{k}] is now fixed reference data in {v}; remove it from the config"
                for k, v in MOVED_TO_DATA.items() if k in cfg]
    if "catalog" in cfg.get("rasterization", {}):
        problems.append("[rasterization.catalog] is now fixed reference data in data/layer_catalog.json; "
                        "remove it from the config")
    if "styles" in cfg.get("rasterization", {}) or "paths" in cfg:
        problems.append("[paths] and [rasterization.styles] are gone: each stage declares its own "
                        "`output_dir` in its block, and the QML styles are read from data/styles/")
    if problems:
        raise ConfigError(f"{path.name} uses an outdated layout:\n  - " + "\n  - ".join(problems))

    data = Path(data_dir) if data_dir else DATA_DIR
    cfg["ufs"] = _read_json(data / "ufs.json")
    cfg["codes"] = _read_json(data / "cnefe_codes.json")
    cfg.setdefault("rasterization", {})["catalog"] = _read_json(data / "layer_catalog.json")
    cfg["data"] = {"dir": str(data), "styles": str(data / "styles")}
    return cfg


def output_dir(cfg: dict, section: str, year: int | None = None) -> Path:
    """The folder a stage writes to, declared as `output_dir` in its own block.

    `{year}` may be used in the value (e.g. "/data/{year}/02_standardized") to keep
    census years in separate folders. Without it, years share the folder and are
    told apart by the year in every file name.
    """
    try:
        value = cfg[section]["output_dir"]
    except KeyError:
        raise ConfigError(f"[{section}] needs an explicit `output_dir`") from None
    if not value:
        raise ConfigError(f"[{section}] output_dir is empty")
    if "{year}" in value:
        if year is None:
            raise ConfigError(f"[{section}] output_dir uses {{year}} but no year was given")
        value = value.replace("{year}", str(year))
    return Path(value)


def uf_id(cfg: dict, uf: str) -> int:
    try:
        return int(cfg["ufs"][uf])
    except KeyError:
        raise ConfigError(f"UF '{uf}' is not declared in [ufs]") from None


def expand(template: str, cfg: dict, *, year: int | None = None, uf: str | None = None) -> str:
    """Expand {year}, {uf} and {uf_id} in a path or name template.

    Placeholders are optional. A placeholder that is used but cannot be
    resolved (e.g. {uf} with no uf given) raises ConfigError.
    """
    values: dict[str, object] = {}
    if year is not None:
        values["year"] = year
    if uf is not None:
        values["uf"] = uf
        values["uf_id"] = uf_id(cfg, uf)

    used = {f for _, f, _, _ in string.Formatter().parse(template) if f}
    missing = used - values.keys()
    if missing:
        raise ConfigError(f"Cannot resolve {sorted(missing)} in '{template}'")
    return template.format_map(values)


DEFAULT_LAYER_NAME = "addresses"


def layer_name(cfg: dict, section: str, year: int, uf: str) -> str:
    """Name of the point layer a stage writes (and the next stage reads).

    Taken from `layer_name` in the stage's own block ([ingestion] or [enrichment]),
    default "addresses". It may use {year}, {uf} and {uf_id}.
    """
    return expand(cfg.get(section, {}).get("layer_name", DEFAULT_LAYER_NAME), cfg, year=year, uf=uf)


def year_block(cfg: dict, section: str, year: int) -> dict:
    """Return cfg[section][str(year)] or raise a clear error."""
    try:
        return cfg[section][str(year)]
    except KeyError:
        raise ConfigError(f"No [{section}.{year}] block in the config") from None


def target_ufs(cfg: dict, year: int, override: list[str] | None = None) -> list[str]:
    """UFs to process: CLI override, else [execution].target_ufs, else every
    UF in [ufs] whose declared input file exists."""
    chosen = override or cfg.get("execution", {}).get("target_ufs") or []
    if chosen:
        for uf in chosen:
            uf_id(cfg, uf)  # validates
        return list(chosen)
    template = year_block(cfg, "ingestion", year)["input"]
    return [uf for uf in cfg["ufs"] if Path(expand(template, cfg, year=year, uf=uf)).is_file()]


def _check_writable(section: str, cfg: dict, year: int) -> list[str]:
    try:
        out = output_dir(cfg, section, year)
    except ConfigError as e:
        return [str(e)]
    try:
        out.mkdir(parents=True, exist_ok=True)
        probe = out / ".write_test"
        probe.touch()
        probe.unlink()
    except OSError as e:
        return [f"[{section}] output_dir not writable: {out} ({e})"]
    return []


def validate_stage1(cfg: dict, year: int, ufs: list[str]) -> list[str]:
    """Return a list of problems found before Stage 1 starts (empty = ok)."""
    problems: list[str] = []
    block = year_block(cfg, "ingestion", year)

    for key in ("adapter", "input", "col_lon", "col_lat", "col_species"):
        if not block.get(key):
            problems.append(f"[ingestion.{year}] missing required key '{key}'")

    if f"{year}" not in cfg.get("codes", {}):
        problems.append(f"No [codes.{year}] dictionary")
    elif not cfg["codes"][str(year)].get("especie"):
        problems.append(f"[codes.{year}.especie] is empty")

    if "input" in block:
        for uf in ufs:
            p = Path(expand(block["input"], cfg, year=year, uf=uf))
            if not p.is_file():
                problems.append(f"Input file not found for {uf}: {p}")

    problems += _check_writable("ingestion", cfg, year)

    return problems


def validate_stage2(cfg: dict, year: int, ufs: list[str]) -> list[str]:
    """Return a list of problems found before Stage 2 starts (empty = ok)."""
    problems: list[str] = []
    try:
        b = year_block(cfg, "enrichment", year)
    except ConfigError as e:
        return [str(e)]

    for key in ("tract_path", "tract_code_column", "microdata_path", "microdata_code_column",
                "population_column", "household_column"):
        if not b.get(key):
            problems.append(f"[enrichment.{year}] missing required key '{key}'")

    suffix = Path(str(b.get("microdata_path", ""))).suffix.lower()
    if suffix in {".csv", ".txt", ".tsv"} and not b.get("microdata_delimiter"):
        problems.append(f"[enrichment.{year}] microdata_path is delimited text: microdata_delimiter is required")

    for key in ("tract_path", "microdata_path"):
        if b.get(key):
            for uf in ufs:
                p = Path(expand(b[key], cfg, year=year, uf=uf))
                if not p.is_file():
                    problems.append(f"{key} not found for {uf}: {p}")

    std_dir = output_dir(cfg, "ingestion", year)
    for uf in ufs:
        f = std_dir / expand(cfg["ingestion"]["output_pattern"], cfg, year=year, uf=uf)
        if not f.is_file():
            problems.append(f"Stage 1 output not found for {uf}: {f} (run Stage 1 first)")

    problems += _check_writable("enrichment", cfg, year)
    return problems
