"""PyQGIS zonal sums (stages/qgis_zonal.py) on a hand-computed example.

Run with the interpreter that has PyQGIS (QGIS_PYTHON, default /usr/bin/python3).
Skipped when QGIS is not available.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER = REPO_ROOT / "stages" / "qgis_zonal.py"
QGIS_PYTHON = os.environ.get("QGIS_PYTHON", "/usr/bin/python3")


def _has_qgis() -> bool:
    try:
        return subprocess.run([QGIS_PYTHON, "-c", "import qgis.core"], capture_output=True).returncode == 0
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _has_qgis(), reason="PyQGIS not available")


def write_raster(path: Path, rows: list[list[float]], xll=10.0, yll=50.0, cell=1.0, nodata=-9999) -> None:
    """A float raster from rows (top row first), through an ASCII grid and gdal_translate."""
    asc = path.with_suffix(".asc")
    asc.write_text(
        f"ncols {len(rows[0])}\nnrows {len(rows)}\nxllcorner {xll}\nyllcorner {yll}\n"
        f"cellsize {cell}\nNODATA_value {nodata}\n" + "\n".join(" ".join(map(str, r)) for r in rows) + "\n")
    subprocess.run(["gdal_translate", "-q", "-of", "GTiff", "-ot", "Float32", "-a_srs", "EPSG:4326",
                    str(asc), str(path)], check=True)


def box(x0, y0, x1, y1):
    return [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]


def run_runner(tmp_path: Path, job: dict) -> dict:
    spec = tmp_path / "job.json"
    spec.write_text(json.dumps({"qgis_prefix": "/usr", "jobs": [job]}))
    r = subprocess.run([QGIS_PYTHON, str(RUNNER), str(spec)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    with open(job["output_csv"], newline="", encoding="utf-8") as fh:
        return {row["code"]: row for row in csv.DictReader(fh)}


@pytest.fixture()
def inputs(tmp_path):
    # 4 x 3 cells of 1 degree; x 10..14, y 50..53. Cell centres x = 10.5 .. 13.5, y = 52.5, 51.5, 50.5
    write_raster(tmp_path / "pop.tif", [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, -9999]])
    write_raster(tmp_path / "ones.tif", [[1, 1, 1, 1], [1, 1, 1, 1], [1, 1, 1, 1]])
    zones = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"code": "A", "name": "west"},
         "geometry": {"type": "Polygon", "coordinates": box(10, 50, 11.6, 53)}},      # centres x 10.5 and 11.5
        {"type": "Feature", "properties": {"code": "B", "name": "east"},
         "geometry": {"type": "Polygon", "coordinates": box(11.6, 50, 14, 53)}},      # centres x 12.5 and 13.5
        {"type": "Feature", "properties": {"code": "C", "name": "tiny"},
         "geometry": {"type": "Polygon", "coordinates": box(10.9, 51.9, 11.1, 52.1)}},  # contains no cell centre
    ]}
    (tmp_path / "zones.geojson").write_text(json.dumps(zones))
    return tmp_path


def job_for(tmp_path: Path, rasters: list[tuple[str, str]]) -> dict:
    return {"scale": "tract", "input": str(tmp_path / "zones.geojson"), "code_field": "code",
            "name_field": "name", "output_csv": str(tmp_path / "out.csv"),
            "rasters": [{"name": n, "path": str(tmp_path / f)} for n, f in rasters]}


def test_centre_rule_and_nodata_for_polygons_with_several_cell_centres(inputs):
    out = run_runner(inputs, job_for(inputs, [("brazilpop", "pop.tif")]))
    a, b = out["A"], out["B"]
    assert (float(a["pi_sum_brazilpop"]), float(a["pi_count_brazilpop"])) == (1 + 2 + 5 + 6 + 9 + 10, 6)
    # B holds the NoData cell: it is skipped, so 5 cells are counted
    assert (float(b["pi_sum_brazilpop"]), float(b["pi_count_brazilpop"])) == (3 + 4 + 7 + 8 + 11, 5)
    assert a["name"] == "west"


def test_qgis_weights_small_polygons_by_area_not_by_cell_centre(inputs):
    """DOCUMENTED QGIS BEHAVIOUR (3.34): a polygon that contains only a few cell centres is
    not summed with the cell-centre rule but by exact overlap area. A polygon of 0.2 x 0.2
    cells straddling four cells receives 0.04 of each (count 0.04 x 4 cells... see values),
    and a cell value can therefore be counted by more than one polygon. If this test fails
    after a QGIS upgrade, the zonal rule has changed and the benchmark must be re-validated."""
    out = run_runner(inputs, job_for(inputs, [("brazilpop", "pop.tif")]))
    c = out["C"]                    # box(10.9, 51.9, 11.1, 52.1): overlaps cells 1, 2, 5, 6 by 0.01 each
    assert float(c["pi_count_brazilpop"]) == pytest.approx(0.04)
    assert float(c["pi_sum_brazilpop"]) == pytest.approx(0.01 * (1 + 2 + 5 + 6))


def test_each_raster_gets_its_own_named_columns(inputs):
    out = run_runner(inputs, job_for(inputs, [("brazilpop", "pop.tif"), ("worldpop", "ones.tif")]))
    assert {"pi_sum_brazilpop", "pi_count_brazilpop", "pi_sum_worldpop", "pi_count_worldpop"} <= set(out["A"])
    assert float(out["A"]["pi_sum_worldpop"]) == 6            # six cells of value 1
    assert float(out["B"]["pi_sum_worldpop"]) == 6            # the NoData cell holds 1 in this raster
    assert float(out["A"]["pi_sum_brazilpop"]) == 33
