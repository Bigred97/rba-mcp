"""Pydantic v2 response models for rba-mcp.

Mirrors abs-mcp's `Observation` and `DataResponse` so a downstream agent that
uses both servers gets a uniform response shape. RBA-specific additions:
- TableSummary, TableDetail, SeriesDetail (replace abs-mcp's Dataset variants)
- DataResponse.attribution (CC-BY 4.0 compliance)
- DataResponse.source default = "Reserve Bank of Australia"
- DataResponse.rba_url (parallel to abs-mcp's abs_url)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


_RBA_ATTRIBUTION = (
    "Data sourced from the Reserve Bank of Australia and licensed under "
    "Creative Commons Attribution 4.0 International (CC BY 4.0). "
    "https://www.rba.gov.au/copyright/"
)


class TableSummary(BaseModel):
    id: str                                  # F-table code, e.g. "F11"
    name: str                                # human name, e.g. "Exchange Rates – Monthly"
    description: str | None = None
    frequency: str | None = None             # "Daily" / "Monthly" / mixed
    is_curated: bool = False


class SeriesDetail(BaseModel):
    key: str                                 # plain-English key (curated) or RBA series ID (non-curated)
    series_id: str                           # canonical RBA series ID, e.g. "FXRUSD"
    description: str | None = None
    unit: str | None = None
    frequency: str | None = None
    start_date: str | None = None            # earliest non-null observation, ISO date
    end_date: str | None = None              # latest non-null observation, ISO date (freshness signal)


class TableDetail(BaseModel):
    id: str
    name: str
    description: str
    is_curated: bool
    frequency: str | None = None
    series: list[SeriesDetail]
    source_url: str
    rba_url: str


class Observation(BaseModel):
    period: str                              # ISO date, "YYYY-MM-DD" / "YYYY-MM"
    value: float | None
    dimensions: dict[str, str]               # {series, table} for RBA — flat
    unit: str | None = None


class DataResponse(BaseModel):
    table_id: str
    table_name: str
    query: dict[str, Any] = Field(default_factory=dict)
    period: dict[str, str | None] = Field(default_factory=lambda: {"start": None, "end": None})
    unit: str | None = None
    records: list[Observation] | list[dict[str, Any]] = Field(default_factory=list)
    csv: str | None = None
    source: str = "Reserve Bank of Australia"
    attribution: str = _RBA_ATTRIBUTION
    retrieved_at: datetime
    rba_url: str
    # Echoed in every response so testers can verify which wheel served the
    # call — uvx caches per-version and stale caches have caused real "is
    # this fixed?" confusion. `pip install -U` / `uvx --refresh` to update.
    server_version: str = Field(default_factory=lambda: _get_server_version())
    # Set when the RBA CDN was unreachable and we served a cached payload
    # past its normal TTL. Agents should surface `stale=True` to end users
    # (e.g. "RBA CDN reported 503; showing data from 12 minutes ago").
    stale: bool = False
    stale_reason: str | None = None
    # Set when `latest()` truncated a large response to a limit. Original
    # row count goes here so agents can detect + surface the cap.
    truncated_at: int | None = None


def _get_server_version() -> str:
    try:
        from importlib.metadata import version
        return version("rba-mcp")
    except Exception:
        return "0.0.0+unknown"
