"""Settings of the raster post-processing tools (kept apart from the pipeline config).

One JSON file drives tools/recompress_rasters.py, tools/add_overviews.py and
tools/post_process_rasters.py (engine: tools/postprocess_common.py) (see tools/post_process.example.json). Keys that start with
"_" are comments. Unknown keys and bad values are errors, so a typo cannot go unnoticed.
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULTS = {
    "input_dir": None,                 # required: searched recursively for *.tif
    "output_dir": None,                # null = replace the rasters in place; a folder = write the results
                                       # there (same sub-folders, .qml copied) and leave the originals alone
    "overwrite": False,                # output_dir only: redo files that already exist there
    "workers": 2,                      # files processed in parallel
    "verify": False,                   # also compare the band checksum (reads files in full)
    "max_growth_pct": 25,              # keep the original if the result would be more than this % larger
                                       # (overviews and COG layout may add some size, not triple it)
    "cog": True,                       # last step: rewrite in the Cloud Optimized GeoTIFF layout
    "compress": {
        "enabled": True,
        "zstd_level": 9,               # 1-22, higher is slower and smaller
        "blocksize": 512,
        # 1 = none, 2 = horizontal difference, 3 = floating point. Measured on a dense 12000 x 12000
        # window of the 2022 rasters (mostly zeros): none gave the smallest files, 3 gave +60% on floats.
        "predictor_integer": 1,
        "predictor_float": 1,
    },
    "overviews": {
        "enabled": True,
        "resampling": "AVERAGE",       # gdaladdo method; GDAL 3.8 has no SUM
        "levels": None,                # e.g. [2, 4, 8, 16]; null = 2, 4, 8, ... until min_side
        "first_level": 4,              # skip 2x: it is the costliest overview and the least useful
        "min_side": 256,               # smallest overview keeps at least this many cells on its short side
        "zstd_level": 9,
        "blocksize": 512,
        "force": False,                # rebuild on files that already have overviews
    },
}
RESAMPLING = {"NEAREST", "AVERAGE", "RMS", "BILINEAR", "CUBIC", "CUBICSPLINE", "LANCZOS", "GAUSS", "MODE"}


class PostProcessConfigError(Exception):
    pass


def _merge(defaults: dict, given: dict, where: str) -> dict:
    out = {}
    for key in given:
        if not key.startswith("_") and key not in defaults:
            raise PostProcessConfigError(f"unknown key {where}{key!r} (allowed: {', '.join(defaults)})")
    for key, default in defaults.items():
        value = given.get(key, default)
        if isinstance(default, dict):
            if not isinstance(value, dict):
                raise PostProcessConfigError(f"{where}{key} must be an object")
            value = _merge(default, value, f"{where}{key}.")
        out[key] = value
    return out


def load(path: str | Path) -> dict:
    path = Path(path)
    try:
        cfg = _merge(DEFAULTS, json.loads(path.read_text(encoding="utf-8")), "")
    except FileNotFoundError:
        raise PostProcessConfigError(f"config file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise PostProcessConfigError(f"{path} is not valid JSON: {exc}") from None
    if not cfg["input_dir"]:
        raise PostProcessConfigError("input_dir is required")
    if not Path(cfg["input_dir"]).is_dir():
        raise PostProcessConfigError(f"input_dir is not a folder: {cfg['input_dir']}")
    if cfg["output_dir"]:
        src, dst = Path(cfg["input_dir"]).resolve(), Path(cfg["output_dir"]).resolve()
        if dst == src or src in dst.parents:
            raise PostProcessConfigError("output_dir must not be input_dir or inside it (its files would be "
                                         "found as inputs); use null to work in place")
    else:
        cfg["output_dir"] = None
    for flag in ("cog", "verify", "overwrite"):
        if not isinstance(cfg[flag], bool):
            raise PostProcessConfigError(f"{flag} must be true or false")
    if not isinstance(cfg["workers"], int) or cfg["workers"] < 1:
        raise PostProcessConfigError("workers must be an integer >= 1")
    for section in ("compress", "overviews"):
        lvl = cfg[section]["zstd_level"]
        if not isinstance(lvl, int) or not 1 <= lvl <= 22:
            raise PostProcessConfigError(f"{section}.zstd_level must be an integer from 1 to 22")
    for key in ("predictor_integer", "predictor_float"):
        if cfg["compress"][key] not in (1, 2, 3):
            raise PostProcessConfigError(f"compress.{key} must be 1, 2 or 3")
    if not isinstance(cfg["max_growth_pct"], (int, float)) or cfg["max_growth_pct"] < 0:
        raise PostProcessConfigError("max_growth_pct must be a number >= 0")
    if not isinstance(cfg["overviews"]["first_level"], int) or cfg["overviews"]["first_level"] < 2:
        raise PostProcessConfigError("overviews.first_level must be an integer >= 2")
    ov = cfg["overviews"]
    ov["resampling"] = str(ov["resampling"]).upper()
    if ov["resampling"] not in RESAMPLING:
        raise PostProcessConfigError(f"overviews.resampling {ov['resampling']!r} is not one of {sorted(RESAMPLING)}")
    if ov["levels"] is not None and (not isinstance(ov["levels"], list) or
                                     not all(isinstance(v, int) and v >= 2 for v in ov["levels"])):
        raise PostProcessConfigError("overviews.levels must be null or a list of integers >= 2")
    return cfg
