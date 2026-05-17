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
    # 0-100 RapidFuzz WRatio score against the search query (with the
    # curated + phrase bonuses already applied, capped at 100). None when
    # entry came from list_curated() rather than search_tables().
    relevance: float | None = None


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
    row_count: int = Field(default=0, description="Number of observation rows in records.")
    records: list[Observation] | list[dict[str, Any]] = Field(default_factory=list)
    csv: str | None = None
    source: str = "Reserve Bank of Australia"
    attribution: str = _RBA_ATTRIBUTION
    retrieved_at: datetime
    source_url: str = Field(
        description=(
            "Canonical click-through URL. Same value as rba_url; both populated "
            "for backward compat."
        )
    )
    rba_url: str = Field(
        description=(
            "Click-through URL for this table's source page. rba-mcp legacy "
            "name — prefer source_url (canonical) for new code. Both fields are "
            "populated identically."
        )
    )
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


class ReleaseEntry(BaseModel):
    """One upcoming RBA publication / event.

    Shared shape with `abs-mcp.ReleaseEntry` so the ausdata-api webhook
    poller doesn't need to branch per source. Note: `dataset_id` is the
    field name on this model for shape parity even though rba calls its
    curated identifiers `table_id` everywhere else — the field carries
    the F-table key when the release is an F-table refresh, else null.
    """
    release_at: datetime = Field(
        description=(
            "Publication time as an ISO-8601 timestamp with tz offset. "
            "RBA publishes in Sydney local time — offset moves between "
            "+10:00 (AEST) and +11:00 (AEDT) across DST."
        )
    )
    title: str = Field(
        description=(
            "Publication or event name as the RBA labels it, e.g. "
            "'Statement on Monetary Policy', 'Financial Aggregates', "
            "'Minutes of Monetary Policy Meeting'."
        )
    )
    event_type: str = Field(
        default="data_release",
        description=(
            "One of 'data_release' (regular statistical publication), "
            "'policy_decision' (cash-rate decision; separate from F1.1 "
            "data which lags), 'statement' (Statement on Monetary Policy, "
            "Minutes, Financial Stability Review, Bulletin), or 'speech'."
        ),
    )
    dataset_id: str | None = Field(
        default=None,
        description=(
            "Curated rba-mcp F-table key when the release refreshes one "
            "(e.g. 'D1' for Financial Aggregates). Null for releases "
            "that don't map to a curated table."
        ),
    )
    publication_id: str | None = Field(
        default=None,
        description=(
            "Stable RBA publication identifier — table letter+number "
            "(e.g. 'A1', 'D1', 'F11.1') or 'SMP' / 'MINUTES' / 'BULLETIN' "
            "for narrative releases. Useful for deduplication."
        ),
    )
    source_url: str = Field(
        description="Canonical click-through URL on rba.gov.au."
    )
    reference_period: str | None = Field(
        default=None,
        description=(
            "The reporting period the release covers, where surfaced "
            "(e.g. 'March 2026', 'May 2026'). Often null on the RBA "
            "schedule — the URL has it but the calendar page itself "
            "doesn't carry it as a structured field."
        ),
    )


class ReleaseCalendarResponse(BaseModel):
    """Upcoming RBA publications + events over a rolling horizon.

    Shape mirrors `abs-mcp.ReleaseCalendarResponse` so the gateway can
    diff/route both sources through the same code path.
    """
    horizon_days: int
    row_count: int
    releases: list[ReleaseEntry] = Field(default_factory=list)
    source: str = "Reserve Bank of Australia"
    source_url: str = "https://www.rba.gov.au/schedules-events/"
    attribution: str = _RBA_ATTRIBUTION
    retrieved_at: datetime
    server_version: str = Field(default_factory=lambda: _get_server_version())
    stale: bool = False
    stale_reason: str | None = None


def _get_server_version() -> str:
    try:
        from importlib.metadata import version
        return version("rba-mcp")
    except Exception:
        return "0.0.0+unknown"
