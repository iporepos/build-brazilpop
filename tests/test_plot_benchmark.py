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
    assert short(12709.1) == "12.7k" and short(32.0) == "32.0" and short(236.8, True) == "236.8%"
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
    assert abs(w3 - w5) < 0.05 * w3          # same three columns whatever the number of products
    assert h5 > 1.6 * h3                     # the fourth and fifth panels open a second row


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
