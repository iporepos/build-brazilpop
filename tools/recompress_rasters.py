"""Shrink the Stage 3 rasters with gdal_translate (compression step only).

    .venv/bin/python tools/recompress_rasters.py --config post_process.json [--dry-run]

Why: counts are burned into a copy of the template with `gdal_rasterize -add`, once per UF.
Every pass rewrites compressed blocks, and a block that grows is appended to the file, leaving
the old copy as dead space. A plain gdal_translate writes each block once and drops it.
The pixels, type, NoData, georeferencing and tiling grid are not changed.

All settings come from the JSON file (tools/post_process.example.json), separate from the
pipeline config. Note that a rewrite does not carry overviews over: to keep them, use
tools/post_process_rasters.py, which compresses first and builds the overviews afterwards.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.postprocess_common import run  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    return run(argparse.ArgumentParser(description=__doc__.splitlines()[0]), argv,
               do_compress=True, do_overviews=False)


if __name__ == "__main__":
    sys.exit(main())
