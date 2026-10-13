"""Default style and plotting parameters of tools/plot_benchmark.py, in one place.

`DEFAULT_STYLE` holds every number and colour the charts use. The JSON file of the tool (or Stage 4)
may overlay any of them under the key "style": only the keys given change, everything else keeps
its default. Unknown keys and values of the wrong kind are errors, so a typo cannot go unnoticed.
Colours are given per mode ("light" / "dark"). Keys that start with "_" are comments.

    "style": {"tick_pt": 9, "hist_bins": 60, "colors": {"panel": {"light": "#f0f0f6"}}}
"""

from __future__ import annotations

import copy

DEFAULT_STYLE: dict = {
    # --- fonts, in points (the font family is the first installed name of `fonts`)
    "fonts": ["Helvetica", "Helvetica Neue", "Nimbus Sans", "Arial", "Liberation Sans", "DejaVu Sans"],
    "tick_pt": 8,            # tick labels
    "text_pt": 10,           # titles, axis labels, group labels, notes
    "note_pt": 8,            # the figures written under each scatter / hexbin / histogram panel
    "bar_value_pt": 7,       # the values written at the tips of the bars
    # --- layout
    "fig_scale": 0.75,       # page-sized figures (fonts keep their point sizes)
    "scatter_scale": 0.8,    # the scatter / hexbin / histogram figures are a further step smaller
    "pad_in": 0.04,          # outer padding, inches
    "gap": 0.025,            # fraction of the figure between panels
    "landscape_ratio": 1.5,  # every figure is grown on its short side to this width : height
    "bar_frame_scale": 0.65,  # the bar chart frame, as a fraction of the height the layout gives it
    "bar_ratio": 0.68,       # bar thickness as a fraction of its row
    "group_gap": 0.8,        # empty space between two groups of bars, in rows
    "row_in": 0.26,          # height of one bar row, inches (before fig_scale)
    # --- scatter / hexbin
    "one_to_one_alpha": 0.25,   # opacity of the 1:1 line (drawn over the data)
    "dot_size_scale": 2.5,      # tract-scale dots: marker area x this (the opacity rule is unchanged)
    "sci_from": 10000,          # populations from here on are written in scientific notation
    "hex_gridsize": 70,         # hexagons across (22 when there are 300 units or fewer)
    "hex_gridsize_small": 22,
    "hex_clip_pct": [5, 95],    # the colour scale is clipped at these percentiles of the hexagon counts
    "hex_floor": 10,            # hard rule: the bottom of the colour scale is never below this
    "hex_bar_shrink": 0.45,     # horizontal colour bar: length (fraction of the panel width)
    "hex_bar_pad": 0.24,        # top of the bar, below the panel, in panel heights (clears ticks and axis title)
    "hex_bar_aspect": 15,       # length / thickness
    # --- histogram
    "hist_bins": 100,           # the same number of bins in every panel and figure
    "hist_window_pct": [5, 95],  # the x window spans the widest of these percentile intervals of the panels
    # --- output
    "jpeg_quality": 95,         # when `format` is jpeg
    # --- colours, per mode
    "colors": {
        "accent": {"light": "#2a78d6", "dark": "#3987e5"},         # the product under evaluation (bars)
        "neutral": {"light": "#52514e", "dark": "#898781"},
        "bar_neutral": {"light": "#a9a8a4", "dark": "#a8a7a0"},    # benchmark bars and histogram bars
        "panel": {"light": "#f4f4f8", "dark": "#55555d"},          # background of every panel
        "surface": {"light": "#fcfcfb", "dark": "#1a1a19"},        # background of the figure
        "grid": {"light": "#ffffff", "dark": "#3b3b49"},
        "subtitle": {"light": "#4a4946", "dark": "#d0cfc8"},       # panel titles (bold)
        "ink": {"light": ["#0b0b0b", "#2e2d2b", "#6d6c68"],        # primary, secondary, muted
                "dark": ["#ffffff", "#e3e2da", "#a8a7a0"]},
    },
}

# style key -> module constant of tools/plot_benchmark.py
GLOBALS = {
    "fonts": "FONT_CANDIDATES", "tick_pt": "TICK_PT", "text_pt": "TEXT_PT", "note_pt": "NOTE_PT",
    "bar_value_pt": "BAR_VALUE_PT", "fig_scale": "FIG_SCALE", "scatter_scale": "SCATTER_SCALE",
    "pad_in": "PAD_IN", "gap": "GAP", "landscape_ratio": "LANDSCAPE_RATIO", "bar_frame_scale": "BAR_FRAME_SCALE",
    "bar_ratio": "BAR_RATIO", "group_gap": "GROUP_GAP", "row_in": "ROW_IN",
    "one_to_one_alpha": "ONE_TO_ONE_ALPHA", "dot_size_scale": "DOT_SIZE_SCALE", "sci_from": "SCI_FROM", "hex_gridsize": "HEX_GRIDSIZE",
    "hex_gridsize_small": "HEX_GRIDSIZE_SMALL", "hex_clip_pct": "HEX_CLIP_PCT", "hex_floor": "HEX_FLOOR",
    "hex_bar_shrink": "HEX_BAR_SHRINK", "hex_bar_pad": "HEX_BAR_PAD", "hex_bar_aspect": "HEX_BAR_ASPECT",
    "hist_bins": "HIST_BINS", "hist_window_pct": "HIST_WINDOW_PCT", "jpeg_quality": "JPEG_QUALITY",
}
COLOR_GLOBALS = {"accent": "ACCENT", "neutral": "NEUTRAL", "bar_neutral": "BAR_NEUTRAL", "panel": "PANEL",
                 "surface": "SURFACE", "grid": "GRID", "subtitle": "SUBTITLE", "ink": "INK"}


class StyleError(Exception):
    pass


def _is_color(v) -> bool:
    return isinstance(v, str) and len(v) in (4, 7) and v.startswith("#") and all(c in "0123456789abcdefABCDEF" for c in v[1:])


def merge(overrides: dict | None = None) -> dict:
    """DEFAULT_STYLE with `overrides` laid over it, checked. Returns a new dict."""
    st = copy.deepcopy(DEFAULT_STYLE)
    over = {k: v for k, v in (overrides or {}).items() if not k.startswith("_")}
    for key, value in over.items():
        if key not in st:
            raise StyleError(f"unknown style key {key!r} (allowed: {', '.join(st)})")
        if key == "colors":
            if not isinstance(value, dict):
                raise StyleError("style.colors must be an object")
            for name, modes in value.items():
                if name.startswith("_"):
                    continue
                if name not in st["colors"]:
                    raise StyleError(f"unknown colour {name!r} (allowed: {', '.join(st['colors'])})")
                if not isinstance(modes, dict) or set(modes) - {"light", "dark"}:
                    raise StyleError(f"style.colors.{name} takes only \"light\" and \"dark\"")
                for mode, c in modes.items():
                    ok = (isinstance(c, list) and len(c) == 3 and all(_is_color(x) for x in c)) \
                        if name == "ink" else _is_color(c)
                    if not ok:
                        raise StyleError(f"style.colors.{name}.{mode} must be "
                                         f"{'a list of three #rrggbb colours' if name == 'ink' else 'a #rrggbb colour'}")
                    st["colors"][name][mode] = c
            continue
        default = st[key]
        if key == "fonts":
            if not isinstance(value, list) or not value or not all(isinstance(x, str) for x in value):
                raise StyleError("style.fonts must be a list of font names")
        elif isinstance(default, list):
            if not (isinstance(value, list) and len(value) == 2 and all(isinstance(x, (int, float)) for x in value)
                    and 0 <= value[0] < value[1] <= 100):
                raise StyleError(f"style.{key} must be [low, high] percentiles, 0 <= low < high <= 100")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise StyleError(f"style.{key} must be a positive number")
        elif isinstance(default, int) and not isinstance(default, bool) and key not in ("jpeg_quality",) \
                and not float(value).is_integer():
            raise StyleError(f"style.{key} must be an integer")
        if key == "jpeg_quality" and not 1 <= value <= 100:
            raise StyleError("style.jpeg_quality must be from 1 to 100")
        st[key] = value
    return st
