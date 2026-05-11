"""FastMCP server entrypoint for rba-mcp.

Five tools, all thin orchestrators over `client`, `parsing`, `tables`,
`curated`, and `shaping`. The shared `RBAClient` is created lazily so
importing this module doesn't open the SQLite cache.

Validation guards mirror abs-mcp 0.2.x: explicit input-type checks, URL-safe
patterns for table IDs / series IDs / period strings, helpful error messages
with "Try X" hints.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any, Literal

from fastmcp import FastMCP

from . import curated, tables
from .client import RBAAPIError, RBAClient
from .models import (
    DataResponse,
    SeriesDetail,
    TableDetail,
    TableSummary,
)
from .parsing import filter_by_dates, filter_by_series, parse_csv
from .shaping import build_response

# F-table IDs are uppercase letters + digits + dot (e.g. F1.1, F11.1).
_TABLE_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9.]*$")
# RBA series IDs are uppercase letters + digits (e.g. FXRUSD, FIRMMCRT, FLRHOOTL).
_SERIES_ID_PATTERN = re.compile(r"^[A-Z0-9_-]+$")
# Period strings: YYYY, YYYY-MM, YYYY-MM-DD (lenient digits/dash check).
_PERIOD_PATTERN = re.compile(r"^[0-9-]{4,10}$")
_VALID_FORMATS = {"records", "series", "csv"}

mcp = FastMCP("rba-mcp")

_client: RBAClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> RBAClient:
    global _client
    async with _client_lock:
        if _client is None:
            _client = RBAClient()
        return _client


async def reset_client_for_tests() -> None:
    """Drop the cached client. Tests that span event loops must clear it."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None


def _rba_url(table_id: str) -> str:
    return f"https://www.rba.gov.au/statistics/tables/#{table_id.lower()}"


def _normalize_table_id(table_id: Any) -> str:
    if not isinstance(table_id, str):
        raise ValueError(
            f"table_id must be a string, got {type(table_id).__name__}. "
            "Try search_tables() to discover IDs like 'F11', 'F1.1', or 'F6'."
        )
    normalized = table_id.strip().upper()
    if not normalized:
        raise ValueError(
            "table_id is empty. Try search_tables() to discover IDs like 'F11', 'F1.1', or 'F6'."
        )
    if not _TABLE_ID_PATTERN.match(normalized):
        raise ValueError(
            f"table_id {table_id!r} contains invalid characters — "
            "F-table IDs use uppercase letters, digits, and dots (e.g. 'F11', 'F1.1'). "
            "Try search_tables() to discover valid IDs."
        )
    return normalized


def _validate_series(series: Any) -> str | list[str] | None:
    if series is None:
        return None
    if isinstance(series, str):
        s = series.strip()
        if not s:
            raise ValueError(
                "series is an empty string. Pass a curated key like 'aud_usd' or 'cash_rate_target', "
                "or omit `series` to query all curated series."
            )
        return s
    if isinstance(series, list):
        if not series:
            raise ValueError(
                "series is an empty list. Pass at least one series, "
                "or omit `series` to query all curated series."
            )
        out: list[str] = []
        for s in series:
            if not isinstance(s, str):
                raise ValueError(
                    f"series list entries must be strings, got {type(s).__name__}."
                )
            stripped = s.strip()
            if not stripped:
                raise ValueError("series list contains an empty string.")
            out.append(stripped)
        return out
    raise ValueError(
        f"series must be a string or list of strings, got {type(series).__name__}."
    )


def _validate_period(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            f"{field_name} must be a string in 'YYYY', 'YYYY-MM', or 'YYYY-MM-DD' format, "
            f"got {type(value).__name__}."
        )
    s = value.strip()
    if not s:
        return None
    if not _PERIOD_PATTERN.match(s):
        raise ValueError(
            f"{field_name} {value!r} has invalid format. "
            "Use 'YYYY', 'YYYY-MM', or 'YYYY-MM-DD' (digits + dashes only)."
        )
    # Semantic check: the regex accepts shapes like '2024-13', '----', '00-00'
    # that aren't real dates. Reject them here so the user gets a clean error
    # instead of silently unfiltered data (filter_by_dates is lenient on
    # purpose — that's the cache-safe behaviour for legitimate edge cases
    # like 'BCE' future dates, not for typos).
    if not _is_valid_period(s):
        raise ValueError(
            f"{field_name} {value!r} is not a valid date. "
            "Use 'YYYY' (e.g. '2024'), 'YYYY-MM' (e.g. '2024-03'), or "
            "'YYYY-MM-DD' (e.g. '2024-03-15')."
        )
    return s


def _is_valid_period(s: str) -> bool:
    """True iff s parses as YYYY, YYYY-MM, or YYYY-MM-DD."""
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            datetime.strptime(s, fmt)
            return True
        except ValueError:
            continue
    return False


def _validate_series_for_url(series_ids: list[str]) -> None:
    """Raw RBA series IDs flow into the URL via the cache key — validate shape."""
    for sid in series_ids:
        if not _SERIES_ID_PATTERN.match(sid):
            raise ValueError(
                f"Series ID {sid!r} contains invalid characters — "
                "RBA series IDs use only letters, digits, underscores, and hyphens "
                "(e.g. 'FXRUSD', 'FIRMMCRT')."
            )


@mcp.tool
async def search_tables(query: str, limit: int = 10) -> list[TableSummary]:
    """Fuzzy-search RBA F-tables by name and topic.

    Returns the top matching tables ranked by relevance. Use this when you
    don't know the exact table ID — for example, search "cash rate", "aud
    usd", "mortgage rates", or "term deposits".
    """
    if not isinstance(query, str):
        raise ValueError(
            f"query must be a string, got {type(query).__name__}. "
            "Try 'cash rate', 'aud usd', 'mortgage', or any other RBA topic."
        )
    if not query.strip():
        raise ValueError(
            "query is required. Try 'cash rate', 'aud usd', 'mortgage', "
            "'term deposits', or any other RBA topic."
        )
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError(
            f"limit must be a positive integer, got {limit!r} ({type(limit).__name__})."
        )
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}.")
    return tables.search_tables(query, limit=limit)


@mcp.tool
async def describe_table(table_id: str) -> TableDetail:
    """Describe an RBA F-table's series, units, and frequency.

    For curated tables (F1.1, F4, F6, F11, F11.1), returns plain-English
    series keys (like 'cash_rate_target', 'aud_usd') with descriptions.
    For other tables, returns raw RBA series IDs from the CSV header.
    """
    table_id = _normalize_table_id(table_id)
    summary = tables.get_table(table_id)
    if summary is None:
        raise ValueError(
            f"Table {table_id!r} is not a known RBA F-table. "
            "Try search_tables() to discover valid IDs (e.g. 'F11', 'F1.1', 'F6')."
        )

    csv_filename = tables.get_csv_filename(table_id)
    if csv_filename is None:
        raise ValueError(
            f"Internal registry inconsistency: table {table_id} has no csv_filename. "
            "Report at https://github.com/Bigred97/rba-mcp/issues."
        )
    rba_url = _rba_url(table_id)
    cd = curated.get(table_id)

    if cd is not None:
        series_list = [
            SeriesDetail(
                key=key,
                series_id=cs.series_id,
                description=cs.description,
                unit=cs.unit,
                frequency=summary.frequency,
            )
            for key, cs in cd.series.items()
        ]
        description = cd.description
        is_curated = True
    else:
        # Fetch the CSV to discover the actual series + their metadata
        client = await _get_client()
        try:
            body = await client.fetch_table_csv(csv_filename)
        except RBAAPIError as e:
            raise ValueError(
                f"Could not fetch RBA table {table_id} ({csv_filename}). ({e})"
            ) from e
        header, df = parse_csv(body)
        series_list = []
        for sid, meta in header.series.items():
            start_date = None
            if sid in df.columns:
                first_valid = df[sid].first_valid_index()
                if first_valid is not None:
                    start_date = first_valid.strftime("%Y-%m-%d")
            series_list.append(
                SeriesDetail(
                    key=sid,
                    series_id=sid,
                    description=meta.name,
                    unit=meta.unit,
                    frequency=meta.frequency,
                    start_date=start_date,
                )
            )
        description = (
            f"{summary.name}. {len(series_list)} series. "
            "Pass raw RBA series IDs (e.g. 'FIRMMCRT') to get_data."
        )
        is_curated = False

    return TableDetail(
        id=table_id,
        name=summary.name,
        description=description,
        is_curated=is_curated,
        frequency=summary.frequency,
        series=series_list,
        source_url="https://www.rba.gov.au/statistics/tables/",
        rba_url=rba_url,
    )


async def _get_data_impl(
    table_id: str,
    series: Any,
    start_date: Any,
    end_date: Any,
    fmt: str,
    last_n: int | None = None,
) -> DataResponse:
    table_id = _normalize_table_id(table_id)
    series_validated = _validate_series(series)
    start_date_validated = _validate_period(start_date, "start_date")
    end_date_validated = _validate_period(end_date, "end_date")
    fmt_norm = (fmt or "records").lower()
    if fmt_norm not in _VALID_FORMATS:
        raise ValueError(
            f"Unknown format {fmt!r}. Valid options: {sorted(_VALID_FORMATS)}"
        )
    if start_date_validated and end_date_validated and start_date_validated > end_date_validated:
        raise ValueError(
            f"end_date ({end_date_validated}) is before start_date ({start_date_validated}). "
            "Try swapping them, or use 'YYYY', 'YYYY-MM', or 'YYYY-MM-DD' format."
        )

    summary = tables.get_table(table_id)
    if summary is None:
        raise ValueError(
            f"Table {table_id!r} is not a known RBA F-table. "
            "Try search_tables() to discover valid IDs."
        )
    csv_filename = tables.get_csv_filename(table_id)
    if csv_filename is None:
        raise ValueError(
            f"Internal registry inconsistency: table {table_id} has no csv_filename. "
            "Report at https://github.com/Bigred97/rba-mcp/issues."
        )
    rba_url = _rba_url(table_id)

    cd = curated.get(table_id)
    user_query: dict[str, Any] = {}
    if series_validated is not None:
        user_query["series"] = series_validated
    if start_date_validated:
        user_query["start_date"] = start_date_validated
    if end_date_validated:
        user_query["end_date"] = end_date_validated

    # Translate series to canonical RBA IDs.
    if cd is not None:
        try:
            series_ids = curated.translate_series(cd, series_validated)
        except ValueError:
            raise
    else:
        # Non-curated: only raw IDs accepted; no defaulting.
        if series_validated is None:
            raise ValueError(
                f"Table {table_id} is not curated; you must specify which series. "
                "Call describe_table() first to see the available raw RBA series IDs."
            )
        series_ids = (
            [series_validated]
            if isinstance(series_validated, str)
            else list(series_validated)
        )
    _validate_series_for_url(series_ids)

    client = await _get_client()
    cache_kind = "latest" if last_n == 1 else "data"
    try:
        body = await client.fetch_table_csv(csv_filename, kind=cache_kind)
    except RBAAPIError as e:
        raise ValueError(
            f"Could not fetch RBA table {table_id} ({csv_filename}). ({e})"
        ) from e

    header, df = parse_csv(body)
    df = filter_by_series(df, series_ids)
    df = filter_by_dates(df, start_date_validated, end_date_validated)

    if last_n is not None and not df.empty:
        df = df.tail(last_n)

    return build_response(
        table_id=table_id,
        table_name=summary.name,
        df=df,
        header=header,
        curated=cd,
        fmt=fmt_norm,
        user_query=user_query,
        rba_url=rba_url,
        start_date=start_date_validated,
        end_date=end_date_validated,
    )


@mcp.tool
async def get_data(
    table_id: str,
    series: str | list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    format: Literal["records", "series", "csv"] = "records",
) -> DataResponse:
    """Query an RBA F-table.

    For curated tables, `series` accepts plain-English keys (e.g. `"aud_usd"`,
    `"cash_rate_target"`). For other tables, pass raw RBA series IDs.
    Pass `series=None` to query all curated series for that table.

    `start_date` / `end_date` accept 'YYYY-MM-DD', 'YYYY-MM', or 'YYYY'.
    `format`: 'records' (default; flat list), 'series' (grouped), or 'csv'.
    """
    return await _get_data_impl(table_id, series, start_date, end_date, format)


@mcp.tool
async def latest(
    table_id: str,
    series: str | list[str] | None = None,
) -> DataResponse:
    """Return the most recent observation for each series in an RBA F-table.

    Pass `series=None` to get the latest observation for every curated
    series in the table (e.g. `latest("F11.1")` returns latest AUD/USD,
    AUD/EUR, etc. all in one response).
    """
    return await _get_data_impl(table_id, series, None, None, "records", last_n=1)


@mcp.tool
def list_curated() -> list[str]:
    """List the RBA F-table IDs that have hand-curated plain-English support."""
    return curated.list_ids()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
