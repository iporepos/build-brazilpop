"""CNEFE 2010 adapter (Stage 1).

Raw files are the harmonized 2010 address tables (Parquet, one per UF) whose species and
typology are already coded as in 2022 (`especie_2022`, `tipo_domicilio_2022`) and whose
points are placed per row (`lon`, `lat`).
All the work is in stages/adapters/tabular.py; this file only says which census
year it serves. Columns, paths and the format are declared in [ingestion.2010].
"""

from __future__ import annotations

from stages.adapters.tabular import UnknownCodeError, ingest as _ingest

YEAR = 2010

__all__ = ["YEAR", "UnknownCodeError", "ingest"]


def ingest(cfg: dict, uf: str, *, overwrite: bool | None = None) -> dict:
    """Standardize one UF. Returns a report dict."""
    return _ingest(cfg, uf, YEAR, overwrite=overwrite)
