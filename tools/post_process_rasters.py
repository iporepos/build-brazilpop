"""Post-process the Stage 3 rasters: compress, overviews, COG layout, in that order.

    .venv/bin/python tools/post_process_rasters.py --config post_process.json [--dry-run]

Order matters: the compression rewrites the file with gdal_translate, which does not carry
overviews over, so the overviews are built AFTERWARDS; and gdaladdo appends them at the end of
the file, which is not a COG layout, so a final gdal_translate -of COG puts the file in order.
All steps run on a working copy; the original is replaced only when the copy is verified
(same size, grid, type and NoData; "verify": true adds the band checksum), so an interruption
never damages a raster. The .qml styles and any .part.tif are not touched.

The original is replaced when the result is smaller, or when it gains overviews or the COG
layout it lacked. With "output_dir" set, nothing in input_dir is touched: every raster (the
result, or a copy of the original when there is no gain) and its .qml are written there under
the same sub-folders; files already present are skipped unless "overwrite" is true.

Switch steps off with "compress.enabled", "overviews.enabled" and "cog" in the JSON file
(tools/post_process.example.json). Overviews use AVERAGE by default (GDAL 3.8 has no SUM):
display-only, the cells show a mean count, not a total.
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
               do_compress=None, do_overviews=None)


if __name__ == "__main__":
    sys.exit(main())
