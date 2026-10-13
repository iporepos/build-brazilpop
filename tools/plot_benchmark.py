"""Quick-look charts of the Stage 4 results.

    .venv/bin/python tools/plot_benchmark.py --config tools/plot_benchmark.local.json

Reads what Stage 4 wrote (the tract and metrics tables) and writes the PNGs. Settings come from one
small JSON file (tools/plot_config.py; scales, kinds, mode, dpi, accent and the products and labels), not
from the pipeline TOML. Stage 4 passes the same settings itself.


  1. scatter_{scale}_truth_vs_pred_{year}.jpg  (or hexbin_..., kind "hexbin"), for the
        scales of `scales` (default tract, district, municipality; a single unit is one dot)
        census residents against the estimate, one panel per product, tracts; dots adapt
        their size and opacity to the number of tracts, hexbin counts tracts per hexagon
  2. bars_metrics_by_scale_{year}.jpg
        MAPE, MAE, RMSE and bias, one panel per metric (units differ, so no shared
        axis), horizontal bars grouped by scale with one bar per product

Colours: in the bar chart the product under evaluation (BrazilPop, `accent`) is the accent blue
and every benchmark is light gray. The scatter and hexbin panels are all gray (dark gray dots and
hexagons): the blue is only for the bars. Panels have the seaborn
darkgrid tone with a white grid. The hexbin density uses the plasma colour map, reversed (yellow
lowest, blue highest). Axes, ticks and tick
labels all use the colour of the axis labels. Every bar and panel is named, so identity never rests on colour.
Bars are square-cornered and there is no legend. Product labels
come from the config (`label`, with {version} and {year} placeholders, e.g. `BrazilPop V6 2022`).
Fonts: Helvetica if installed (else Nimbus Sans / Arial / Liberation Sans); ticks and the values
at the bars are 8 pt, everything else 10 pt. Surfaces and ink are the reference data-viz palette.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

log = logging.getLogger("plot_benchmark")

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LogNorm  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

SCALES = ["tract", "district", "municipality", "state"]
# The product under evaluation (BrazilPop) is the accent blue in the bars, every benchmark is gray.
ACCENT_KEY = "brazilpop"
FORMAT_EXT = {"jpeg": "jpg", "png": "png", "pdf": "pdf", "svg": "svg"}   # `format` of the settings -> extension
DEFAULT_CMAP = "plasma_r"      # hexbin colour map: blue is the highest density, yellow the lowest


def apply_style(overrides: dict | None = None) -> dict:
    """Set the style constants of this module: tools/plot_style.py DEFAULT_STYLE with `overrides`
    (the "style" key of the tool's JSON) laid over it. Called by build() every time, so a style never
    leaks from one call to the next. Returns the merged style."""
    from tools.plot_style import COLOR_GLOBALS, GLOBALS, merge

    st = merge(overrides)
    g = globals()
    for key, name in GLOBALS.items():
        g[name] = tuple(st[key]) if isinstance(st[key], list) and key != "fonts" else st[key]
    for key, name in COLOR_GLOBALS.items():
        g[name] = {m: (tuple(v) if isinstance(v, list) else v) for m, v in st["colors"][key].items()}
    return st


apply_style()


def _clipped_cmap(name: str, low: float = 0.0, high: float = 1.0):
    """The colour map `name` restricted to the fractions [low, high] of it, rescaled to the full range."""
    cmap = plt.get_cmap(name)
    if low <= 0.0 and high >= 1.0:
        return cmap
    from matplotlib.colors import ListedColormap
    return ListedColormap(cmap(np.linspace(low, high, 256)), name=f"{name}_{low:g}_{high:g}")


def _save(fig, out: Path, dpi: int) -> None:
    """Write the figure in the format of the file extension (jpeg quality: style.jpeg_quality)."""
    if out.suffix.lower() in (".jpg", ".jpeg"):
        fig.savefig(out, dpi=dpi, format="jpeg", pil_kwargs={"quality": int(JPEG_QUALITY)})
    else:
        fig.savefig(out, dpi=dpi)


def _landscape(width: float, height: float) -> tuple[float, float]:
    """Figure size at the landscape ratio LANDSCAPE_RATIO (3:2): the layout's natural size is grown
    on its short side, never shrunk, so the panels and the fonts keep their size."""
    return (max(width, height * LANDSCAPE_RATIO), max(height, width / LANDSCAPE_RATIO))


def _tighten(fig) -> None:
    """Paper style: small outer margin and small gaps between the panels."""
    fig.get_layout_engine().set(w_pad=PAD_IN, h_pad=PAD_IN, wspace=GAP, hspace=GAP)


def _pick_font() -> str:
    """Helvetica when installed, else its metric-compatible clones (Nimbus Sans, Arial, Liberation Sans)."""
    from matplotlib import font_manager
    for name in FONT_CANDIDATES:
        try:
            font_manager.findfont(name, fallback_to_default=False)
            return name
        except ValueError:
            continue
    return "sans-serif"
PRODUCT_LABEL = {"brazilpop": "BrazilPop", "landscan": "LandScan", "worldpop": "WorldPop", "ghsl": "GHSL",
                 "hrsl": "HRSL V1 2018"}


def label_of(key: str, labels: dict | None = None) -> str:
    """Display name: the config `label` (from the Stage 4 report), then a built-in name, then the key."""
    return (labels or {}).get(key) or PRODUCT_LABEL.get(key, key)


def order_products(keys: list[str], labels: dict | None = None, accent: str = ACCENT_KEY) -> list[str]:
    """Standard panel order: the product under evaluation (accent) first, then the others
    alphabetically by the name shown in the charts."""
    return sorted(keys, key=lambda k: (k != accent, label_of(k, labels).casefold(), k))


def expand_label(text: str, version: str, year: int) -> str:
    """`BrazilPop V{version} {year}` -> `BrazilPop V6 2022`. Labels without placeholders are unchanged."""
    return text.replace("{version}", str(version)).replace("{year}", str(year))




def bar_colors(products: list[str], accent: str, mode: str = "light") -> dict[str, str]:
    """Bar chart colours: the accent product keeps the accent blue, every benchmark is the light gray."""
    return {k: ACCENT[mode] if k == accent else BAR_NEUTRAL[mode] for k in products}


def color_of(key: str, mode: str = "light", accent: str = ACCENT_KEY, neutral: dict | None = None) -> str:
    """Accent blue for the product under evaluation, gray for every benchmark."""
    return ACCENT[mode] if key == accent else (neutral or NEUTRAL)[mode]


def thousand_label(v: float, sig: int | None = None) -> str:
    """Scientific notation anchored at 10^3: 12,500 -> 12.5 x 10^3, 4,000,000 -> 4,000 x 10^3. The
    exponent never runs, so labels on one axis are comparable at a glance. `sig` = significant digits
    of the mantissa (None: exact)."""
    m = v / 1000.0
    if sig:
        d = max(0, sig - 1 - int(np.floor(np.log10(abs(m)))))
        text = f"{m:,.{d}f}"
        text = text.rstrip("0").rstrip(".") if "." in text else text
    else:
        text = f"{m:,g}"
    return f"{text} × 10$^{{3}}$"


def short(v: float, pct: bool = False, sci: bool = True) -> str:
    """Compact figure for a bar tip or a caption. From SCI_FROM up (`sci`) it is written as a power of ten
    with an integer mantissa and no spaces, the exponent running: 12,709 -> 1x10^4, 434,102 -> 4x10^5."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "n/a"
    if pct:
        return f"{v:.1f}%"
    a = abs(v)
    if sci and a >= SCI_FROM:
        e = int(np.floor(np.log10(a)))
        m = int(round(a / 10 ** e))
        if m == 10:
            m, e = 1, e + 1
        sign = "-" if v < 0 else ""
        return f"{sign}10$^{{{e}}}$" if m == 1 else f"{sign}{m}×10$^{{{e}}}$"
    return f"{v:,.0f}" if a >= 100 else f"{v:.1f}"


def _style(mode: str) -> None:
    ink = INK[mode]
    plt.rcParams.update({
        "font.family": "sans-serif", "font.sans-serif": [_pick_font()], "font.size": TEXT_PT,
        "axes.labelsize": TEXT_PT, "axes.titlesize": TEXT_PT, "xtick.labelsize": TICK_PT, "ytick.labelsize": TICK_PT,
        "legend.fontsize": TEXT_PT,
        "mathtext.default": "regular",
        "figure.facecolor": SURFACE[mode], "axes.facecolor": SURFACE[mode], "savefig.facecolor": SURFACE[mode],
        "axes.edgecolor": ink[1], "axes.labelcolor": ink[1], "text.color": ink[0],       # axis, ticks, tick labels
        "xtick.color": ink[1], "ytick.color": ink[1], "xtick.labelcolor": ink[1], "ytick.labelcolor": ink[1],
        "axes.linewidth": 1.0,
        "axes.spines.top": False, "axes.spines.right": False,
        "grid.color": GRID[mode], "grid.linewidth": 1.0, "grid.linestyle": "-",
    })


def _plural(scale: str, n: int) -> str:
    word = {"municipality": "municipalit"}.get(scale, scale)
    return word + (("y" if scale == "municipality" else "") if n == 1 else ("ies" if scale == "municipality" else "s"))


def pop_label(v: float, _=None) -> str:
    """A population label: plain below 10,000 (so tract-level labels read as numbers), from there
    in scientific notation anchored at 10^3 (12,500 -> 12.5 x 10^3)."""
    return thousand_label(v) if abs(v) >= SCI_FROM else f"{v:,.0f}"


def _five_ticks(ax, hi: float) -> None:
    """Five ticks and grid lines per axis, no clutter: the two edges and three in between."""
    ticks = np.linspace(0, hi, 5)
    ax.set_xticks(ticks)
    ax.set_yticks(ticks)
    if hi >= SCI_FROM:       # scientific notation anchored at 10^3: the ticks hold the thousands, the
        f = FuncFormatter(lambda v, _: f"{v / 1000:,g}")          # multiplier is written once, in the labels
        unit = " (× 10$^{3}$)"
    else:
        f, unit = FuncFormatter(lambda v, _: f"{v:,.0f}"), ""
    ax.xaxis.set_major_formatter(f)
    ax.yaxis.set_major_formatter(f)
    ax.set_xlabel("Census Population" + unit)
    ax.set_ylabel("Estimated Population" + unit)


# ------------------------------------------------------------------------------ scatter

def _window(tract: pd.DataFrame, products: list[str]) -> float:
    """Upper axis limit: the bulk of the data (99.5th percentile of census and estimates), rounded up
    so that each of the four steps between the five ticks is a round number (1, 2, 2.5 or 5
    times a power of ten)."""
    pooled = pd.concat([tract["p_k"]] + [tract[f"pi_sum_{k}"] for k in products]).dropna()
    quarter = max(float(pooled.quantile(0.995)) * 1.05, 1.0) / 4
    e = 10.0 ** np.floor(np.log10(quarter))
    step = next(m * e for m in (1, 2, 2.5, 5, 10) if m * e >= quarter * (1 - 1e-9))
    return float(4 * step)


def hex_bounds(counts: np.ndarray) -> tuple[float, float]:
    """(low, high) of the shared hexbin colour scale: the HEX_CLIP_PCT percentiles (5th, 95th) of the
    hexagon counts, each snapped to the nearest power of ten (10, 10^2 ...), at least one decade
    apart. Hard rule: the bottom is never below HEX_FLOOR (10), so hexagons with fewer units all share
    the lowest colour. Hexagons outside the range take the end colours, so a few extreme cells at
    either end do not wash out the rest. Feed it the counts of EVERY panel for one composite scale."""
    counts = np.asarray(counts, dtype=float)
    counts = counts[np.isfinite(counts) & (counts > 0)]
    if not counts.size:
        return HEX_FLOOR, HEX_FLOOR * 10.0
    snap = lambda p: 10.0 ** round(np.log10(max(float(p), 1.0)))      # noqa: E731
    lo = max(snap(np.percentile(counts, HEX_CLIP_PCT[0])), HEX_FLOOR)
    hi = snap(np.percentile(counts, HEX_CLIP_PCT[1]))
    return lo, max(hi, lo * 10.0)


def hex_counts(tract: pd.DataFrame, products: list[str], gridsize: int | None = None) -> np.ndarray:
    """Tracts per hexagon of every product panel of one figure, pooled (same binning as the panels)."""
    keys = [k for k in products if f"pi_sum_{k}" in tract.columns]
    if not keys:
        return np.array([])
    hi = _window(tract, keys)
    fig, ax = plt.subplots()
    out = []
    try:
        for k in keys:
            d = tract[tract["p_k"].notna() & tract[f"pi_sum_{k}"].notna()]
            x, y = d["p_k"].to_numpy(), d[f"pi_sum_{k}"].to_numpy()
            inside = (x <= hi) & (y <= hi)
            if inside.any():
                hb = ax.hexbin(x[inside], y[inside], gridsize=(gridsize or HEX_GRIDSIZE) if inside.sum() > 300 else HEX_GRIDSIZE_SMALL,
                               extent=(0, hi, 0, hi), mincnt=1)
                out.append(np.ma.compressed(hb.get_array()))
    finally:
        plt.close(fig)
    return np.concatenate(out) if out else np.array([])


def _point_style(n: int, accent_product: bool) -> tuple[float, float]:
    """(marker size in points^2, alpha) for n points: smaller and fainter as the cloud gets denser,
    so a national run (hundreds of thousands of tracts) stays readable instead of one solid blob."""
    if n <= 30:                                        # a handful of units (a state, a municipality): visible dots
        return 36.0, 0.9
    scale = min(1.0, (300.0 / max(n, 1)) ** 0.5)
    alpha = float(np.clip(0.35 * scale * (1.0 if accent_product else 0.9), 0.012, 0.35))
    size = float(np.clip(7.0 * scale ** 0.5 * 1.4, 1.2, 7.0)) * DOT_SIZE_SCALE     # alpha rule untouched
    return size, alpha


def _hex_cax(ax):
    """Axes of the horizontal colour bar: HEX_BAR_SHRINK of the panel width, starting at its left edge,
    HEX_BAR_PAD (in panel heights) below the panel, thin by HEX_BAR_ASPECT. A child of the panel, so
    its position follows the panel and the layout reserves its room."""
    return ax.inset_axes([0.0, -HEX_BAR_PAD, HEX_BAR_SHRINK, HEX_BAR_SHRINK / HEX_BAR_ASPECT])


def _null_scatter_panel(ax, fig, key, hi, kind, mode, ink, labels, scale="tract",
                        reason="no data") -> None:
    """A panel with the same frame as the others and no data, so the row keeps its place."""
    ax.plot([0, hi], [0, hi], color=ink[2], linewidth=1.0, alpha=ONE_TO_ONE_ALPHA, zorder=3)
    ax.set_xlim(0, hi)
    ax.set_ylim(0, hi)
    ax.set_aspect("equal", adjustable="box")
    _five_ticks(ax, hi)
    ax.set_title(f"{label_of(key, labels)}  ·  {reason}", loc="left", fontsize=TEXT_PT, fontweight="bold",
                 color=SUBTITLE[mode])
    ax.text(0.5, 0.5, reason, transform=ax.transAxes, ha="center", va="center", fontsize=TEXT_PT,
            color=ink[2])
    if kind == "hexbin":                      # an invisible colour bar keeps the panel as tall as the others
        from matplotlib.cm import ScalarMappable
        from matplotlib.colors import ListedColormap
        cb = fig.colorbar(ScalarMappable(norm=LogNorm(vmin=1, vmax=10), cmap=ListedColormap([SURFACE[mode]])),
                          cax=_hex_cax(ax), orientation="horizontal")
        cb.set_ticks([1, 10])
        cb.ax.minorticks_off()
        cb.ax.tick_params(which="both", length=0, labelsize=TICK_PT, labelcolor=SURFACE[mode])
        cb.outline.set_visible(False)


def scatter_truth_vs_pred(tract: pd.DataFrame, metrics: pd.DataFrame, products: list[str], out: Path,
                          title: str, mode: str = "light", dpi: int = 150, labels: dict | None = None,
                          accent: str = ACCENT_KEY, kind: str = "scatter", gridsize: int | None = None,
                          null_products: list[str] | None = None, scale: str = "tract",
                          hex_range: tuple[float, float] | None = None, cmap: str = DEFAULT_CMAP,
                          cmap_range: tuple[float, float] = (0.0, 1.0)) -> Path:
    """One square panel per product, three across (5 products: 3 above, 2 below): census residents
    (x) against the estimate (y), one point per unit of `scale` (tract, district, municipality,
    state; the table passed in is the one of that scale). A product that is not scored at this
    scale gets an empty panel saying so. A handful of units, even one, is drawn as visible dots.

    A product listed in `null_products` has no data yet: its panel is drawn empty ("no data") so
    the figure keeps its place for it.

    The axes show the bulk of the tracts (same limits in every panel). Tracts beyond them are
    counted in the panel, and the one with the largest error is named.

    kind="scatter" draws one dot per tract (size and opacity adapt to the number of tracts);
    kind="hexbin" counts tracts per hexagon on a log colour scale, which is the one to use
    for a national run. Only points inside the axes are handed to the renderer. `hex_range` is
    its (low, high) colour scale; None takes it from this figure's own panels (hex_bounds). `cmap`
    is the matplotlib colour map of the hexagons and `cmap_range` the (low, high) fractions of it that are
    used, so a bright end can be left out.
    """
    if kind not in ("scatter", "hexbin"):
        raise ValueError(f"kind must be 'scatter' or 'hexbin', not {kind!r}")
    _style(mode)
    ink = INK[mode]
    nulls = set(null_products or [])
    n = len(products)
    hi = _window(tract, [k for k in products if k not in nulls])
    cols = min(n, 3)                                      # three across, then the rest on the next row
    rows = -(-n // cols)                                  # (5 products: 3 above, 2 below)
    w, h = (5.6, 8.3) if kind == "hexbin" else (5.6, 7.0)          # hexbin panels also carry a colour bar
    fig, axes = plt.subplots(rows, cols, figsize=_landscape(w * cols * FIG_SCALE * SCATTER_SCALE,
                                                            h * rows * FIG_SCALE * SCATTER_SCALE),
                             squeeze=False, layout="constrained")
    _tighten(fig)
    fig.suptitle(title, x=0.01, ha="left", fontsize=TEXT_PT, fontweight="bold", color=ink[0])
    for extra in list(axes.flat)[n:]:
        extra.set_visible(False)

    hexes = []
    for ax, key in zip(axes.flat, products):
        col = INK[mode][0]                      # scatter dots are black (white in dark mode); the accent blue is for the bars
        ax.set_facecolor(PANEL[mode])
        ax.set_axisbelow(True)
        ax.grid(True, linewidth=1.0)
        if key in nulls or f"pi_sum_{key}" not in tract.columns:
            _null_scatter_panel(ax, fig, key, hi, kind, mode, ink, labels, scale)
            continue
        d = tract[tract["p_k"].notna() & tract[f"pi_sum_{key}"].notna()]
        if d.empty:                                    # configured, but not scored at this scale
            _null_scatter_panel(ax, fig, key, hi, kind, mode, ink, labels, scale, "not scored at this scale")
            continue
        x, y = d["p_k"].to_numpy(), d[f"pi_sum_{key}"].to_numpy()
        inside = (x <= hi) & (y <= hi)                     # what is beyond the axes is counted, not drawn
        x, y = x[inside], y[inside]
        if kind == "hexbin":
            hb = ax.hexbin(x, y, gridsize=(gridsize or HEX_GRIDSIZE) if len(x) > 300 else HEX_GRIDSIZE_SMALL, extent=(0, hi, 0, hi), mincnt=1,
                           norm=LogNorm(vmin=1, vmax=10, clip=True),             # vmax is set once all panels are known
                           cmap=_clipped_cmap(cmap, *cmap_range),
                           edgecolors="face", linewidths=0.3, zorder=2, rasterized=True)
            hexes.append((ax, hb))
        else:
            size, alpha = _point_style(len(x), False)
            if scale != "tract":              # few units (districts, municipalities, states): opaque black dots
                alpha = 1.0
            ax.plot(x, y, linestyle="", marker="o", markersize=size ** 0.5, markeredgewidth=0, color=col,
                    alpha=alpha, zorder=2, rasterized=True)
        ax.plot([0, hi], [0, hi], color=ink[2], linewidth=1.0, alpha=ONE_TO_ONE_ALPHA, zorder=3)   # 1:1 reference, over the data
        ax.set_xlim(0, hi)
        ax.set_ylim(0, hi)
        ax.set_aspect("equal", adjustable="box")
        _five_ticks(ax, hi)

        m = metrics[(metrics["product"] == key) & (metrics["scale"] == scale)]
        res = m["resolution"].iloc[0] if len(m) else ""
        sub = "" if res == "external" else f"{res} raster" if res else ""     # a benchmark needs no remark
        ax.set_title(f"{label_of(key, labels)}  ·  {sub}" if sub else label_of(key, labels), loc="left",
                     fontsize=TEXT_PT, fontweight="bold", color=SUBTITLE[mode])
        # the same statistics as the metrics file; computed from the table when the product is
        # not scored at tract scale (its min_scale is higher), so every panel is readable
        e = d[f"pi_sum_{key}"] - d["p_k"]
        pos = d["p_k"] > 0
        stats = {"units": len(d), "mae": float(e.abs().mean()) if len(d) else np.nan,
                 "mape": float((e.abs()[pos] / d["p_k"][pos]).mean() * 100) if pos.any() else np.nan,
                 "ratio": float(d[f"pi_sum_{key}"].sum() / d["p_k"].sum()) if d["p_k"].sum() else np.nan}
        if len(m):
            stats.update({k: m.iloc[0][k] for k in ("units", "mae", "mape", "ratio")})
        outside = d[(d["p_k"] > hi) | (d[f"pi_sum_{key}"] > hi)]
        err = (d[f"pi_sum_{key}"] - d["p_k"]).abs()
        noun = _plural(scale, int(stats["units"]))
        caption = [f"{int(stats['units']):,} {noun}  ·  MAE {short(stats['mae'], sci=False)}  ·  "
                   f"MAPE {short(stats['mape'], True)}",
                   f"estimate / census {stats['ratio']:.3f}"
                   + (f"  ·  {len(outside)} beyond the axes" if len(outside) else "")]
        if len(err):
            w = d.loc[err.idxmax()]
            caption.append(f"largest error: {scale} {w['code']}")
            caption.append(f"census {w['p_k']:,.0f}, estimate {w[f'pi_sum_{key}']:,.0f}")
        # the figures sit under the panel, below the centred axis label (and, in a hexbin figure,
        # below the colour bar), never over the data: small, light gray, flush left. The annotation
        # is narrower than the panel, so the layout reserves its height without squeezing the axes.
        text = "\n".join(caption)
        if kind == "hexbin":
            hexes[-1] = (*hexes[-1], text)         # anchored to the colour bar, drawn once it exists
        else:
            ax.annotate(text, xy=(0, 0), xycoords="axes fraction", xytext=(0, -33),
                        textcoords="offset points", ha="left", va="top", multialignment="left",
                        fontsize=NOTE_PT, color=ink[2], linespacing=1.5, annotation_clip=False)
        ax.text(hi * 0.985, hi * 0.93, "1:1", color=ink[2], fontsize=TICK_PT, ha="right", va="top", rotation=45,
                rotation_mode="anchor")

    if hexes:         # one colour scale for every panel (and, from build(), for every figure)
        lo, top = hex_range or hex_bounds(np.concatenate([np.ma.compressed(hb.get_array()) for _, hb, _text in hexes]))
        ticks = [10.0 ** e for e in range(int(round(np.log10(lo))), int(round(np.log10(top))) + 1)]
        for ax, hb, text in hexes:
            hb.set_norm(LogNorm(vmin=lo, vmax=top, clip=True))
            # horizontal, below the x axis title and above the figures of the panel
            # the bar starts at the left edge of the axes (like the figures under it); the units
            # comment sits right after its end
            cb = fig.colorbar(hb, cax=_hex_cax(ax), orientation="horizontal", extend="both" if lo > 1 else "max")
            cb.set_ticks(ticks)
            cb.ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: sci_label(v)))
            cb.ax.minorticks_off()
            cb.ax.tick_params(labelsize=TICK_PT, color=ink[1], labelcolor=ink[1])
            cb.outline.set_edgecolor(ink[1])
            cb.ax.text(1.10, 0.5, f"{_plural(scale, 2).capitalize()} count", transform=cb.ax.transAxes,
                       ha="left", va="center", fontsize=TICK_PT, color=ink[1])      # units, next to the bar
            cb.ax.annotate(text, xy=(0, 0), xycoords=(ax, cb.ax), xytext=(0, -26),
                           textcoords="offset points", ha="left", va="top", multialignment="left",
                           fontsize=NOTE_PT, color=ink[2], linespacing=1.5, annotation_clip=False)

    out.parent.mkdir(parents=True, exist_ok=True)
    _save(fig, out, dpi)
    plt.close(fig)
    return out


# ------------------------------------------------------------------------- error histograms

def _nice_up(v: float, mantissas: tuple[float, ...]) -> float:
    """Smallest m x 10^k (m in `mantissas`) that is >= v."""
    v = max(float(v), 1e-9)
    e = 10.0 ** np.floor(np.log10(v))
    return float(next(m * e for m in mantissas if m * e >= v * (1 - 1e-9)))


def error_histograms(tract: pd.DataFrame, products: list[str], out: Path, title: str, mode: str = "light",
                     dpi: int = 150, labels: dict | None = None, null_products: list[str] | None = None,
                     scale: str = "tract") -> Path:
    """The scatter's layout, with the histogram of the error (estimate - census) of the units of
    `scale` in every panel. Made to be compared: the error axis is in the actual units (population),
    symmetric, the same in every panel: it spans the widest central 90% interval (5th to 95th
    percentile) of the panels, so the tails beyond it are only counted; there
    are HIST_BINS (100) equal bins over it, in every panel and every figure; the vertical axis is the
    share of the panel's units in each bin, in percent, with one limit for all panels. Bars are the
    gray of the bar chart."""
    _style(mode)
    ink = INK[mode]
    nulls = set(null_products or [])
    present = [k for k in products if k not in nulls and f"pi_sum_{k}" in tract.columns]
    errs, nunits = {}, {}
    for k in present:
        d = tract[tract["p_k"].notna() & tract[f"pi_sum_{k}"].notna()]
        errs[k] = (d[f"pi_sum_{k}"] - d["p_k"]).to_numpy(dtype=float)
        nunits[k] = len(d)
    pooled = np.concatenate([e for e in errs.values() if len(e)]) if errs else np.array([])
    if pooled.size == 0:
        raise ValueError("no unit with a census total and an estimate: nothing to draw")
    # the window holds the central 90% of the errors of the panel that needs the most room
    lo_q, hi_q = HIST_WINDOW_PCT[0] / 100, HIST_WINDOW_PCT[1] / 100
    reach = max(max(abs(float(np.quantile(e, lo_q))), abs(float(np.quantile(e, hi_q)))) for e in errs.values() if len(e))
    limit = _nice_up(reach * 1.05, (1, 2, 4, 6, 8, 10))
    edges = np.linspace(-limit, limit, HIST_BINS + 1)                    # the same 100 bins everywhere
    share = {k: 100.0 * np.histogram(e, bins=edges)[0] / max(len(e), 1) for k, e in errs.items()}
    top = max((float(v.max()) for v in share.values() if len(v)), default=1.0)
    ymax = 4 * _nice_up(top * 1.05 / 4, (1, 1.25, 2, 2.5, 5, 10))            # quarters are round numbers

    n = len(products)
    cols = min(n, 3)
    rows = -(-n // cols)
    fig, axes = plt.subplots(rows, cols, figsize=_landscape(5.6 * cols * FIG_SCALE * SCATTER_SCALE,
                                                            7.0 * rows * FIG_SCALE * SCATTER_SCALE),
                             squeeze=False, layout="constrained")
    _tighten(fig)
    fig.suptitle(title, x=0.01, ha="left", fontsize=TEXT_PT, fontweight="bold", color=ink[0])
    for extra in list(axes.flat)[n:]:
        extra.set_visible(False)

    thousands = limit >= SCI_FROM            # scientific notation anchored at 10^3, as on the scatter axes
    unit = " (× 10$^{3}$)" if thousands else ""
    fmt = FuncFormatter((lambda v, _: f"{v / 1000:,g}") if thousands else (lambda v, _: f"{v:,.0f}"))
    for ax, key in zip(axes.flat, products):
        ax.set_facecolor(PANEL[mode])
        ax.set_axisbelow(True)
        ax.grid(True, linewidth=1.0)
        ax.set_xlim(-limit, limit)
        ax.set_ylim(0, ymax)
        ax.set_box_aspect(2 / 3)                                          # the frame is 3:2 landscape
        ax.set_xticks(np.linspace(-limit, limit, 5))
        ax.set_yticks(np.linspace(0, ymax, 5))
        ax.xaxis.set_major_formatter(fmt)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        ax.set_xlabel("Error (estimate − census)" + unit)
        ax.set_ylabel("Share of units (%)")
        ax.set_title(label_of(key, labels), loc="left", fontsize=TEXT_PT, fontweight="bold", color=SUBTITLE[mode])
        if key not in errs or not len(errs[key]):
            ax.text(0.5, 0.5, "no data" if key in nulls or key not in errs else "not scored at this scale",
                    transform=ax.transAxes, ha="center", va="center", fontsize=TEXT_PT, color=ink[2])
            continue
        e = errs[key]
        ax.stairs(share[key], edges, fill=True, color=BAR_NEUTRAL[mode], zorder=2)
        beyond = int((np.abs(e) > limit).sum())
        caption = [f"{len(e):,} {_plural(scale, len(e))}  ·  MAE {short(float(np.abs(e).mean()), sci=False)}"
                   f"  ·  bias {float(e.mean()):+,.0f}",
                   f"median error {float(np.median(e)):+,.0f}" + (f"  ·  {beyond} beyond the axes" if beyond else "")]
        ax.annotate("\n".join(caption), xy=(0, 0), xycoords="axes fraction", xytext=(0, -33),
                    textcoords="offset points", ha="left", va="top", multialignment="left",
                    fontsize=NOTE_PT, color=ink[2], linespacing=1.5, annotation_clip=False)

    out.parent.mkdir(parents=True, exist_ok=True)
    _save(fig, out, dpi)
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------------- bars

PANELS = [  # column, title, unit, percentage?, signed?
    ("mape", "Mean absolute percentage error", "% error per unit", True, False),
    ("mae", "Mean absolute error", "Population per unit", False, False),
    ("rmse", "Root mean square error", "Population per unit", False, False),
    ("bias", "Bias (estimate − census)", "Population per unit", False, True),
]


def sci_label(v: float, sig: int | None = None) -> str:
    """Scientific notation: 10^3, -10^4, 2x10^2 (mathtext). `sig` limits the significant digits
    of the mantissa (1,234 -> 1.2x10^3 with sig=2); None keeps it exact (axis ticks)."""
    if v == 0:
        return "0"
    if sig:
        mant, exp = f"{abs(v):.{sig - 1}e}".split("e")
        m, e = float(mant), int(exp)
    else:
        e = int(np.floor(np.log10(abs(v)) + 1e-9))
        m = abs(v) / 10 ** e
    sign = "-" if v < 0 else ""
    if abs(m - 1) < 1e-6:
        return f"${sign}10^{{{e}}}$"
    return f"${sign}{m:g}\\times10^{{{e}}}$"


def _sci_formatter(v, _):
    return sci_label(v)


def bars_metrics_by_scale(metrics: pd.DataFrame, products: list[str], out: Path, title: str,
                          mode: str = "light", dpi: int = 150, footnote: str = "",
                          labels: dict | None = None, accent: str = ACCENT_KEY,
                          null_products: list[str] | None = None) -> Path:
    """Four panels side by side, one per metric (their units differ, so no shared value axis),
    horizontal bars. The rows are shared: the names are written once, at the left of the first panel.

    Scales are the groups, from tract (top) to state. Each group opens with its scale name, in
    bold on the line above the bar names (like a bar of zero length), then one bar per product,
    named at its left in plain horizontal text, with the value at the tip. The value
    axes are logarithmic (symmetric log for the signed bias) because the absolute errors
    grow about 400-fold from tract to state. BrazilPop is the accent blue and the benchmarks
    are light gray; every bar is named, so there is no legend.
    """
    _style(mode)
    ink = INK[mode]
    nulls = set(null_products or [])                      # no data yet: a name and "no data", no bar
    scales = [s for s in SCALES if s in set(metrics["scale"])]
    n = max(len(products), 1)
    per_group = (n + 1 + GROUP_GAP) * ROW_IN            # inches of height: scale label row + bars + gap
    panel_h = len(scales) * per_group + 1.25             # + title, value axis and its label
    fig, axes = plt.subplots(1, len(PANELS), sharey=True, layout="constrained", dpi=dpi,
                             figsize=_landscape((2.4 + 3.6 * len(PANELS)) * FIG_SCALE, (panel_h + 0.5) * FIG_SCALE))
    _tighten(fig)
    fig.suptitle(title, x=0.01, ha="left", fontsize=TEXT_PT, fontweight="bold", color=ink[0])

    def value(scale, key, col):
        row = metrics[(metrics["scale"] == scale) & (metrics["product"] == key)]
        if row.empty or pd.isna(row[col].iloc[0]):
            return None
        return float(row[col].iloc[0])

    pitch = 1.0 / (n + 1 + GROUP_GAP)                     # one row; a group is a label row + one row per bar
    bar_h = BAR_RATIO * pitch

    def rows(col=None):
        """({(group index, product): bar centre}, {group index: label centre}) in axis units. Each
        group starts with its scale name, set like a bar of zero length, then the bars present. The
        rows are the same in every panel (a product present for any metric), so they can be shared."""
        bars, heads = {}, {}
        for gi, scale in enumerate(scales):
            top = gi - 0.5
            heads[gi] = top + pitch / 2
            present = [k for k in products if k in nulls
                       or any(value(scale, k, c[0]) is not None for c in PANELS)]
            for j, key in enumerate(present):
                bars[(gi, key)] = top + pitch * (j + 1.5)
        return bars, heads

    for pi, (ax, (col, ttl, unit, pct, signed)) in enumerate(zip(axes.flat, PANELS)):
        vals = [v for sc in scales for k in products if (v := value(sc, k, col)) is not None]
        mags = [abs(v) for v in vals if v != 0]
        ax.set_facecolor(PANEL[mode])
        ax.set_axisbelow(True)
        ax.grid(True, axis="x", linewidth=1.0)
        ax.set_ylim(len(scales) - 1.5 + pitch * (n + 1.15), -0.5)      # first scale at the top, last bar hugs the axis
        top = 10 ** np.ceil(np.log10(max(mags)) + 0.6) if mags else 10.0
        if signed:
            ax.set_xscale("symlog", linthresh=1.0, linscale=0.6)
            ax.set_xlim(-top if any(v < 0 for v in vals) else 0, top if any(v > 0 for v in vals) else 1)
            ax.axvline(0, color=ink[1], linewidth=1.0, zorder=5)
        else:
            bottom = 10 ** np.floor(np.log10(min(mags)) - 0.35) if mags else 0.1
            ax.set_xscale("log")
            ax.set_xlim(bottom, top)
        if signed:      # both sides of zero double the number of decades: tick (and grid) every other one
            exps = range(2, int(round(np.log10(top))) + 1, 2)
            ticks = [0.0] + [10.0 ** e for e in exps if any(v > 0 for v in vals)] + \
                    [-(10.0 ** e) for e in exps if any(v < 0 for v in vals)]
            ax.set_xticks(sorted(ticks))
            ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: sci_label(v)))
        else:
            ax.xaxis.set_major_formatter(FuncFormatter(_sci_formatter))
        bars, heads = rows(col)
        ticks = sorted([(y, label_of(k, labels), False) for (_gi, k), y in bars.items()] +
                       [(y, scales[gi].capitalize(), True) for gi, y in heads.items()])
        ax.set_yticks([t[0] for t in ticks])
        ax.set_yticklabels([t[1] for t in ticks], fontsize=TICK_PT, color=ink[1])
        for lab, (_y, _t, is_head) in zip(ax.get_yticklabels(), ticks):
            if is_head:                                    # the scale: bold, same colour and size as the names
                lab.set_fontweight("bold")
        ax.tick_params(axis="y", length=0, pad=4, labelleft=(pi == 0))     # names once, on the first panel
        for sp in ax.spines.values():                      # the axis lines stay in front of the bars
            sp.set_zorder(10)
        ax.set_title(ttl, loc="left", fontsize=TEXT_PT, fontweight="bold", color=ink[0])
        ax.set_xlabel(unit, color=ink[1], fontsize=TEXT_PT)
    fig.canvas.draw()                                     # layout is final: pixels can now be measured

    colors = bar_colors(products, accent, mode)
    for ax, (col, _t, _u, pct, signed) in zip(axes.flat, PANELS):
        base = 0.0 if signed else ax.get_xlim()[0]
        for (gi, key), yc in rows(col)[0].items():
            if key not in nulls and value(scales[gi], key, col) is None:
                continue                                  # this metric is undefined for the unit
            if key in nulls:
                ax.annotate("no data", xy=(0.012, yc), xycoords=("axes fraction", "data"), ha="left",
                            va="center", fontsize=TICK_PT, color=ink[2])
                continue
            v = value(scales[gi], key, col)
            tip = max(v, base) if not signed else v
            if tip != base:                                   # square-cornered bars, anchored at the baseline
                ax.barh(yc, tip - base, left=base, height=bar_h, color=colors[key],
                        edgecolor="none", linewidth=0, zorder=3)
            right = v >= 0
            ax.annotate(short(v, pct), xy=(tip, yc), xytext=(4 if right else -4, 0),
                        textcoords="offset points", ha="left" if right else "right", va="center",
                        fontsize=BAR_VALUE_PT, color=ink[1])

    # the frame (the four panels, same height) is BAR_FRAME_SCALE of what the layout gave it
    fig.canvas.draw()
    box = axes[0].get_position()
    fw, fh = fig.get_size_inches()
    frame_aspect = (box.height * fh) / (box.width * fw)
    for ax in axes:
        ax.set_box_aspect(BAR_FRAME_SCALE * frame_aspect)
        ax.set_anchor("N")
    if footnote:
        import textwrap
        fig.supxlabel(textwrap.fill(footnote, 150), x=0.01, ha="left", fontsize=TEXT_PT, color=ink[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    _save(fig, out, dpi)
    plt.close(fig)
    return out


# ------------------------------------------------------------------------------------ CLI

def plain_columns(table: pd.DataFrame, epochs: dict | None = None) -> pd.DataFrame:
    """Stage 4 names its product columns `pi_sum_<product>_<year>`; the charts work with
    `pi_sum_<product>`. Tables from before the year was added are returned unchanged."""
    ren = {}
    for key, epoch in (epochs or {}).items():
        if f"pi_sum_{key}_{epoch}" in table.columns:
            ren[f"pi_sum_{key}_{epoch}"] = f"pi_sum_{key}"
    for c in table.columns:
        m = re.fullmatch(r"pi_sum_(.+)_(\d{4})", c)
        if m and c not in ren and f"pi_sum_{m[1]}" not in table.columns:
            ren[c] = f"pi_sum_{m[1]}"
    return table.rename(columns=ren)


def build(settings: dict) -> list[Path]:
    """Draw every chart of `settings` (tools/plot_config.py: a loaded JSON file or a dict built by
    Stage 4) from the Stage 4 tables. The bar chart is drawn once, a scatter per kind and scale."""
    from tools.plot_config import validate

    st = validate(settings)
    apply_style(st["style"])                 # defaults of tools/plot_style.py + the "style" of the settings
    ext = FORMAT_EXT[st["format"]]
    year, kinds, scales, mode, dpi, accent = st["year"], st["kinds"], st["scales"], st["mode"], st["dpi"], st["accent"]
    base = Path(st["input_dir"].replace("{year}", str(year)))
    pattern = {n: st[f"{n}_pattern"] for n in ("table", "metrics", "report")}
    log.info("reading the Stage 4 tables of %s from %s", year, base)
    report_p = base / pattern["report"].format(year=year)
    report = json.loads(report_p.read_text()) if report_p.is_file() else {}
    epochs = report.get("epochs", {})
    tract = plain_columns(pd.read_csv(base / pattern["table"].format(year=year, scale="tract"),
                                      dtype={"code": str}), epochs)
    metrics = pd.read_csv(base / pattern["metrics"].format(year=year)).rename(
        columns={"ratio_product_to_census": "ratio"})        # the charts' short name

    log.info("tract table: %d rows; metrics: %d rows", len(tract), len(metrics))
    spec = st["products"]
    if spec is None:                         # no list: what the table holds, named by the Stage 4 report
        spec = {c[len("pi_sum_"):]: {} for c in tract.columns if c.startswith("pi_sum_")}
    keys = [k for k in spec if f"pi_sum_{k}" in tract.columns]
    nulls = [k for k in spec if k not in keys]
    if not keys:
        have = ", ".join(c[7:] for c in tract.columns if c.startswith("pi_sum_")) or "none"
        raise SystemExit(f"no product of the config matches the columns of the Stage 4 table ({have}); "
                         f"check `products` and input_dir")
    labels = dict(report.get("labels", {}))
    for k, s in spec.items():                # the config wins over an older report
        if s.get("label"):
            labels[k] = s["label"]
    labels = {k: expand_label(v, st["version"], year) for k, v in labels.items()}
    all_keys = order_products(keys + nulls, labels, accent)      # the same order in every chart

    run_ufs = report.get("ufs", [])
    ufs = ("Brazil" if len(run_ufs) >= 27 else f"{len(run_ufs)} UFs" if len(run_ufs) > 6
           else ", ".join(run_ufs) or "selected UFs")        # a national title is not a list of 27 codes
    out = Path(st["output_dir"].replace("{year}", str(year))) if st["output_dir"] else base / "plots"
    notes = []
    single = [s for s in SCALES if s in set(metrics["scale"])
              and int(metrics[metrics["scale"] == s]["units"].max()) == 1]
    if len(single) > 1:
        notes.append(f"{', '.join(x.capitalize() for x in single)} each hold a single unit in this run, "
                     f"so those groups describe the same unit")
    for k in keys:
        if spec[k].get("min_scale", "tract") != "tract":
            notes.append(f"{label_of(k, labels)} is scored from the {spec[k]['min_scale']} scale up")
    for k in nulls:
        notes.append(f"{label_of(k, labels)} has no data in this run")
    written = []
    hex_range = None
    t_all = time.time()
    if "hexbin" in kinds:                 # one composite colour scale: the counts of every scale and product
        pooled = []
        for i, scale in enumerate(scales, 1):
            p = base / pattern["table"].format(year=year, scale=scale)
            if p.is_file():
                t0 = time.time()
                log.info("hexbin colour scale: counting the hexagons of the %s table (%d/%d) ...", scale, i, len(scales))
                tbl = tract if scale == "tract" else plain_columns(pd.read_csv(p, dtype={"code": str}), epochs)
                pooled.append(hex_counts(tbl, keys))
                log.info("  %s: %d hexagons (%.1fs)", scale, len(pooled[-1]), time.time() - t0)
        hex_range = hex_bounds(np.concatenate(pooled)) if pooled else None
        if hex_range:
            log.info("hexbin colour scale: %g to %g units per hexagon", *hex_range)
    total, done = len(kinds) * len(scales) + 1, 0

    def drawn(path: Path, t0: float) -> None:
        nonlocal done
        done += 1
        written.append(path)
        log.info("[%d/%d] %s (%.1fs)", done, total, path.name, time.time() - t0)

    for kind in kinds:
        for scale in scales:
            path = base / pattern["table"].format(year=year, scale=scale)
            if not path.is_file():            # one figure per scale; a state or a municipality may be one point
                log.warning("skipped the %s %s: %s not found", scale, kind, path)
                done += 1
                continue
            log.info("drawing the %s of the %s scale ...", kind, scale)
            t0 = time.time()
            table = tract if scale == "tract" else plain_columns(pd.read_csv(path, dtype={"code": str}), epochs)
            if kind == "histogram":
                drawn(error_histograms(
                    table, all_keys, out / f"histogram_{scale}_error_{year}.{ext}",
                    f"{_plural(scale, 2).capitalize()}, {ufs} {year}: error of the estimate", mode, dpi, labels,
                    null_products=nulls, scale=scale), t0)
                continue
            drawn(scatter_truth_vs_pred(
                table, metrics, all_keys, out / f"{kind}_{scale}_truth_vs_pred_{year}.{ext}",
                f"{_plural(scale, 2).capitalize()}, {ufs} {year}: estimate against census", mode, dpi, labels,
                accent, kind, null_products=nulls, scale=scale, hex_range=hex_range, cmap=st["cmap"],
                cmap_range=(st["cmap_min"], st["cmap_max"])), t0)
    log.info("drawing the accuracy bars ...")
    t0 = time.time()
    drawn(bars_metrics_by_scale(
        metrics, all_keys, out / f"bars_metrics_by_scale_{year}.{ext}", f"Accuracy by scale, {ufs} {year}",
        mode, dpi, footnote="; ".join(notes), labels=labels, accent=accent, null_products=nulls), t0)
    log.info("%d figures in %s (%.1fs)", len(written), out, time.time() - t_all)
    return written


def main(argv: list[str] | None = None) -> int:
    from tools.plot_config import PlotConfigError, load

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True, help="JSON settings (see tools/plot_benchmark.example.json)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        for p in build(load(args.config)):
            print(p)
    except PlotConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
