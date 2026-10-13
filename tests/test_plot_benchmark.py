"""tools/plot_benchmark.py: the charts are written and follow the colour rules."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

pytest.importorskip("matplotlib")
from tools.plot_benchmark import (  # noqa: E402
    ACCENT, BAR_NEUTRAL, NEUTRAL, PANEL, bars_metrics_by_scale, color_of, label_of, sci_label, scatter_truth_vs_pred, short,
)

PRODUCTS = ["brazilpop", "landscan"]


def frames(products=PRODUCTS):
    rng = np.random.default_rng(1)
    truth = rng.integers(50, 1500, 200).astype(float)
    t = pd.DataFrame({"code": [f"53{i:013d}" for i in range(200)], "p_k": truth})
    for i, k in enumerate(products):
        t[f"pi_sum_{k}"] = truth * rng.normal(1 + 0.03 * i, 0.05 + 0.1 * i, 200).clip(0)
    rows = []
    for scale, units in (("tract", 200), ("district", 4), ("municipality", 1), ("state", 1)):
        for k in products:
            rows.append({"scale": scale, "product": k, "resolution": "030" if k == "brazilpop" else "external",
                         "units": units, "bias": -2.0 * units, "mae": 30.0 * units, "rmse": 40.0 * units,
                         "mape": 14.0 / units, "ratio": 0.99})
    return t, pd.DataFrame(rows)


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_both_charts_are_written(tmp_path, mode):
    t, m = frames()
    f1 = scatter_truth_vs_pred(t, m, PRODUCTS, tmp_path / "s.png", "title", mode, dpi=60)
    f2 = bars_metrics_by_scale(m, PRODUCTS, tmp_path / "b.png", "title", mode, dpi=60, footnote="note")
    assert f1.stat().st_size > 5_000 and f2.stat().st_size > 5_000


@pytest.mark.parametrize("n", [1, 3, 4, 5, 8, 11])
def test_charts_take_any_number_of_benchmarks(tmp_path, n):
    products = ["brazilpop"] + [f"bench{i}" for i in range(1, n)]
    t, m = frames(products)
    labels = {"bench1": "Bench One (2020)"}
    assert scatter_truth_vs_pred(t, m, products, tmp_path / "s.png", "t", dpi=50, labels=labels).stat().st_size > 3_000
    assert bars_metrics_by_scale(m, products, tmp_path / "b.png", "t", dpi=50, labels=labels).stat().st_size > 3_000


def test_a_product_without_tract_metrics_still_gets_a_readable_panel(tmp_path):
    """A benchmark scored only from municipality up has no tract row in the metrics file."""
    t, m = frames()
    m = m[~((m["product"] == "landscan") & (m["scale"] == "tract"))]
    out = scatter_truth_vs_pred(t, m, PRODUCTS, tmp_path / "s.png", "t", dpi=50)
    assert out.stat().st_size > 3_000
    bars_metrics_by_scale(m, PRODUCTS, tmp_path / "b.png", "t", dpi=50)       # gaps are centred, not an error


def test_one_accent_and_one_neutral_whatever_the_number_of_benchmarks():
    """BrazilPop is the accent; every benchmark is the same gray (no rainbow)."""
    assert color_of("brazilpop") == ACCENT["light"]
    others = {color_of(k) for k in ("landscan", "worldpop", "ghsl", "anything_else")}
    assert others == {NEUTRAL["light"]}
    assert color_of("brazilpop", "dark") == ACCENT["dark"] and color_of("landscan", "dark") == NEUTRAL["dark"]
    assert color_of("landscan", accent="landscan") == ACCENT["light"]          # the accent can be changed


def test_labels_come_from_the_config_then_a_builtin_name_then_the_key():
    assert label_of("ghsl", {"ghsl": "GHSL (2020)"}) == "GHSL (2020)"
    assert label_of("landscan") == "LandScan"
    assert label_of("something_new") == "something_new"


def test_value_labels():
    assert short(12709.1) == "10$^{4}$" and short(26000) == "3×10$^{4}$" and short(430000) == "4×10$^{5}$" and short(-94624) == "-9×10$^{4}$" and short(32.0) == "32.0" and short(236.8, True) == "236.8%"
    assert short(1000) == "1,000" and short(9999) == "9,999" and short(10000) == "10$^{4}$" and short(96000) == "10$^{5}$"
    assert short(999.4) == "999" and short(450) == "450"
    assert short(float("nan")) == "n/a"


def test_axis_ticks_use_scientific_notation():
    assert sci_label(1000) == "$10^{3}$"
    assert sci_label(0.1) == "$10^{-1}$"
    assert sci_label(1) == "$10^{0}$"
    assert sci_label(-10000) == "$-10^{4}$"
    assert sci_label(2000) == "$2\\times10^{3}$"
    assert sci_label(0) == "0"


def _luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def test_benchmark_bars_are_a_lighter_gray_than_the_scatter_points():
    assert _luminance(BAR_NEUTRAL["light"]) > _luminance(NEUTRAL["light"])
    assert _luminance(BAR_NEUTRAL["dark"]) > _luminance(NEUTRAL["dark"])
    assert color_of("landscan", neutral=BAR_NEUTRAL) == BAR_NEUTRAL["light"]
    assert color_of("brazilpop", neutral=BAR_NEUTRAL) == ACCENT["light"]


def test_panel_background_is_a_light_tone_close_to_the_surface():
    from tools.plot_benchmark import SURFACE
    for mode in ("light", "dark"):
        a, b = _luminance(PANEL[mode]), _luminance(SURFACE[mode])
        assert a != b and abs(a - b) < 0.2           # the seaborn tone: visible, never a block of colour


def test_a_product_without_data_is_drawn_as_a_null_bar_and_a_null_panel(tmp_path):
    t, m = frames()
    keys = PRODUCTS + ["hrsl"]
    f1 = scatter_truth_vs_pred(t, m, keys, tmp_path / "s.png", "t", null_products=["hrsl"])
    f2 = bars_metrics_by_scale(m, keys, tmp_path / "b.png", "t", null_products=["hrsl"])
    assert f1.stat().st_size > 0 and f2.stat().st_size > 0


def test_scatter_panels_are_three_across_then_the_rest_below(tmp_path):
    from PIL import Image
    keys = ["brazilpop", "landscan", "worldpop", "ghsl"]
    t, m = frames(keys)
    three = scatter_truth_vs_pred(t, m, keys[:3], tmp_path / "s3.png", "t")
    five = scatter_truth_vs_pred(t, m, keys + ["hrsl"], tmp_path / "s5.png", "t", null_products=["hrsl"])
    (w3, h3), (w5, h5) = Image.open(three).size, Image.open(five).size
    assert abs(w3 / h3 - 1.5) < 0.01 and abs(w5 / h5 - 1.5) < 0.01     # both landscape 3:2
    assert h5 > 1.2 * h3 and w5 >= w3         # the fourth and fifth panels open a second row


@pytest.mark.parametrize("kind", ["scatter", "hexbin"])
@pytest.mark.parametrize("scale,units", [("district", 4), ("municipality", 1), ("state", 1)])
def test_coarse_scales_with_very_few_units_are_drawn(tmp_path, kind, scale, units):
    rng = np.random.default_rng(2)
    truth = rng.integers(500_000, 3_000_000, units).astype(float)
    t = pd.DataFrame({"code": [str(i) for i in range(units)], "p_k": truth})
    for i, k in enumerate(PRODUCTS):
        t[f"pi_sum_{k}"] = truth * (1 + 0.02 * i)
    m = frames()[1]
    out = scatter_truth_vs_pred(t, m, PRODUCTS, tmp_path / "s.png", "t", kind=kind, scale=scale)
    assert out.stat().st_size > 0


def test_a_product_not_scored_at_a_scale_gets_an_empty_panel(tmp_path):
    t = pd.DataFrame({"code": ["1"], "p_k": [1000.0], "pi_sum_brazilpop": [990.0], "pi_sum_landscan": [np.nan]})
    out = scatter_truth_vs_pred(t, frames()[1], PRODUCTS, tmp_path / "s.png", "t", scale="municipality")
    assert out.stat().st_size > 0


def test_plot_config_is_minimal_strict_and_commented(tmp_path):
    import json
    from tools.plot_config import PlotConfigError, load, validate
    p = tmp_path / "plot.json"
    p.write_text(json.dumps({"_comment": "x", "input_dir": "/d/{year}", "year": 2022}))
    cfg = load(p)
    assert cfg["kinds"] == ["scatter"] and cfg["products"] is None and cfg["mode"] == "light"
    for bad in ({"year": 2022}, {"input_dir": "d"}, {"input_dir": "d", "year": 2022, "dpii": 9},
                {"input_dir": "d", "year": 2022, "scales": ["village"]},
                {"input_dir": "d", "year": 2022, "products": {"a": {"min_scale": "x"}}}):
        with pytest.raises(PlotConfigError):
            validate(bad)
    with pytest.raises(PlotConfigError):
        load(tmp_path / "missing.json")


def test_build_runs_from_the_settings_alone(tmp_path):
    from tools.plot_benchmark import build
    t, m = frames()
    t.to_csv(tmp_path / "benchmark_2022_tract.csv", index=False)
    m.to_csv(tmp_path / "benchmark_2022_metrics.csv", index=False)
    out = build({"input_dir": str(tmp_path), "year": 2022, "dpi": 50, "scales": ["tract"],
                 "products": {"brazilpop": {"label": "BrazilPop V{version}"}, "landscan": {}, "hrsl": {}},
                 "version": "6"})
    names = sorted(p.name for p in out)
    assert names == ["bars_metrics_by_scale_2022.jpg", "scatter_tract_truth_vs_pred_2022.jpg"]
    assert all(p.parent == tmp_path / "plots" and p.stat().st_size > 3_000 for p in out)


def test_charts_read_the_year_suffixed_columns_of_stage_4(tmp_path):
    from tools.plot_benchmark import build, plain_columns
    t, m = frames(["brazilpop", "ghsl"])
    t = t.rename(columns={"pi_sum_brazilpop": "pi_sum_brazilpop_2022", "pi_sum_ghsl": "pi_sum_ghsl_2020"})
    m = m.rename(columns={"ratio": "ratio_product_to_census"})
    assert {"pi_sum_brazilpop", "pi_sum_ghsl"} <= set(plain_columns(t, {"ghsl": 2020}).columns)
    t.to_csv(tmp_path / "benchmark_2022_tract.csv", index=False)
    m.to_csv(tmp_path / "benchmark_2022_metrics.csv", index=False)
    (tmp_path / "benchmark_2022_report.json").write_text('{"epochs": {"brazilpop": 2022, "ghsl": 2020}}')
    out = build({"input_dir": str(tmp_path), "year": 2022, "dpi": 50, "scales": ["tract"]})
    assert len(out) == 2 and all(p.stat().st_size > 3_000 for p in out)


def test_scatter_axes_have_five_round_ticks_and_population_labels_are_scientific():
    from tools.plot_benchmark import _window, pop_label
    t, _ = frames()
    hi = _window(t, PRODUCTS)
    quarter = hi / 4
    mant = quarter / 10 ** np.floor(np.log10(quarter))
    assert any(abs(mant - m) < 1e-9 for m in (1, 2, 2.5, 5, 10))     # every step is a round number
    assert pop_label(0) == "0" and pop_label(750) == "750" and pop_label(999.6) == "1,000"
    assert pop_label(1000) == "1,000" and pop_label(2500) == "2,500" and pop_label(10000) == "10 × 10$^{3}$"
    assert pop_label(12500) == "12.5 × 10$^{3}$" and pop_label(4_000_000) == "4,000 × 10$^{3}$"
    assert short(13000, sci=False) == "13,000" and "k" not in pop_label(50000)


def test_hexbin_colour_scale_clips_both_ends_and_snaps_to_powers_of_ten(tmp_path):
    from tools.plot_benchmark import hex_bounds, hex_counts
    assert hex_bounds(np.arange(1, 1001)) == (100.0, 1000.0)         # p5 = 51 -> 10^2, p95 = 950 -> 10^3
    assert hex_bounds(np.array([])) == (10.0, 100.0)
    lo, hi = hex_bounds(np.r_[np.ones(50), np.full(40, 30), np.full(9, 700), [90000]])
    assert (lo, hi) == (10.0, 1000.0)                                # p5 = 1 -> floored at 10; p95 = 700: the 90,000 is clipped
    assert hex_bounds(np.arange(1, 11))[0] == 10.0                   # never below 10, whatever the data
    assert hex_bounds(np.full(20, 300.0)) == (100.0, 1000.0)         # never less than a decade
    t, m = frames()
    counts = hex_counts(t, PRODUCTS)
    assert len(counts) > 0 and counts.min() >= 1
    out = scatter_truth_vs_pred(t, m, PRODUCTS, tmp_path / "h.png", "t", dpi=50, kind="hexbin", hex_range=(10.0, 1000.0))
    assert out.stat().st_size > 3_000


def test_panels_are_ordered_brazilpop_first_then_alphabetically_by_label():
    from tools.plot_benchmark import order_products
    keys = ["worldpop", "hrsl", "landscan", "brazilpop", "ghsl"]
    assert order_products(keys) == ["brazilpop", "ghsl", "hrsl", "landscan", "worldpop"]
    assert order_products(keys, {"ghsl": "Zeta GHSL", "hrsl": "alpha HRSL"}) == \
        ["brazilpop", "hrsl", "landscan", "worldpop", "ghsl"]               # by the label shown, case-insensitive
    assert order_products(keys, accent="landscan")[0] == "landscan"


def test_cmap_is_a_setting_with_the_plasma_default_and_is_checked(tmp_path):
    from tools.plot_config import PlotConfigError, validate
    base = {"input_dir": "d", "year": 2022}
    assert validate(base)["cmap"] == "plasma_r"
    assert validate({**base, "cmap": "viridis"})["cmap"] == "viridis"
    with pytest.raises(PlotConfigError):
        validate({**base, "cmap": "not_a_cmap"})
    t, m = frames()
    out = scatter_truth_vs_pred(t, m, PRODUCTS, tmp_path / "h.png", "t", dpi=50, kind="hexbin", cmap="viridis")
    assert out.stat().st_size > 3_000


def test_error_histograms_have_100_bins_everywhere_and_are_written(tmp_path):
    import tools.plot_benchmark as pb
    from tools.plot_benchmark import build, error_histograms
    assert pb.HIST_BINS == 100
    t, m = frames(["brazilpop", "landscan"])
    out = error_histograms(t, ["brazilpop", "landscan", "hrsl"], tmp_path / "h.png", "t", dpi=50,
                           null_products=["hrsl"])
    assert out.stat().st_size > 3_000
    t.to_csv(tmp_path / "benchmark_2022_tract.csv", index=False)
    m.to_csv(tmp_path / "benchmark_2022_metrics.csv", index=False)
    names = sorted(p.name for p in build({"input_dir": str(tmp_path), "year": 2022, "dpi": 50, "scales": ["tract"],
                                          "kinds": ["histogram"]}))
    assert names == ["bars_metrics_by_scale_2022.jpg", "histogram_tract_error_2022.jpg"]



def test_every_figure_is_landscape_3_by_2(tmp_path):
    from PIL import Image
    from tools.plot_benchmark import _landscape, error_histograms
    assert _landscape(10, 10) == (15, 10) and _landscape(12, 4) == (12, 8) and _landscape(15, 10) == (15, 10)
    t, m = frames(["brazilpop", "landscan", "worldpop", "ghsl", "hrsl"])
    keys = ["brazilpop", "landscan", "worldpop", "ghsl", "hrsl"]
    for name, out in (("scatter", scatter_truth_vs_pred(t, m, keys, tmp_path / "s.png", "t", dpi=50)),
                      ("hexbin", scatter_truth_vs_pred(t, m, keys, tmp_path / "h.png", "t", dpi=50, kind="hexbin")),
                      ("hist", error_histograms(t, keys, tmp_path / "e.png", "t", dpi=50)),
                      ("bars", bars_metrics_by_scale(m, keys, tmp_path / "b.png", "t", dpi=50)),
                      ("one row", scatter_truth_vs_pred(t, m, keys[:2], tmp_path / "r.png", "t", dpi=50))):
        w, h = Image.open(out).size
        assert w / h >= 1.49, (name, w, h)               # never narrower than 3:2 (grown, not shrunk)


def test_style_defaults_are_central_overlaid_by_the_style_key_and_checked(tmp_path):
    import tools.plot_benchmark as pb
    from tools.plot_config import PlotConfigError, validate
    from tools.plot_style import DEFAULT_STYLE, StyleError, merge
    assert merge({})["tick_pt"] == DEFAULT_STYLE["tick_pt"] == 8
    st = merge({"tick_pt": 9, "colors": {"panel": {"light": "#f0f0f6"}}})
    assert st["tick_pt"] == 9 and st["colors"]["panel"]["light"] == "#f0f0f6" and st["colors"]["panel"]["dark"] == "#55555d"
    for bad in ({"tickpt": 9}, {"tick_pt": -1}, {"hist_bins": 2.5}, {"colors": {"panel": {"light": "red"}}},
                {"colors": {"nope": {}}}, {"hex_clip_pct": [90, 10]}, {"jpeg_quality": 200}):
        with pytest.raises(StyleError):
            merge(bad)
    with pytest.raises(PlotConfigError):
        validate({"input_dir": "d", "year": 2022, "style": {"tickpt": 9}})
    with pytest.raises(PlotConfigError):
        validate({"input_dir": "d", "year": 2022, "format": "gif"})
    assert validate({"input_dir": "d", "year": 2022})["format"] == "jpeg"
    # build() applies the overlay and does not leak it into the next call
    t, m = frames(["brazilpop", "landscan"])
    t.to_csv(tmp_path / "benchmark_2022_tract.csv", index=False)
    m.to_csv(tmp_path / "benchmark_2022_metrics.csv", index=False)
    base = {"input_dir": str(tmp_path), "year": 2022, "dpi": 50, "scales": ["tract"], "kinds": ["histogram"]}
    pb.build({**base, "style": {"hist_bins": 40, "tick_pt": 9}})
    assert pb.HIST_BINS == 40 and pb.TICK_PT == 9
    out = pb.build({**base, "format": "png"})
    assert pb.HIST_BINS == 100 and pb.TICK_PT == 8
    assert sorted(p.suffix for p in out) == [".png", ".png"]


def test_the_default_output_is_a_jpeg_file(tmp_path):
    from tools.plot_benchmark import build
    t, m = frames(["brazilpop", "landscan"])
    t.to_csv(tmp_path / "benchmark_2022_tract.csv", index=False)
    m.to_csv(tmp_path / "benchmark_2022_metrics.csv", index=False)
    out = build({"input_dir": str(tmp_path), "year": 2022, "dpi": 50, "scales": ["tract"]})
    assert all(p.suffix == ".jpg" and p.read_bytes()[:2] == b"\xff\xd8" for p in out)       # JPEG magic number


def test_cmap_can_be_clipped_to_a_range(tmp_path):
    from tools.plot_benchmark import _clipped_cmap
    from tools.plot_config import PlotConfigError, validate
    full, clipped = _clipped_cmap("gnuplot"), _clipped_cmap("gnuplot", 0.0, 0.8)
    assert clipped(1.0) == full(0.8) and clipped(0.0) == full(0.0)          # the end is the 80% point, rescaled
    base = {"input_dir": "d", "year": 2022}
    assert validate(base)["cmap_min"] == 0.0 and validate(base)["cmap_max"] == 1.0
    for bad in ({"cmap_min": 0.9, "cmap_max": 0.5}, {"cmap_max": 1.5}, {"cmap_min": -0.1}):
        with pytest.raises(PlotConfigError):
            validate({**base, **bad})
    t, m = frames()
    out = scatter_truth_vs_pred(t, m, PRODUCTS, tmp_path / "h.png", "t", dpi=50, kind="hexbin", cmap="gnuplot",
                                cmap_range=(0.0, 0.8))
    assert out.stat().st_size > 3_000
