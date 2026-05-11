"""Shape parsed RBA data into the response models exposed to MCP clients.

Three output formats:
  - records: flat list of {period, value, dimensions={series,table}, unit}
  - series: grouped by series, each with an inner observation list
  - csv: pandas to_csv()

Simpler than abs-mcp's shaping because RBA data is already tabular — no
SDMX codelist remap, no hidden-dim filtering. We map each (date, series)
cell to one Observation.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .curated import CuratedTable
from .models import DataResponse, Observation
from .parsing import TableHeader


def _safe_value(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


def _series_display(
    sid: str, header: TableHeader, curated: CuratedTable | None
) -> tuple[str, str | None]:
    """Pick the human-friendly display name + unit override for a series.

    Curated wins (matches the user's requested key when possible); fall back
    to header metadata; fall back to the series ID itself.
    """
    if curated is not None:
        for key, c_series in curated.series.items():
            if c_series.series_id == sid:
                meta = header.series.get(sid)
                unit = c_series.unit or (meta.unit if meta else None)
                # Prefer the curated description for clarity; fall back to plain key.
                display = c_series.description or key
                return display, unit
    meta = header.series.get(sid)
    if meta is None:
        return sid, None
    display = meta.name or sid
    return display, meta.unit


def to_records(
    df: pd.DataFrame,
    header: TableHeader,
    table_id: str,
    curated: CuratedTable | None = None,
) -> list[Observation]:
    """One Observation per non-null (date, series) cell."""
    if df.empty:
        return []
    series_meta = {sid: _series_display(sid, header, curated) for sid in df.columns}
    records: list[Observation] = []
    # Iterate column-major so all observations of one series are grouped.
    # That gives the LLM a more intuitive ordering when multiple series queried.
    for sid in df.columns:
        display, unit = series_meta[sid]
        col = df[sid]
        for date_idx, raw in col.items():
            value = _safe_value(raw)
            if value is None:
                continue
            period = date_idx.strftime("%Y-%m-%d") if hasattr(date_idx, "strftime") else str(date_idx)
            records.append(
                Observation(
                    period=period,
                    value=value,
                    dimensions={"series": display, "table": table_id},
                    unit=unit,
                )
            )
    return records


def to_csv(df: pd.DataFrame) -> str:
    if df.empty:
        return ""
    return df.to_csv()


def to_series(records: list[Observation]) -> list[dict[str, Any]]:
    """Group records by series — `[{series, table, observations:[{period,value}]}]`."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in records:
        key = (r.dimensions.get("series", ""), r.dimensions.get("table", ""))
        groups.setdefault(key, []).append({"period": r.period, "value": r.value})
    out: list[dict[str, Any]] = []
    for (series_name, table_name), obs_list in groups.items():
        out.append({
            "series": series_name,
            "table": table_name,
            "observations": obs_list,
        })
    return out


def build_response(
    table_id: str,
    table_name: str,
    df: pd.DataFrame,
    header: TableHeader,
    curated: CuratedTable | None,
    fmt: str,
    user_query: dict[str, Any],
    rba_url: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> DataResponse:
    if fmt == "csv":
        records: list[Observation] | list[dict[str, Any]] = []
        csv_text: str | None = to_csv(df)
        underlying = to_records(df, header, table_id, curated=curated)
    elif fmt == "series":
        underlying = to_records(df, header, table_id, curated=curated)
        records = to_series(underlying)
        csv_text = None
    else:  # records
        records = to_records(df, header, table_id, curated=curated)
        underlying = records  # type: ignore[assignment]
        csv_text = None

    response_unit: str | None = None
    if underlying:
        units = {o.unit for o in underlying if o.unit}  # type: ignore[union-attr]
        if len(units) == 1:
            response_unit = next(iter(units))

    if (start_date is None or end_date is None) and underlying:
        periods = sorted({o.period for o in underlying if o.period})  # type: ignore[union-attr]
        start_date = start_date or (periods[0] if periods else None)
        end_date = end_date or (periods[-1] if periods else None)

    return DataResponse(
        table_id=table_id,
        table_name=table_name,
        query=user_query,
        period={"start": start_date, "end": end_date},
        unit=response_unit,
        records=records,
        csv=csv_text,
        retrieved_at=datetime.now(timezone.utc),
        rba_url=rba_url,
    )
