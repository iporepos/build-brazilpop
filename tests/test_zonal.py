"""stages/zonal.py: zonal sums by the cell-centre rule, on hand-computed examples."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stages.zonal import ZonalError, ZoneSet, sample_raster  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("gdal_rasterize") is None, reason="GDAL command-line tools not installed")


def write_raster(path: Path, rows, *, dtype="Float32", nodata=-9999, xll=10.0, yll=50.0, cell=1.0, srs="EPSG:4326"):
    asc = path.with_suffix(".asc")
    asc.write_text(f"ncols {len(rows[0])}\nnrows {len(rows)}\nxllcorner {xll}\nyllcorner {yll}\n"
                   f"cellsize {cell}\nNODATA_value {nodata}\n" + "\n".join(" ".join(map(str, r)) for r in rows) + "\n")
    subprocess.run(["gdal_translate", "-q", "-of", "GTiff", "-ot", dtype, "-a_srs", srs, str(asc), str(path)], check=True)
    return path


def zone_set(path: Path, name: str, geoms: list) -> ZoneSet:
    gdf = gpd.GeoDataFrame({"zid": np.arange(1, len(geoms) + 1, dtype="int32"), "code": [f"z{i}" for i in range(len(geoms))]},
                           geometry=geoms, crs="EPSG:4326")
    gdf.to_file(path, layer="z", driver="GPKG")
    return ZoneSet(name, str(path), "z", len(geoms), tuple(gdf.total_bounds))


# 4 x 3 cells of 1 degree, x 10..14, y 50..53; centres x 10.5 .. 13.5, y 52.5, 51.5, 50.5
VALUES = [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, -9999]]


@pytest.fixture()
def raster(tmp_path):
    return write_raster(tmp_path / "r.tif", VALUES)


def test_centre_rule_nodata_and_polygon_smaller_than_a_cell(tmp_path, raster):
    zs = zone_set(tmp_path / "z.gpkg", "s", [
        box(10, 50, 11.6, 53),            # centres x 10.5, 11.5  -> cols 0, 1
        box(11.6, 50, 14, 53),            # centres x 12.5, 13.5  -> cols 2, 3 (one NoData cell)
        box(10.9, 51.9, 11.1, 52.1),      # contains no cell centre
    ])
    r = sample_raster(raster, [zs], workers=1)
    s, c = r["sums"]["s"], r["counts"]["s"]
    assert (s[1], c[1]) == (1 + 2 + 5 + 6 + 9 + 10, 6)
    assert (s[2], c[2]) == (3 + 4 + 7 + 8 + 11, 5)          # the NoData cell is not counted
    assert (s[3], c[3]) == (0, 0)                            # centre rule: nothing, not a fraction
    assert r["raster_total"] == sum(v for row in VALUES for v in row if v != -9999)


def test_overlapping_polygons_never_count_a_cell_twice(tmp_path, raster):
    zs = zone_set(tmp_path / "z.gpkg", "s", [box(11.6, 50, 14, 53), box(13.4, 52.4, 20, 60)])
    r = sample_raster(raster, [zs], workers=1)
    # cell (13.5, 52.5) = 4 lies in both: the last polygon burned (zone 2) takes it
    assert r["sums"]["s"][2] == 4 and r["counts"]["s"][2] == 1
    assert r["sums"]["s"][1] == 3 + 7 + 8 + 11
    assert r["sums"]["s"][1:].sum() <= r["raster_total"]


def test_mass_outside_the_polygons_is_visible(tmp_path, raster):
    zs = zone_set(tmp_path / "z.gpkg", "s", [box(10, 50, 11.6, 53)])
    r = sample_raster(raster, [zs], workers=1)
    assert r["sums"]["s"][1:].sum() == 33 and r["raster_total"] == 66      # 33 lies outside every polygon


def test_several_windows_and_workers_give_the_same_result(tmp_path, raster):
    zs = zone_set(tmp_path / "z.gpkg", "s", [box(10, 50, 11.6, 53), box(11.6, 50, 14, 53)])
    one = sample_raster(raster, [zs], workers=1, window_cells=10_000)
    many = sample_raster(raster, [zs], workers=2, window_cells=4)          # one raster row per window
    assert one["windows"] == 1 and many["windows"] == 3
    np.testing.assert_array_equal(one["sums"]["s"], many["sums"]["s"])
    np.testing.assert_array_equal(one["counts"]["s"], many["counts"]["s"])
    assert one["raster_total"] == many["raster_total"]


def test_windows_with_no_polygon_are_skipped(tmp_path, raster):
    zs = zone_set(tmp_path / "z.gpkg", "s", [box(10, 52.05, 14, 53)])      # only the top row
    r = sample_raster(raster, [zs], workers=1, window_cells=4)
    assert r["windows"] == 3 and r["windows_skipped"] == 2
    assert r["sums"]["s"][1] == 1 + 2 + 3 + 4


def test_two_scales_are_sampled_from_one_read(tmp_path, raster):
    fine = zone_set(tmp_path / "f.gpkg", "fine", [box(10, 50, 12, 53), box(12, 50, 14, 53)])
    coarse = zone_set(tmp_path / "c.gpkg", "coarse", [box(10, 50, 14, 53)])
    r = sample_raster(raster, [fine, coarse], workers=1)
    assert r["sums"]["coarse"][1] == r["sums"]["fine"][1:].sum() == 66


@pytest.mark.parametrize("dtype,nodata", [("Byte", 255), ("UInt16", 65535), ("Int16", -1)])
def test_other_data_types_and_nodata_values(tmp_path, dtype, nodata):
    rows = [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, nodata]]
    path = write_raster(tmp_path / "r.tif", rows, dtype=dtype, nodata=nodata)
    zs = zone_set(tmp_path / "z.gpkg", "s", [box(10, 50, 14, 53)])
    r = sample_raster(path, [zs], workers=1)
    assert r["sums"]["s"][1] == 66 and r["counts"]["s"][1] == 11


def test_float_raster_with_real_nan_pixels(tmp_path):
    """True NaN pixels (declared NoData = NaN) are skipped. Built from raw floats, because
    the ASCII grid reader turns the text 'nan' into 0."""
    arr = np.array([[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, np.nan]], dtype="<f4")
    raw = tmp_path / "nan.bin"
    arr.tofile(raw)
    raw.with_suffix(".hdr").write_text("ENVI\nsamples = 4\nlines = 3\nbands = 1\nheader offset = 0\n"
                                       "file type = ENVI Standard\ndata type = 4\ninterleave = bsq\nbyte order = 0\n")
    path = tmp_path / "r.tif"
    subprocess.run(["gdal_translate", "-q", "-of", "GTiff", "-a_srs", "EPSG:4326", "-a_ullr", "10", "53", "14", "50",
                    "-a_nodata", "nan", str(raw), str(path)], check=True)
    zs = zone_set(tmp_path / "z.gpkg", "s", [box(10, 50, 14, 53)])
    r = sample_raster(path, [zs], workers=1)
    assert r["sums"]["s"][1] == 66 and r["counts"]["s"][1] == 11 and r["raster_total"] == 66


def test_raster_that_is_not_4326_is_rejected(tmp_path):
    path = write_raster(tmp_path / "r.tif", VALUES, srs="EPSG:4674")
    zs = zone_set(tmp_path / "z.gpkg", "s", [box(10, 50, 14, 53)])
    with pytest.raises(ZonalError, match="EPSG:4326"):
        sample_raster(path, [zs])
