"""Zonal sums through PyQGIS (native:zonalstatisticsfb).

This script is the only place that imports QGIS. It is run by a Python
interpreter that has PyQGIS installed (the project's virtualenv does not),
configured as `[benchmarking] qgis_python`:

    /usr/bin/python3 stages/qgis_zonal.py job.json

job.json
    {
      "qgis_prefix": "/usr",
      "jobs": [
        {"scale": "tract",
         "input": "/path/db.gpkg|layername=setores|subset=substr(\\"setor_code\\",1,2) IN ('53')",
         "code_field": "setor_code", "name_field": null,
         "output_csv": "/path/zonal_tract.csv",
         "rasters": [{"name": "brazilpop_030", "path": "/path/raster.tif"}, ...]}
      ]
    }

For every job it writes one CSV: the zone code, an optional name and, per raster,
`pi_sum_<name>` (sum of the cell values) and `pi_count_<name>` (number of cells
counted). The rule is QGIS's: a cell belongs to the polygon that contains its
centre; NoData cells are skipped. Polygons are transformed to the raster CRS by QGIS.

Nothing here depends on the rest of the project, so it can be tested alone.
"""

from __future__ import annotations

import csv
import json
import os
import sys

STAT_COUNT, STAT_SUM = 0, 1            # QgsZonalStatistics.Statistic indices used by the algorithm
PREFIX = "pi_"                          # QGIS names the fields prefix + statistic: pi_count, pi_sum


def _init_qgis(prefix: str):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qgis.core import QgsApplication

    QgsApplication.setPrefixPath(prefix, True)
    app = QgsApplication([], False)
    app.initQgis()
    plugins = os.path.join(prefix, "share", "qgis", "python", "plugins")
    if plugins not in sys.path:
        sys.path.append(plugins)
    import processing
    from processing.core.Processing import Processing

    Processing.initialize()
    return app, processing


def run_job(processing, job: dict) -> dict:
    """Run every raster of one job and write its CSV. Returns a summary."""
    from qgis.core import QgsVectorLayer

    zones: dict[str, dict] = {}                     # code -> {"name": ..., column: value}
    columns: list[str] = []
    summary = {}

    for raster in job["rasters"]:
        res = processing.run("native:zonalstatisticsfb", {
            "INPUT": job["input"],
            "INPUT_RASTER": raster["path"],
            "RASTER_BAND": 1,
            "COLUMN_PREFIX": PREFIX,
            "STATISTICS": [STAT_COUNT, STAT_SUM],
            "OUTPUT": "memory:",
        })
        layer = res["OUTPUT"]
        if isinstance(layer, str):                   # a path, not a layer object
            layer = QgsVectorLayer(layer, "zonal", "ogr")
        names = layer.fields().names()
        for need in (job["code_field"], PREFIX + "sum", PREFIX + "count"):
            if need not in names:
                raise RuntimeError(f"zonal output has no field '{need}' (fields: {names})")

        sum_col, count_col = f"pi_sum_{raster['name']}", f"pi_count_{raster['name']}"
        columns += [sum_col, count_col]
        name_field = job.get("name_field")
        n = 0
        for feat in layer.getFeatures():
            code = feat[job["code_field"]]
            if code is None:
                continue
            z = zones.setdefault(str(code), {})
            if name_field and name_field in names:
                z["name"] = feat[name_field]
            z[sum_col] = feat[PREFIX + "sum"]
            z[count_col] = feat[PREFIX + "count"]
            n += 1
        summary[raster["name"]] = n

    os.makedirs(os.path.dirname(os.path.abspath(job["output_csv"])), exist_ok=True)
    with open(job["output_csv"], "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["code", "name"] + columns)
        for code in sorted(zones):
            z = zones[code]
            w.writerow([code, z.get("name", "")] + [z.get(c, "") for c in columns])
    return {"scale": job["scale"], "zones": len(zones), "rasters": summary, "output_csv": job["output_csv"]}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        spec = json.load(fh)
    app, processing = _init_qgis(spec.get("qgis_prefix", "/usr"))
    try:
        results = [run_job(processing, job) for job in spec["jobs"]]
        print(json.dumps({"results": results}))
    finally:
        app.exitQgis()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
