"""CNEFE 2022 adapter (Stage 1).

Raw files are delimited text, one per UF, with the IBGE 2022 columns.
All the work is in stages/adapters/tabular.py; this file only says which census
year it serves. Columns, paths and the format are declared in [ingestion.2022].
"""

from __future__ import annotations

from stages.adapters.tabular import UnknownCodeError, ingest as _ingest

YEAR = 2022

__all__ = ["YEAR", "UnknownCodeError", "ingest"]


def ingest(cfg: dict, uf: str, *, overwrite: bool | None = None) -> dict:
    """Standardize one UF. Returns a report dict."""
    return _ingest(cfg, uf, YEAR, overwrite=overwrite)
