"""Quick-look charts of the Stage 4 results.

    .venv/bin/python tools/plot_benchmark.py --config config.local.toml --year 2022

Reads what Stage 4 wrote (the tract and metrics tables) and writes two PNGs:

  1. scatter_{scale}_truth_vs_pred_{year}.png  (or hexbin_..., with --kind hexbin), for the
        scales of --scales (default tract, district, municipality; a single unit is one dot)
        census residents against the estimate, one panel per product, tracts; dots adapt
        their size and opacity to the number of tracts, hexbin counts tracts per hexagon
  2. bars_metrics_by_scale_{year}.png
        MAPE, MAE, RMSE and bias, one panel per metric (units differ, so no shared
        axis), horizontal bars grouped by scale with one bar per product

Colours: the product under evaluation (BrazilPop, `--accent`) is the accent blue and every
benchmark is gray (light gray bars, dark gray dots and hexagons). Panels have the seaborn
darkgrid tone with a white grid. The hexbin density ramps start at a very light tone that
still differs from the panel. Axes, ticks and tick
labels all use the colour of the axis labels. Every bar and panel is named, so identity never rests on colour.
Bars are square-cornered and there is no legend. Product labels
come from the config (`label`, with {version} and {year} placeholders, e.g. `BrazilPop V6 2022`).
Fonts: Helvetica if installed (else Nimbus Sans / Arial / Liberation Sans); ticks and the values
at the bars are 8 pt, everything else 10 pt. Surfaces and ink are the reference data-viz palette.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, LogNorm, to_rgb  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

SCALES = ["tract", "district", "municipality", "state"]
# Emphasis form: the product under evaluation (BrazilPop) is the accent blue, every benchmark
# is the same dark gray. Identity is not carried by colour, so each bar is named under it.
ACCENT_KEY = "brazilpop"
ACCENT = {"light": "#2a78d6", "dark": "#3987e5"}
NEUTRAL = {"light": "#52514e", "dark": "#898781"}        # scatter points
BAR_NEUTRAL = {"light": "#a9a8a4", "dark": "#a8a7a0"}    # benchmark bars: a light gray
PANEL = {"light": "#eaeaf2", "dark": "#2a2a35"}          # seaborn "darkgrid" tone behind every panel
SURFACE = {"light": "#fcfcfb", "dark": "#1a1a19"}
INK = {  # primary, secondary, muted
    "light": ("#0b0b0b", "#2e2d2b", "#6d6c68"),     # labels, axes and ticks use the second tone: strong gray
    "dark": ("#ffffff", "#e3e2da", "#a8a7a0"),
}
GRID = {"light": "#ffffff", "dark": "#3b3b49"}          # white grid on the seaborn tone, as in seaborn darkgrid
BASELINE = {"light": "#c3c2b7", "dark": "#383835"}
FONT_CANDIDATES = ["Helvetica", "Helvetica Neue", "Nimbus Sans", "Arial", "Liberation Sans", "DejaVu Sans"]
TICK_PT = 8        # tick labels and the values written at the bars
TEXT_PT = 10       # everything else: titles, axis labels, legend, group labels, notes
NOTE_PT = 8        # the figures written under each scatter / hexbin panel
FIG_SCALE = 0.75   # page-sized figures: 25% smaller than the first layout, fonts keep their point sizes
SCATTER_SCALE = 0.8   # the scatter / hexbin figure is a further 20% smaller (fonts unchanged)
PAD_IN = 0.04      # outer padding, inches (matplotlib default is 0.04); gaps between panels below
GAP = 0.025        # fraction of the figure between panels (default 0.02 of axes; kept tight)


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
EXPECTED_PRODUCTS = ["hrsl"]   # always on the charts: drawn as a null bar / null panel while it has no data


def label_of(key: str, labels: dict | None = None) -> str:
    """Display name: the config `label` (from the Stage 4 report), then a built-in name, then the key."""
    return (labels or {}).get(key) or PRODUCT_LABEL.get(key, key)


def expand_label(text: str, version: str, year: int) -> str:
    """`BrazilPop V{version} {year}` -> `BrazilPop V6 2022`. Labels without placeholders are unchanged."""
    return text.replace("{version}", str(version)).replace("{year}", str(year))


BAR_RATIO = 0.68   # bar thickness as a fraction of its row (20% thinner than the previous 0.85)
GROUP_GAP = 0.8    # empty space between two groups, in rows
ROW_IN = 0.26      # height of one row in inches (before FIG_SCALE)
MIN_RAMP_DE = 10.0  # the first colour of a density ramp must differ from the panel by at least this (OKLab x100)


def bar_colors(products: list[str], accent: str, mode: str = "light") -> dict[str, str]:
    """Bar chart colours: the accent product keeps the accent blue, every benchmark is the light gray."""
    return {k: ACCENT[mode] if k == accent else BAR_NEUTRAL[mode] for k in products}


def _oklab(rgb) -> np.ndarray:
    c = np.asarray(rgb, dtype=float)
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    lms = np.cbrt(np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                            [0.2119034982, 0.6806995451, 0.1073969566],
                            [0.0883024619, 0.2817188376, 0.6299787005]]) @ lin)
    return np.array([[0.2104542553, 0.7936177850, -0.0040720468],
                     [1.9779984951, -2.4285922050, 0.4505937099],
                     [0.0259040371, 0.7827717662, -0.8086757660]]) @ lms


def color_of(key: str, mode: str = "light", accent: str = ACCENT_KEY, neutral: dict | None = None) -> str:
    """Accent blue for the product under evaluation, gray for every benchmark."""
    return ACCENT[mode] if key == accent else (neutral or NEUTRAL)[mode]


def short(v: float, pct: bool = False) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "n/a"
    if pct:
        return f"{v:.1f}%"
    a = abs(v)
    return f"{v / 1000:.1f}k" if a >= 10_000 else f"{v:,.0f}" if a >= 100 else f"{v:.1f}"


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


def _compact(v: float, _=None) -> str:
    """0, 500, 2,000 ... 50k, 1.5M: keeps the ticks of a district or state axis short."""
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:g}M"
    if a >= 1e5:
        return f"{v / 1e3:g}k"
    return f"{v:,.0f}"


def _thousands(ax, axis="y") -> None:
    f = FuncFormatter(_compact)
    (ax.yaxis if axis == "y" else ax.xaxis).set_major_formatter(f)


# ------------------------------------------------------------------------------ scatter

def _window(tract: pd.DataFrame, products: list[str]) -> float:
    """Upper axis limit: the bulk of the data (99.5th percentile of census and estimates), rounded up."""
    pooled = pd.concat([tract["p_k"]] + [tract[f"pi_sum_{k}"] for k in products]).dropna()
    q = float(pooled.quantile(0.995)) * 1.05
    if q >= 100_000:                                   # districts, municipalities, states: half a power of ten
        step = 10 ** int(np.floor(np.log10(q))) / 2
    else:
        step = 100 if q < 2000 else 500 if q < 10000 else 1000
    return float(np.ceil(q / step) * step)


def _density_ramp(is_accent: bool, mode: str) -> LinearSegmentedColormap:
    """Hexbin colours, few tracts to many: one hue from a very light tone to the full colour (the
    accent blue for the product under evaluation, a dark gray for the benchmarks). The first tone
    is the lightest blend that still differs from the panel by MIN_RAMP_DE, so a single tract is
    visible and never melts into the background."""
    bg = np.array(to_rgb(PANEL[mode]))
    fg = np.array(to_rgb(ACCENT[mode] if is_accent else (NEUTRAL["light"] if mode == "light" else BAR_NEUTRAL["dark"])))
    t = next((t for t in np.arange(0.05, 1.0, 0.01)
              if 100 * np.linalg.norm(_oklab(bg + (fg - bg) * t) - _oklab(bg)) >= MIN_RAMP_DE), 0.3)
    return LinearSegmentedColormap.from_list("density", [tuple(bg + (fg - bg) * t), tuple(fg)])


def _point_style(n: int, accent_product: bool) -> tuple[float, float]:
    """(marker size in points^2, alpha) for n points: smaller and fainter as the cloud gets denser,
    so a national run (hundreds of thousands of tracts) stays readable instead of one solid blob."""
    if n <= 30:                                        # a handful of units (a state, a municipality): visible dots
        return 36.0, 0.9
    scale = min(1.0, (300.0 / max(n, 1)) ** 0.5)
    alpha = float(np.clip(0.35 * scale * (1.0 if accent_product else 0.9), 0.012, 0.35))
    size = float(np.clip(7.0 * scale ** 0.5 * 1.4, 1.2, 7.0))
    return size, alpha


def _null_scatter_panel(ax, fig, key, hi, kind, mode, ink, labels, scale="tract",
                        reason="no data") -> None:
    """A panel with the same frame as the others and no data, so the row keeps its place."""
    ax.plot([0, hi], [0, hi], color=ink[2], linewidth=1.0, zorder=3)
    ax.set_xlim(0, hi)
    ax.set_ylim(0, hi)
    ax.set_aspect("equal", adjustable="box")
    _thousands(ax, "x")
    _thousands(ax, "y")
    ax.set_ylabel("Raster Residents")
    ax.set_xlabel(f"Census Residents ({scale.capitalize()} level)")
    ax.set_title(f"{label_of(key, labels)}  ·  {reason}", loc="left", fontsize=TEXT_PT, fontweight="bold",
                 color=ink[0])
    ax.text(0.5, 0.5, reason, transform=ax.transAxes, ha="center", va="center", fontsize=TEXT_PT,
            color=ink[2])
    if kind == "hexbin":                      # an invisible colour bar keeps the panel as wide as the others
        from matplotlib.cm import ScalarMappable
        from matplotlib.colors import ListedColormap
        cb = fig.colorbar(ScalarMappable(norm=LogNorm(vmin=1, vmax=10),
                                         cmap=ListedColormap([SURFACE[mode]])), ax=ax, shrink=0.55, pad=0.02,
                          aspect=22)
        cb.set_ticks([])
        cb.ax.minorticks_off()
        cb.ax.tick_params(which="both", length=0)
        cb.outline.set_visible(False)
        cb.set_label(" ", fontsize=TEXT_PT)


def scatter_truth_vs_pred(tract: pd.DataFrame, metrics: pd.DataFrame, products: list[str], out: Path,
                          title: str, mode: str = "light", dpi: int = 150, labels: dict | None = None,
                          accent: str = ACCENT_KEY, kind: str = "scatter", gridsize: int = 70,
                          null_products: list[str] | None = None, scale: str = "tract") -> Path:
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
    for a national run. Only points inside the axes are handed to the renderer.
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
    w, h = (6.5, 6.9) if kind == "hexbin" else (5.6, 7.0)          # hexbin panels also carry a colour bar
    fig, axes = plt.subplots(rows, cols, figsize=(w * cols * FIG_SCALE * SCATTER_SCALE,
                                                  h * rows * FIG_SCALE * SCATTER_SCALE),
                             squeeze=False, layout="constrained")
    _tighten(fig)
    fig.suptitle(title, x=0.01, ha="left", fontsize=TEXT_PT, fontweight="bold", color=ink[0])
    for extra in list(axes.flat)[n:]:
        extra.set_visible(False)

    for ax, key in zip(axes.flat, products):
        col = color_of(key, mode, accent)
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
            hb = ax.hexbin(x, y, gridsize=gridsize if len(x) > 300 else 22, extent=(0, hi, 0, hi), mincnt=1,
                           norm=LogNorm(vmin=1),
                           cmap=_density_ramp(key == accent, mode),
                           edgecolors="face", linewidths=0.3, zorder=2, rasterized=True)
            hb.set_clim(1, max(10.0, float(np.ma.max(hb.get_array()))))     # a lone unit: scale still reads 1 to 10
            cb = fig.colorbar(hb, ax=ax, shrink=0.55, pad=0.02, aspect=22)
            cb.set_label("tracts per hexagon", fontsize=TEXT_PT, color=ink[1])
            cb.ax.tick_params(labelsize=TICK_PT, color=ink[1], labelcolor=ink[1])
            cb.outline.set_edgecolor(ink[1])
        else:
            size, alpha = _point_style(len(x), key == accent)
            ax.plot(x, y, linestyle="", marker="o", markersize=size ** 0.5, markeredgewidth=0, color=col,
                    alpha=alpha, zorder=2, rasterized=True)
        ax.plot([0, hi], [0, hi], color=ink[2], linewidth=1.0, zorder=3)             # 1:1 reference
        ax.set_xlim(0, hi)
        ax.set_ylim(0, hi)
        ax.set_aspect("equal", adjustable="box")
        _thousands(ax, "x")
        _thousands(ax, "y")
        ax.set_ylabel("Raster Residents")

        m = metrics[(metrics["product"] == key) & (metrics["scale"] == scale)]
        res = m["resolution"].iloc[0] if len(m) else ""
        sub = "external raster" if res == "external" else f"{res} raster" if res else ""
        ax.set_title(f"{label_of(key, labels)}  ·  {sub}" if sub else label_of(key, labels), loc="left",
                     fontsize=TEXT_PT, fontweight="bold", color=ink[0])
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
        caption = [f"{int(stats['units']):,} {noun}  ·  MAE {short(stats['mae'])}  ·  "
                   f"MAPE {short(stats['mape'], True)}",
                   f"estimate / census {stats['ratio']:.3f}"
                   + (f"  ·  {len(outside)} beyond the axes" if len(outside) else "")]
        if len(err):
            w = d.loc[err.idxmax()]
            caption.append(f"largest error: {scale} {w['code']}")
            caption.append(f"census {w['p_k']:,.0f}, estimate {w[f'pi_sum_{key}']:,.0f}")
        # the figures sit under the panel, below the centred axis label, never over the data (a
        # national run fills the whole panel): small, light gray, flush left. The annotation is
        # narrower than the panel, so the layout reserves its height without squeezing the axes.
        ax.set_xlabel(f"Census Residents ({scale.capitalize()} level)")
        ax.annotate("\n".join(caption), xy=(0, 0), xycoords="axes fraction", xytext=(0, -33),
                    textcoords="offset points", ha="left", va="top", multialignment="left",
                    fontsize=NOTE_PT, color=ink[2], linespacing=1.5, annotation_clip=False)
        ax.text(hi * 0.985, hi * 0.93, "1:1", color=ink[2], fontsize=TICK_PT, ha="right", va="top", rotation=45,
                rotation_mode="anchor")

    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=dpi)
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------------- bars

PANELS = [  # column, title, unit, percentage?, signed?
    ("mape", "Mean absolute percentage error", "% error per unit", True, False),
    ("mae", "Mean absolute error", "residents per unit", False, False),
    ("rmse", "Root mean square error", "residents per unit", False, False),
    ("bias", "Bias (estimate − census)", "residents per unit", False, True),
]


def sci_label(v: float) -> str:
    """Scientific notation for an axis tick: 10^3, -10^4, 2x10^2 (mathtext)."""
    if v == 0:
        return "0"
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
                             figsize=((2.4 + 3.6 * len(PANELS)) * FIG_SCALE, (panel_h + 0.5) * FIG_SCALE))
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
                        fontsize=TICK_PT, color=ink[1])

    if footnote:
        import textwrap
        fig.supxlabel(textwrap.fill(footnote, 150), x=0.01, ha="left", fontsize=TEXT_PT, color=ink[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=dpi)
    plt.close(fig)
    return out


# ------------------------------------------------------------------------------------ CLI

def build(config: str, year: int, out_dir: Path | None, mode: str, dpi: int,
          accent: str = ACCENT_KEY, kind: str = "scatter",
          scales: tuple[str, ...] = ("tract", "district", "municipality")) -> list[Path]:
    from stages.config import load_config, output_dir
    from stages.stage_04_benchmark import products as cfg_products

    cfg = load_config(config)
    base = output_dir(cfg, "benchmarking", year)
    b = cfg["benchmarking"]
    tract = pd.read_csv(base / b.get("table_pattern", "benchmark_{year}_{scale}.csv").format(year=year, scale="tract"),
                        dtype={"code": str})
    metrics = pd.read_csv(base / b.get("metrics_pattern", "benchmark_{year}_metrics.csv").format(year=year))
    report_p = base / b.get("report_pattern", "benchmark_{year}_report.json").format(year=year)
    report = json.loads(report_p.read_text()) if report_p.is_file() else {}
    keys = [k for k in cfg_products(cfg, year) if f"pi_sum_{k}" in tract.columns]
    configured = list(cfg_products(cfg, year)) + EXPECTED_PRODUCTS
    nulls = [k for i, k in enumerate(configured) if k not in keys and k not in configured[:i]]
    if not keys:
        raise SystemExit(f"{config}: no [benchmarking.products.*] entry matches the columns of the Stage 4 "
                         f"table ({', '.join(c[7:] for c in tract.columns if c.startswith('pi_sum_')) or 'none'}). "
                         f"Use the config that produced the table, or add the products to this one.")
    version = str(cfg.get("project", {}).get("version", ""))
    labels = dict(report.get("labels", {}))
    for k, spec in cfg_products(cfg, year).items():          # the current config wins over an older report
        if spec.get("label"):
            labels[k] = spec["label"]
    labels = {k: expand_label(v, version, year) for k, v in labels.items()}
    all_keys = keys + nulls

    run_ufs = report.get("ufs", [])
    ufs = ("Brazil" if len(run_ufs) >= 27 else f"{len(run_ufs)} UFs" if len(run_ufs) > 6
           else ", ".join(run_ufs) or "selected UFs")        # a national title is not a list of 27 codes
    out = Path(out_dir) if out_dir else base / "plots"
    notes = []
    single = [s for s in SCALES if s in set(metrics["scale"])
              and int(metrics[metrics["scale"] == s]["units"].max()) == 1]
    if len(single) > 1:
        notes.append(f"{', '.join(x.capitalize() for x in single)} each hold a single unit in this run, "
                     f"so those groups describe the same unit")
    for k, spec in cfg_products(cfg, year).items():
        if k in keys and spec.get("min_scale", "tract") != "tract":
            notes.append(f"{label_of(k, labels)} is scored from the {spec['min_scale']} scale up")
    for k in nulls:
        notes.append(f"{label_of(k, labels)} has no data in this run")
    scatters = []
    for scale in scales:                  # one figure per scale; a state or a municipality may be one point
        path = base / b.get("table_pattern", "benchmark_{year}_{scale}.csv").format(year=year, scale=scale)
        if not path.is_file():
            print(f"skipped the {scale} scatter: {path} not found", file=sys.stderr)
            continue
        table = tract if scale == "tract" else pd.read_csv(path, dtype={"code": str})
        scatters.append(scatter_truth_vs_pred(
            table, metrics, all_keys, out / f"{kind}_{scale}_truth_vs_pred_{year}.png",
            f"{_plural(scale, 2).capitalize()}, {ufs} {year}: estimate against census", mode, dpi, labels,
            accent, kind, null_products=nulls, scale=scale))
    f2 = bars_metrics_by_scale(metrics, all_keys, out / f"bars_metrics_by_scale_{year}.png",
                               f"Accuracy by scale, {ufs} {year}", mode, dpi, footnote="; ".join(notes),
                               labels=labels, accent=accent, null_products=nulls)
    return scatters + [f2]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--out-dir", help="default: <benchmarking output_dir>/plots")
    ap.add_argument("--mode", choices=["light", "dark"], default="light")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--accent", default=ACCENT_KEY,
                    help="product drawn in the accent blue (default brazilpop); the others are gray")
    ap.add_argument("--scales", nargs="+", choices=SCALES, default=["tract", "district", "municipality"],
                    help="one tract-style scatter / hexbin figure per scale (default: tract district municipality)")
    ap.add_argument("--kind", choices=["scatter", "hexbin"], default="scatter",
                    help="tract chart: one dot per tract, or a hexagon density map (for national runs)")
    args = ap.parse_args(argv)
    for p in build(args.config, args.year, args.out_dir, args.mode, args.dpi, args.accent, args.kind,
                   tuple(args.scales)):
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
