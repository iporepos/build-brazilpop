"""Stage 1 manager: standardize raw CNEFE files, one UF at a time.

    python stages/stage_01_ingest_manager.py --config config.toml
    python stages/stage_01_ingest_manager.py --config config.toml --year 2022 --uf DF --uf RS
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from stages.config import (  # noqa: E402
    ConfigError, load_config, target_ufs, validate_stage1, year_block,
)

log = logging.getLogger("stage_01")


def _load_adapter(cfg: dict, year: int):
    rel = year_block(cfg, "ingestion", year)["adapter"]
    path = Path(rel)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.is_file():
        raise ConfigError(f"Adapter script not found: {path}")
    spec = importlib.util.spec_from_file_location(f"adapter_{year}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "ingest"):
        raise ConfigError(f"Adapter {path} has no ingest(cfg, uf) function")
    return module


def run(cfg: dict, year: int, ufs: list[str], overwrite: bool | None = None) -> list[dict]:
    problems = validate_stage1(cfg, year, ufs)
    if problems:
        raise ConfigError("Config validation failed:\n  - " + "\n  - ".join(problems))
    adapter = _load_adapter(cfg, year)
    reports = []
    for uf in ufs:
        log.info("Ingesting %s %s", year, uf)
        rep = adapter.ingest(cfg, uf, overwrite=overwrite)
        log.info("%s done in %ss: %s rows written, %s households, %s dropped (invalid coords)",
                 uf, rep["seconds"], f"{rep['rows_written']:,}", f"{rep['households']:,}",
                 f"{rep['dropped_invalid_coords']:,}")
        reports.append(rep)
    return reports


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--year", type=int, help="census year (default: execution.target_year)")
    ap.add_argument("--uf", action="append", help="UF acronym, repeatable (default: execution.target_ufs)")
    ap.add_argument("--overwrite", action="store_true", help="replace existing outputs")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
        logging.basicConfig(
            level=cfg.get("execution", {}).get("log_level", "INFO"),
            format="%(asctime)s %(levelname)s %(message)s",
        )
        year = args.year or cfg["execution"]["target_year"]
        ufs = target_ufs(cfg, year, args.uf)
        if not ufs:
            raise ConfigError("No UFs to process")
        run(cfg, year, ufs, overwrite=True if args.overwrite else None)
    except (ConfigError, FileExistsError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
