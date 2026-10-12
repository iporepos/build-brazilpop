"""Quick-look charts of the Stage 4 results.

    .venv/bin/python tools/plot_benchmark.py --config config.local.toml --year 2022

Reads what Stage 4 wrote (the tract and metrics tables) and writes two PNGs:

  1. scatter_tract_truth_vs_pred_{year}.png
        census residents against the estimate, one panel per product, tracts
  2. bars_metrics_by_scale_{year}.png
        MAPE, MAE, RMSE and bias, one panel per metric (units differ, so no shared
        axis), bars grouped by scale with one bar per product

Emphasis form: the product under evaluation (BrazilPop, `--accent`) is the accent blue and
every benchmark is one dark gray, so there is no rainbow. Because colour then does not tell
the benchmarks apart, every bar is named under it. Surfaces and ink are the reference
data-viz palette.
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
from matplotlib.patches import PathPatch  # noqa: E402
from matplotlib.path import Path as MPath  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

SCALES = ["tract", "district", "municipality", "state"]
# Emphasis form: the product under evaluation (BrazilPop) is the accent blue, every benchmark
# is the same dark gray. Identity is not carried by colour, so each bar is named under it.
ACCENT_KEY = "brazilpop"
ACCENT = {"light": "#2a78d6", "dark": "#3987e5"}
NEUTRAL = {"light": "#52514e", "dark": "#898781"}        # scatter points
BAR_NEUTRAL = {"light": "#898781", "dark": "#a8a7a0"}    # benchmark bars: a lighter gray
PANEL = {"light": "#f3f3f1", "dark": "#222221"}          # very subtle gray behind the bar panels
SURFACE = {"light": "#fcfcfb", "dark": "#1a1a19"}
INK = {  # primary, secondary, muted
    "light": ("#0b0b0b", "#52514e", "#898781"),
    "dark": ("#ffffff", "#c3c2b7", "#898781"),
}
GRID = {"light": "#e1e0d9", "dark": "#2c2c2a"}
BASELINE = {"light": "#c3c2b7", "dark": "#383835"}
FONT = ["system-ui", "Segoe UI", "DejaVu Sans", "sans-serif"]
PRODUCT_LABEL = {"brazilpop": "BrazilPop", "landscan": "LandScan", "worldpop": "WorldPop", "ghsl": "GHSL"}


def label_of(key: str, labels: dict | None = None) -> str:
    """Display name: the config `label` (from the Stage 4 report), then a built-in name, then the key."""
    return (labels or {}).get(key) or PRODUCT_LABEL.get(key, key)


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
        "font.family": "sans-serif", "font.sans-serif": FONT, "font.size": 9,
        "mathtext.default": "regular",
        "figure.facecolor": SURFACE[mode], "axes.facecolor": SURFACE[mode], "savefig.facecolor": SURFACE[mode],
        "axes.edgecolor": BASELINE[mode], "axes.labelcolor": ink[1], "text.color": ink[0],
        "xtick.color": ink[2], "ytick.color": ink[2], "axes.linewidth": 1.0,
        "axes.spines.top": False, "axes.spines.right": False,
        "grid.color": GRID[mode], "grid.linewidth": 1.0, "grid.linestyle": "-",
    })


def _thousands(ax, axis="y") -> None:
    f = FuncFormatter(lambda v, _: f"{v:,.0f}")
    (ax.yaxis if axis == "y" else ax.xaxis).set_major_formatter(f)


# ------------------------------------------------------------------------------ scatter

def _window(tract: pd.DataFrame, products: list[str]) -> float:
    """Upper axis limit: the bulk of the data (99.5th percentile of census and estimates), rounded up."""
    pooled = pd.concat([tract["p_k"]] + [tract[f"pi_sum_{k}"] for k in products]).dropna()
    q = float(pooled.quantile(0.995)) * 1.05
    step = 100 if q < 2000 else 500 if q < 10000 else 1000
    return float(np.ceil(q / step) * step)


def scatter_truth_vs_pred(tract: pd.DataFrame, metrics: pd.DataFrame, products: list[str], out: Path,
                          title: str, mode: str = "light", dpi: int = 150, labels: dict | None = None,
                          accent: str = ACCENT_KEY) -> Path:
    """One square panel per product: census residents (x) against the estimate (y), per tract.

    The axes show the bulk of the tracts (same limits in every panel). Tracts beyond them are
    counted in the panel, and the one with the largest error is named.
    """
    _style(mode)
    ink = INK[mode]
    n = len(products)
    hi = _window(tract, products)
    cols = n if n <= 3 else 2 if n == 4 else 3          # 1-3 in a row, 4 as 2 x 2, more in rows of three
    rows = -(-n // cols)
    fig, axes = plt.subplots(rows, cols, figsize=(5.6 * cols, 6.1 * rows), squeeze=False, layout="constrained")
    fig.suptitle(title, x=0.01, ha="left", fontsize=12, fontweight="bold", color=ink[0])
    for extra in list(axes.flat)[n:]:
        extra.set_visible(False)

    for ax, key in zip(axes.flat, products):
        col = color_of(key, mode, accent)
        d = tract[tract["p_k"].notna() & tract[f"pi_sum_{key}"].notna()]
        ax.set_axisbelow(True)
        ax.grid(True, linewidth=1.0)
        ax.plot([0, hi], [0, hi], color=ink[2], linewidth=1.0, zorder=1)             # 1:1 reference
        ax.scatter(d["p_k"], d[f"pi_sum_{key}"], s=7, color=col, alpha=0.35 if key == accent else 0.3,
                   linewidths=0, zorder=2)
        ax.set_xlim(0, hi)
        ax.set_ylim(0, hi)
        ax.set_aspect("equal", adjustable="box")
        _thousands(ax, "x")
        _thousands(ax, "y")
        ax.set_xlabel("Census residents of the tract")
        ax.set_ylabel("Estimated residents (zonal sum)")

        m = metrics[(metrics["product"] == key) & (metrics["scale"] == "tract")]
        res = m["resolution"].iloc[0] if len(m) else ""
        sub = "external raster" if res == "external" else f"{res} raster" if res else ""
        ax.set_title(f"{label_of(key, labels)}  ·  {sub}" if sub else label_of(key, labels), loc="left",
                     fontsize=10, fontweight="bold", color=ink[0])
        # the same statistics as the metrics file; computed from the table when the product is
        # not scored at tract scale (its min_scale is higher), so every panel is readable
        e = d[f"pi_sum_{key}"] - d["p_k"]
        pos = d["p_k"] > 0
        stats = {"units": len(d), "mae": float(e.abs().mean()) if len(d) else np.nan,
                 "mape": float((e.abs()[pos] / d["p_k"][pos]).mean() * 100) if pos.any() else np.nan,
                 "ratio": float(d[f"pi_sum_{key}"].sum() / d["p_k"].sum()) if d["p_k"].sum() else np.nan}
        if len(m):
            stats.update({k: m.iloc[0][k] for k in ("units", "mae", "mape", "ratio")})
        ax.text(0.03, 0.97, f"{int(stats['units']):,} tracts\nMAE {short(stats['mae'])}  ·  "
                            f"MAPE {short(stats['mape'], True)}\nestimate / census {stats['ratio']:.3f}",
                transform=ax.transAxes, va="top", ha="left", fontsize=8.5, color=ink[1], linespacing=1.5)

        outside = d[(d["p_k"] > hi) | (d[f"pi_sum_{key}"] > hi)]
        err = (d[f"pi_sum_{key}"] - d["p_k"]).abs()
        note = []
        if len(outside):
            note.append(f"{len(outside)} tract{'s' if len(outside) != 1 else ''} beyond the axes")
        if len(err):
            w = d.loc[err.idxmax()]
            note.append(f"largest error: tract {w['code']}\ncensus {w['p_k']:,.0f}, estimate {w[f'pi_sum_{key}']:,.0f}")
        if note:
            ax.text(0.97, 0.03, "\n".join(note), transform=ax.transAxes, va="bottom", ha="right", fontsize=8,
                    color=ink[1], linespacing=1.5)
        ax.text(hi * 0.985, hi * 0.93, "1:1", color=ink[2], fontsize=8, ha="right", va="top", rotation=45,
                rotation_mode="anchor")

    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=dpi)
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------------- bars

def _rounded_bar(ax, x0: float, x1: float, base: float, tip: float, color: str, radius_px: float) -> None:
    """A bar that grows from `base`: square at the baseline, rounded at the data end.

    The corner radius is given in pixels and converted to data units per axis, so the
    corners look circular whatever the axis scales (including log and symlog).
    """
    t, inv = ax.transData, ax.transData.inverted()
    (px0, pb), (px1, pt) = t.transform((x0, base)), t.transform((x1, tip))
    r = min(radius_px, abs(px1 - px0) / 2, abs(pt - pb))
    s = 1.0 if pt >= pb else -1.0
    k = 0.5523 * r

    def pt_(px, py):
        return tuple(inv.transform((px, py)))

    verts = [pt_(px0, pb), pt_(px0, pt - s * r),
             pt_(px0, pt - s * r + s * k), pt_(px0 + r - k, pt), pt_(px0 + r, pt),
             pt_(px1 - r, pt),
             pt_(px1 - r + k, pt), pt_(px1, pt - s * r + s * k), pt_(px1, pt - s * r),
             pt_(px1, pb), pt_(px0, pb)]
    codes = [MPath.MOVETO, MPath.LINETO, MPath.CURVE4, MPath.CURVE4, MPath.CURVE4, MPath.LINETO,
             MPath.CURVE4, MPath.CURVE4, MPath.CURVE4, MPath.LINETO, MPath.CLOSEPOLY]
    ax.add_patch(PathPatch(MPath(verts, codes), facecolor=color, edgecolor="none", zorder=3))


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
                          labels: dict | None = None, accent: str = ACCENT_KEY) -> Path:
    """2 x 2 panels, one per metric (their units differ, so no shared axis).

    Bars are grouped by scale, one bar per product, with the value at the tip. The value
    axes are logarithmic (symmetric log for the signed bias) because the absolute errors
    grow about 400-fold from tract to state; every bar carries its value. BrazilPop is the
    accent blue and the benchmarks are one gray, so each bar is named under it and the
    scale is the group label.
    """
    _style(mode)
    ink = INK[mode]
    scales = [s for s in SCALES if s in set(metrics["scale"])]
    n = max(len(products), 1)
    fig, axes = plt.subplots(2, 2, figsize=(11.4 + 1.7 * max(0, n - 2), 8.6), layout="constrained", dpi=dpi)
    fig.suptitle(title, x=0.01, ha="left", fontsize=12, fontweight="bold", color=ink[0])

    def value(scale, key, col):
        row = metrics[(metrics["scale"] == scale) & (metrics["product"] == key)]
        if row.empty or pd.isna(row[col].iloc[0]):
            return None
        return float(row[col].iloc[0])

    gap = 0.05
    slot = 0.8 / n                                        # one bar width for every group

    def bar_slots(col):
        """{(group index, product): (x0, x1)}. A product not scored at a scale leaves no gap:
        the bars that are present are centred in their group."""
        out_ = {}
        for gi, scale in enumerate(scales):
            present = [k for k in products if value(scale, k, col) is not None]
            start = gi - slot * len(present) / 2
            for j, key in enumerate(present):
                out_[(gi, key)] = (start + j * slot + gap / 2, start + (j + 1) * slot - gap / 2)
        return out_

    names = [label_of(k, labels) for k in products]
    name_pts = max((len(x) for x in names), default=6) * 7.5 * 0.58 + 6      # rotated name height, points

    for ax, (col, ttl, unit, pct, signed) in zip(axes.flat, PANELS):
        vals = [v for sc in scales for k in products if (v := value(sc, k, col)) is not None]
        mags = [abs(v) for v in vals if v != 0]
        ax.set_facecolor(PANEL[mode])
        ax.set_axisbelow(True)
        ax.grid(True, axis="y", linewidth=1.0)
        ax.set_xlim(-0.6, len(scales) - 0.4)
        top = 10 ** np.ceil(np.log10(max(mags)) + 0.35) if mags else 10.0
        if signed:
            ax.set_yscale("symlog", linthresh=1.0, linscale=0.6)
            ax.set_ylim(-top if any(v < 0 for v in vals) else 0, top if any(v > 0 for v in vals) else 1)
            ax.axhline(0, color=BASELINE[mode], linewidth=1.0, zorder=2)
            note = "symmetric log scale"
        else:
            bottom = 10 ** np.floor(np.log10(min(mags)) - 0.35) if mags else 0.1
            ax.set_yscale("log")
            ax.set_ylim(bottom, top)
            note = "log scale"
        ax.yaxis.set_major_formatter(FuncFormatter(_sci_formatter))
        slots = bar_slots(col)
        centres = [(x0 + x1) / 2 for x0, x1 in slots.values()]
        ax.set_xticks(centres)
        ax.set_xticklabels([label_of(k, labels) for (_gi, k) in slots], rotation=90, fontsize=7.5, color=ink[1])
        ax.tick_params(axis="x", length=0, pad=3)
        for gi, scale in enumerate(scales):                # the scale is the group label, under the names
            ax.annotate(scale.capitalize(), xy=(gi, 0), xycoords=("data", "axes fraction"),
                        xytext=(0, -(name_pts + 10)), textcoords="offset points", ha="center", va="top",
                        fontsize=9.5, fontweight="bold", color=ink[0], annotation_clip=False)
        ax.set_title(ttl, loc="left", fontsize=10, fontweight="bold", color=ink[0])
        ax.set_ylabel(f"{unit}  ({note})", color=ink[1], fontsize=8.5)
    fig.canvas.draw()                                     # layout is final: pixels can now be measured

    radius_px = 4.0 * dpi / 96.0
    for ax, (col, _t, _u, pct, signed) in zip(axes.flat, PANELS):
        base = 0.0 if signed else ax.get_ylim()[0]
        for (gi, key), (x0, x1) in bar_slots(col).items():
            v = value(scales[gi], key, col)
            tip = max(v, base) if not signed else v
            if tip != base:
                _rounded_bar(ax, x0, x1, base, tip, color_of(key, mode, accent, BAR_NEUTRAL), radius_px)
            up = v >= 0
            ax.annotate(short(v, pct), xy=((x0 + x1) / 2, tip), xytext=(0, 4 if up else -4),
                        textcoords="offset points", ha="center", va="bottom" if up else "top",
                        fontsize=8 if n <= 3 else 7, color=ink[1])

    roles = []
    if accent in products:
        roles.append((label_of(accent, labels), ACCENT[mode]))
    if any(k != accent for k in products):
        roles.append(("Benchmark rasters (named under each bar)", BAR_NEUTRAL[mode]))
    fig.legend([plt.Line2D([0], [0], marker="s", linestyle="", markersize=9, color=c) for _, c in roles],
               [t for t, _ in roles], loc="upper right", ncol=len(roles), frameon=False,
               fontsize=9.5, labelcolor=ink[1], handletextpad=0.3, columnspacing=1.6)
    if footnote:
        fig.supxlabel(footnote, x=0.01, ha="left", fontsize=8, color=ink[2])      # layout reserves its space
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=dpi)
    plt.close(fig)
    return out


# ------------------------------------------------------------------------------------ CLI

def build(config: str, year: int, out_dir: Path | None, mode: str, dpi: int,
          accent: str = ACCENT_KEY) -> list[Path]:
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
    labels = report.get("labels", {})

    ufs = ", ".join(report.get("ufs", [])) or "selected UFs"
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
    f1 = scatter_truth_vs_pred(tract, metrics, keys, out / f"scatter_tract_truth_vs_pred_{year}.png",
                               f"Tracts, {ufs} {year}: estimate against census", mode, dpi, labels, accent)
    f2 = bars_metrics_by_scale(metrics, keys, out / f"bars_metrics_by_scale_{year}.png",
                               f"Accuracy by scale, {ufs} {year}", mode, dpi, footnote="; ".join(notes),
                               labels=labels, accent=accent)
    return [f1, f2]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--out-dir", help="default: <benchmarking output_dir>/plots")
    ap.add_argument("--mode", choices=["light", "dark"], default="light")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--accent", default=ACCENT_KEY,
                    help="product drawn in the accent blue (default brazilpop); the others are gray")
    args = ap.parse_args(argv)
    for p in build(args.config, args.year, args.out_dir, args.mode, args.dpi, args.accent):
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
