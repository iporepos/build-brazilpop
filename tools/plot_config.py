"""Settings of tools/plot_benchmark.py (kept apart from the pipeline config).

One small JSON file drives the tool (see tools/plot_benchmark.example.json). Keys that start
with "_" are comments. Unknown keys and bad values are errors, so a typo cannot go unnoticed.
`input_dir` and `output_dir` may contain {year}.
"""

from __future__ import annotations

import json
from pathlib import Path

SCALES = ["tract", "district", "municipality", "state"]
KINDS = ["scatter", "hexbin", "histogram"]
FORMATS = ["jpeg", "png", "pdf", "svg"]

DEFAULTS = {
    "input_dir": None,                 # required: the Stage 4 output folder (tables, metrics, report)
    "year": None,                      # required: census year of the tables
    "output_dir": None,                # null = <input_dir>/plots
    "version": "",                     # fills {version} in the labels (the [project] version of the pipeline)
    "kinds": ["scatter"],              # scatter = one dot per unit; hexbin = tracts per hexagon (national runs);
                                       # histogram = distribution of the error (estimate - census)
    "scales": ["tract", "district", "municipality"],
    "mode": "light",                   # or "dark"
    "dpi": 150,
    "format": "jpeg",                  # file format of the figures: jpeg (.jpg), png, pdf or svg
    "style": {},                       # overlay of the defaults of tools/plot_style.py (fonts, sizes, colours ...)
    "accent": "brazilpop",             # the product drawn in the accent colour; the others are gray
    "cmap": "plasma_r",                # hexbin colour map (any matplotlib name; _r reverses). plasma_r: blue = densest
    "cmap_min": 0.0,                   # the part of the colour map that is used, as fractions 0..1 of it:
    "cmap_max": 1.0,                   # e.g. gnuplot with cmap_max 0.85 drops the bright yellow end
    # Products to chart, in order: {"key": {"label": "...", "min_scale": "municipality"}}. Both fields
    # are optional. A product listed here with no column in the table is drawn as a "no data" panel.
    # null = every pi_sum_<key>_<year> column of the tract table, labelled from the Stage 4 report.
    "products": None,
    "table_pattern": "benchmark_{year}_{scale}.csv",
    "metrics_pattern": "benchmark_{year}_metrics.csv",
    "report_pattern": "benchmark_{year}_report.json",
}


class PlotConfigError(Exception):
    pass


def validate(given: dict, where: str = "") -> dict:
    """Merge `given` over the defaults and check it. Accepts a dict as well as a parsed file."""
    for key in given:
        if not key.startswith("_") and key not in DEFAULTS:
            raise PlotConfigError(f"unknown key {where}{key!r} (allowed: {', '.join(DEFAULTS)})")
    cfg = {k: given.get(k, v) for k, v in DEFAULTS.items()}
    if not cfg["input_dir"]:
        raise PlotConfigError("input_dir is required")
    if not isinstance(cfg["year"], int) or isinstance(cfg["year"], bool):
        raise PlotConfigError("year is required and must be an integer")
    for key in ("kinds", "scales"):
        allowed = KINDS if key == "kinds" else SCALES
        if not isinstance(cfg[key], list) or not cfg[key] or any(v not in allowed for v in cfg[key]):
            raise PlotConfigError(f"{key} must be a non-empty list of: {', '.join(allowed)}")
    if cfg["mode"] not in ("light", "dark"):
        raise PlotConfigError("mode must be \"light\" or \"dark\"")
    if not isinstance(cfg["dpi"], int) or not 30 <= cfg["dpi"] <= 600:
        raise PlotConfigError("dpi must be an integer from 30 to 600")
    if cfg["format"] not in FORMATS:
        raise PlotConfigError(f"format must be one of: {', '.join(FORMATS)}")
    if not isinstance(cfg["style"], dict):
        raise PlotConfigError("style must be an object")
    from tools.plot_style import StyleError, merge
    try:
        merge(cfg["style"])
    except StyleError as exc:
        raise PlotConfigError(str(exc)) from None
    if not isinstance(cfg["cmap"], str) or not cfg["cmap"]:
        raise PlotConfigError("cmap must be the name of a matplotlib colour map")
    try:
        from matplotlib import colormaps
    except ImportError:               # the tool itself needs matplotlib; the loader does not
        colormaps = None
    if colormaps is not None and cfg["cmap"] not in colormaps:
        raise PlotConfigError(f"cmap {cfg['cmap']!r} is not a matplotlib colour map")
    lo, hi = cfg["cmap_min"], cfg["cmap_max"]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (lo, hi)) or not 0 <= lo < hi <= 1:
        raise PlotConfigError("cmap_min and cmap_max must be numbers with 0 <= cmap_min < cmap_max <= 1")
    prods = cfg["products"]
    if prods is not None:
        if not isinstance(prods, dict) or not prods:
            raise PlotConfigError("products must be null or a non-empty object keyed by product")
        norm = {}
        for key, spec in prods.items():
            if key.startswith("_"):
                continue
            spec = {} if spec is None else spec
            if not isinstance(spec, dict) or set(spec) - {"label", "min_scale"}:
                raise PlotConfigError(f"products.{key} takes only label and min_scale")
            if spec.get("min_scale", "tract") not in SCALES:
                raise PlotConfigError(f"products.{key}.min_scale must be one of: {', '.join(SCALES)}")
            norm[key] = spec
        cfg["products"] = norm
    return cfg


def load(path: str | Path) -> dict:
    path = Path(path)
    try:
        return validate(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise PlotConfigError(f"config file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise PlotConfigError(f"{path} is not valid JSON: {exc}") from None
