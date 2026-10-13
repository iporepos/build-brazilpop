"""Run one stage of the BrazilPop pipeline.

    .venv/bin/python run_pipeline.py --config config.local.toml --stage 4 [--year 2022] [--overwrite]

--stage    1 ingest, 2 vector enrichment, 3 rasterization, 4 benchmark (required)
--year     census year (default: execution.target_year of the config)
--overwrite replace existing outputs (default: execution.overwrite of the config)

Any other option is handed to the stage as it is (for instance `--uf DF`, or `--no-plots` in
stage 4), so the wrapper adds nothing of its own to what a stage does.
"""

from __future__ import annotations

import argparse
import importlib
import sys

STAGES = {
    1: ("stages.stage_01_ingest_manager", "ingest"),
    2: ("stages.stage_02_vector_enrichment", "vector enrichment"),
    3: ("stages.stage_03_additive_rasterize", "rasterization"),
    4: ("stages.stage_04_benchmark", "benchmark"),
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], epilog="Other options go to the stage.")
    ap.add_argument("--config", required=True, help="the pipeline TOML file")
    ap.add_argument("--stage", required=True, type=int, choices=sorted(STAGES),
                    help="; ".join(f"{n} {name}" for n, (_, name) in STAGES.items()))
    ap.add_argument("--year", type=int, help="census year (default: execution.target_year)")
    ap.add_argument("--overwrite", action="store_true", help="replace existing outputs")
    args, extra = ap.parse_known_args(argv)

    forwarded = ["--config", args.config]
    if args.year is not None:
        forwarded += ["--year", str(args.year)]
    if args.overwrite:
        forwarded.append("--overwrite")
    module = importlib.import_module(STAGES[args.stage][0])
    return module.main(forwarded + extra)


if __name__ == "__main__":
    sys.exit(main())
