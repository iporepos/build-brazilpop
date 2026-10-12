"""Add internal overviews (pyramids) to the Stage 3 rasters (overviews step only).

    .venv/bin/python tools/add_overviews.py --config post_process.json [--dry-run]

Overviews are display-only (a quick zoom-out in QGIS); they are not used by the analyses.
GDAL (3.8) has no SUM method for overviews, so the default is AVERAGE: an overview cell shows
the MEAN count of the cells it covers, not their total. Other gdaladdo methods can be chosen
with "overviews.resampling" (NEAREST, AVERAGE, RMS, MODE, ...).

Files that already carry overviews are skipped ("overviews.force": true rebuilds them). With
"cog": true the file is then rewritten in the COG layout, keeping its overviews.
Settings come from the JSON file (tools/post_process.example.json).
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
               do_compress=False, do_overviews=True)


if __name__ == "__main__":
    sys.exit(main())
