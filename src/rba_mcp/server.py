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
import difflib
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from . import curated, tables
from .client import RBAAPIError, RBAClient, get_stale_signal, reset_stale_signal
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
                "or omit `series` to use the table's headline series."
            )
        return s
    if isinstance(series, list):
        if not series:
            raise ValueError(
                "series is an empty list. Pass at least one series, "
                "or omit `series` to use the table's headline series."
            )
        out: list[str] = []
        for s in series:
            if not isinstance(s, str):
                raise ValueError(
                    f"series list entries must be strings, got {type(s).__name__}. "
                    "Each entry should be a curated key (e.g. 'aud_usd') or a raw RBA "
                    "series ID (e.g. 'FXRUSD'). "
                    "Try describe_table('F11.1') to see valid keys for a curated table."
                )
            stripped = s.strip()
            if not stripped:
                raise ValueError(
                    "series list contains an empty string. "
                    "Each entry should be a non-empty curated key like 'aud_usd' or a "
                    "raw RBA series ID like 'FXRUSD'. "
                    "Try describe_table('<table_id>') to list valid series."
                )
            out.append(stripped)
        return out
    raise ValueError(
        f"series must be a string or list of strings, got {type(series).__name__}. "
        "Pass a single key (e.g. 'aud_usd'), a list of keys "
        "(e.g. ['aud_usd', 'aud_eur']), or omit to use the table's headline series. "
        "See describe_table('<table_id>') for valid keys."
    )


def _validate_period(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    # MCP / LLM clients often send a year as a JSON number rather than a
    # string (`start_date=2024` instead of `start_date="2024"`). Coerce
    # int → str so both forms work. Excludes bool (which subclasses int)
    # to keep `True`/`False` rejected as type errors.
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
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


def _known_series_ids() -> list[str]:
    """All canonical RBA series IDs known across curated tables (for "Did you mean?")."""
    known: set[str] = set()
    for cid in curated.list_ids():
        ct = curated.get(cid)
        if ct is None:
            continue
        for s in ct.series.values():
            known.add(s.series_id)
    return sorted(known)


def _validate_series_for_url(series_ids: list[str]) -> None:
    """Raw RBA series IDs flow into the URL via the cache key — validate shape."""
    for sid in series_ids:
        if not _SERIES_ID_PATTERN.match(sid):
            hint = (
                f"Series ID {sid!r} contains invalid characters. "
                "RBA series IDs are uppercase letters + digits, optionally with "
                "underscores or hyphens (e.g. 'FXRUSD', 'FIRMMCRT', 'FLRHOOVA')."
            )
            # Best-effort "Did you mean?" against known curated series IDs —
            # cheap (~30 IDs) and harmless when there's no close match.
            try:
                candidates = _known_series_ids()
                close = difflib.get_close_matches(
                    sid.upper(), candidates, n=1, cutoff=0.6
                )
                if close:
                    hint += f" Did you mean '{close[0]}'?"
            except Exception:
                pass
            hint += (
                " Try describe_table('F11.1') (FX) or describe_table('F1.1') "
                "(money market) to see valid series IDs for a table."
            )
            raise ValueError(hint)


@mcp.tool
async def search_tables(
    query: Annotated[
        str,
        Field(
            description=(
                "Free-text search query. Matches against F-table IDs, names, "
                "and topic keywords. Case-insensitive."
            ),
            examples=["cash rate", "aud usd", "mortgage rates", "term deposits", "yield curve"],
        ),
    ],
    limit: Annotated[
        int,
        Field(
            description="Maximum number of results to return, ranked by relevance.",
            examples=[5, 10, 20],
            ge=1,
            le=100,
        ),
    ] = 10,
) -> list[TableSummary]:
    """Fuzzy-search RBA F-tables by name and topic.

    Use this when you don't know the exact table ID. The 5 curated F-tables
    (F1.1, F4, F6, F11, F11.1) cover the most-asked indicators: cash rate,
    money-market rates, household lending rates, FX rates.

    Examples:
        # Find the F-table that publishes the cash rate
        results = await search_tables("cash rate")
        # → [{id: 'F1.1', name: 'Interest Rates and Yields - Money Market', ...}]

        # Discover what's available on FX
        results = await search_tables("aud usd", limit=5)
        # → top 5 FX-related tables, curated F11/F11.1 first

    When to use:
        - You have a natural-language question and need to identify the table
        - You want to discover what RBA publishes on a topic
        - You're enumerating the F-table catalog programmatically

    Returns:
        List of TableSummary (id, name, frequency, description), ranked
        by relevance. Curated tables surface above the rest.
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
            f"limit must be a positive integer, got {limit!r} ({type(limit).__name__}). "
            "Try limit=5 for a short list, limit=10 for the default, or limit=20 "
            "for broader exploration (max 100)."
        )
    if limit < 1:
        raise ValueError(
            f"limit must be >= 1, got {limit}. "
            "Try limit=5 for a short list or limit=10 for the default (max 100)."
        )
    return tables.search_tables(query, limit=limit)


@mcp.tool
async def describe_table(
    table_id: Annotated[
        str,
        Field(
            description=(
                "RBA F-table ID like 'F1.1', 'F11', 'F6'. Use search_tables() "
                "to discover or list_curated() to enumerate the 5 plain-English "
                "tables. Case-insensitive ('f11' resolves to 'F11')."
            ),
            examples=["F1.1", "F4", "F6", "F11", "F11.1"],
        ),
    ],
) -> TableDetail:
    """Describe an RBA F-table's series, units, and frequency.

    For curated F-tables (F1.1, F4, F6, F11, F11.1), returns plain-English
    series keys (like 'cash_rate_target', 'aud_usd') with descriptions and
    units. For other F-tables, fetches the CSV and returns the raw RBA
    series IDs from the header along with start dates.

    Examples:
        # Curated table — plain-English keys
        detail = await describe_table("F1.1")
        # detail.series[0]: key='cash_rate_target', series_id='FIRMMCRT',
        #   unit='Per cent per annum', frequency='Daily'

        # Curated FX table
        detail = await describe_table("F11.1")
        # detail.series has 'aud_usd', 'aud_eur', 'aud_jpy', 'aud_cny', etc.

    When to use:
        - Before calling get_data on a new table — to discover valid series keys
        - To get the canonical RBA source URL for citation
        - To distinguish curated (plain-English) tables from raw F-tables

    Returns:
        TableDetail with id, name, description, is_curated flag, frequency,
        list of SeriesDetail (key, series_id, description, unit), and rba_url.
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
        # Fetch the CSV so we can populate `start_date` per series — the
        # earliest non-null observation. The non-curated branch did this
        # already; the curated branch used to skip it (start_date was
        # always null in describe_table for curated tables, hiding info
        # the LLM needs to pick a sensible date range — 0.1.8 fix).
        client = await _get_client()
        try:
            body = await client.fetch_table_csv(csv_filename)
        except RBAAPIError as e:
            raise ValueError(
                f"Could not fetch RBA table {table_id} ({csv_filename}) from "
                f"www.rba.gov.au. ({e}) "
                "Try again in a moment — the RBA CDN occasionally rate-limits. "
                f"Confirm the table is published at {rba_url} or try "
                "list_curated() for a known-good table ID."
            ) from e
        _, df = parse_csv(body)

        series_list = []
        for key, cs in cd.series.items():
            start_date = None
            end_date = None
            if cs.series_id in df.columns:
                col = df[cs.series_id]
                first_valid = col.first_valid_index()
                if first_valid is not None:
                    start_date = first_valid.strftime("%Y-%m-%d")
                last_valid = col.last_valid_index()
                if last_valid is not None:
                    end_date = last_valid.strftime("%Y-%m-%d")
            series_list.append(
                SeriesDetail(
                    key=key,
                    series_id=cs.series_id,
                    description=cs.description,
                    unit=cs.unit,
                    frequency=summary.frequency,
                    start_date=start_date,
                    end_date=end_date,
                )
            )
        description = cd.description
        is_curated = True
    else:
        # Fetch the CSV to discover the actual series + their metadata
        client = await _get_client()
        try:
            body = await client.fetch_table_csv(csv_filename)
        except RBAAPIError as e:
            raise ValueError(
                f"Could not fetch RBA table {table_id} ({csv_filename}) from "
                f"www.rba.gov.au. ({e}) "
                "Try again in a moment — the RBA CDN occasionally rate-limits. "
                f"Confirm the table is published at {rba_url} or try "
                "list_curated() for a known-good curated table ID."
            ) from e
        header, df = parse_csv(body)
        series_list = []
        for sid, meta in header.series.items():
            start_date = None
            end_date = None
            if sid in df.columns:
                col = df[sid]
                first_valid = col.first_valid_index()
                if first_valid is not None:
                    start_date = first_valid.strftime("%Y-%m-%d")
                last_valid = col.last_valid_index()
                if last_valid is not None:
                    end_date = last_valid.strftime("%Y-%m-%d")
            series_list.append(
                SeriesDetail(
                    key=sid,
                    series_id=sid,
                    description=meta.name,
                    unit=meta.unit,
                    frequency=meta.frequency,
                    start_date=start_date,
                    end_date=end_date,
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


def _resolve_period_alias(
    canonical: Any,
    legacy: Any,
    canonical_name: str,
    legacy_name: str,
) -> Any:
    """Resolve the new canonical period parameter against the legacy alias.

    Portfolio interoperability (Wave 4): rba-mcp historically used
    `start_date` / `end_date`; the portfolio standard (7 of 9 sisters) is
    `start_period` / `end_period`. Both names are accepted for backward
    compatibility — but supplying both with non-None values is ambiguous
    and rejected with a "Use either X or Y, not both" hint.
    """
    if canonical is not None and legacy is not None:
        raise ValueError(
            f"Use either {canonical_name} or {legacy_name}, not both. "
            f"Got {canonical_name}={canonical!r} and {legacy_name}={legacy!r}. "
            f"{canonical_name} is the portfolio-standard name; {legacy_name} "
            "is retained as a legacy alias."
        )
    return canonical if canonical is not None else legacy


async def _get_data_impl(
    table_id: str,
    series: Any,
    start_date: Any,
    end_date: Any,
    fmt: str,
    last_n: int | None = None,
) -> DataResponse:
    # Reset the graceful-degradation flag at the start of each tool call so
    # we only report staleness introduced by THIS call's fetches.
    reset_stale_signal()
    table_id = _normalize_table_id(table_id)
    series_validated = _validate_series(series)
    # `_validate_period` keeps the legacy `start_date` / `end_date` field
    # names in its error messages — the caller has already resolved aliasing
    # in `get_data` / `latest` so we don't double-warn here.
    start_date_validated = _validate_period(start_date, "start_date")
    end_date_validated = _validate_period(end_date, "end_date")
    # Bug-fix (0.1.6): type-check `fmt` BEFORE coercing. Previously
    # `(fmt or "records").lower()` ran first; `format=42` crashed with
    # AttributeError on .lower() before the validation guard below could
    # reject it cleanly. Now: non-string fmt errors with a useful hint.
    if fmt is None:
        fmt_norm = "records"
    elif isinstance(fmt, str):
        fmt_norm = fmt.lower()
    else:
        raise ValueError(
            f"format must be a string, got {type(fmt).__name__}. "
            f"Valid options: {sorted(_VALID_FORMATS)}"
        )
    if fmt_norm not in _VALID_FORMATS:
        msg = f"Unknown format {fmt!r}. Valid options: {sorted(_VALID_FORMATS)}."
        close = difflib.get_close_matches(
            fmt_norm, sorted(_VALID_FORMATS), n=1, cutoff=0.5
        )
        if close:
            msg += f" Did you mean {close[0]!r}?"
        raise ValueError(msg)
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

    # Bug-fix (0.1.4): dedupe series_ids while preserving order. Without this,
    # passing both a curated key and its underlying raw ID (e.g.
    # `["aud_usd", "FXRUSD"]`) produced duplicate columns in the
    # filtered DataFrame, which then made `df[sid]` return a DataFrame
    # instead of a Series — the period field got polluted with the
    # series ID and N duplicate records came out.
    seen: set[str] = set()
    series_ids = [s for s in series_ids if not (s in seen or seen.add(s))]

    _validate_series_for_url(series_ids)

    client = await _get_client()
    cache_kind = "latest" if last_n == 1 else "data"
    try:
        body = await client.fetch_table_csv(csv_filename, kind=cache_kind)
    except RBAAPIError as e:
        raise ValueError(
            f"Could not fetch RBA table {table_id} ({csv_filename}) from "
            f"www.rba.gov.au. ({e}) "
            "Try again in a moment — the RBA CDN occasionally rate-limits. "
            f"Confirm the table is published at {rba_url} or try "
            "list_curated() for a known-good curated table ID."
        ) from e

    header, df = parse_csv(body)

    # Bug-fix (0.1.4): for non-curated tables, validate requested series
    # against the actual CSV header. Previously a typo silently produced
    # an empty result that looked indistinguishable from "no data in range".
    if cd is None:
        unknown = [sid for sid in series_ids if sid not in header.series]
        if unknown:
            valid = sorted(header.series.keys())
            hint = ", ".join(valid[:10]) + ("..." if len(valid) > 10 else "")
            raise ValueError(
                f"Unknown series {unknown} for non-curated table '{table_id}'. "
                f"Valid series IDs from the CSV header: {hint}. "
                f"Call describe_table('{table_id}') to see the full list."
            )

    df = filter_by_series(df, series_ids)
    df = filter_by_dates(df, start_date_validated, end_date_validated)

    if last_n is not None and not df.empty:
        # Drop trailing rows where every selected series is null before
        # taking the tail. RBA tables sometimes carry forward-dated empty
        # rows (e.g. G3 has future quarterly periods that backward-looking
        # series like consumer expectations don't populate). Without this,
        # latest() would return an all-null row for those series.
        df = df.dropna(how="all")
        df = df.tail(last_n)

    resp = build_response(
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
    # If the fetch served a stale-cache fallback because the upstream CDN
    # was unreachable, propagate it to the response.
    stale, reason = get_stale_signal()
    if stale:
        resp.stale = True
        resp.stale_reason = reason
    return resp


@mcp.tool
async def get_data(
    table_id: Annotated[
        str,
        Field(
            description="RBA F-table ID like 'F1.1', 'F11'. Use search_tables() to discover.",
            examples=["F1.1", "F11", "F6", "F4"],
        ),
    ],
    series: Annotated[
        str | list[str] | None,
        Field(
            description=(
                "Which series to return. For curated tables: plain-English keys "
                "(e.g. 'aud_usd', 'cash_rate_target') or a list for multi-series. "
                "For raw F-tables: raw RBA series IDs (e.g. 'FXRUSD'). "
                "Pass None (default) to use the table's headline series — e.g. F1.1 "
                "defaults to the cash rate target, F11/F11.1 to AUD/USD, F6 to the "
                "owner-occupier outstanding variable rate. Pass an explicit list to "
                "fetch multiple series."
            ),
            examples=[
                "cash_rate_target",
                "aud_usd",
                ["aud_usd", "aud_eur", "aud_jpy"],
                "FXRUSD",
            ],
        ),
    ] = None,
    start_period: Annotated[
        str | int | None,
        Field(
            description=(
                "Inclusive start period (portfolio-standard name). Accepts "
                "'YYYY', 'YYYY-MM', or 'YYYY-MM-DD'. An int year (e.g. 2024) "
                "is also accepted and treated as 'YYYY'. Semantic-checked: "
                "'2024-13' or '----' rejected at the boundary. Mutually "
                "exclusive with the legacy `start_date` alias."
            ),
            examples=["2024", "2024-03", "2024-03-15", 2024],
        ),
    ] = None,
    end_period: Annotated[
        str | int | None,
        Field(
            description=(
                "Inclusive end period (portfolio-standard name). Same format "
                "as start_period. Mutually exclusive with the legacy "
                "`end_date` alias."
            ),
            examples=["2025", "2025-12", "2025-12-31", 2025],
        ),
    ] = None,
    start_date: Annotated[
        str | int | None,
        Field(
            description=(
                "Legacy alias for `start_period` — retained for backward "
                "compatibility (rba-mcp <= 0.2.x). Prefer `start_period` for "
                "cross-sister consistency. Same format and semantics as "
                "`start_period`. Supplying both raises ValueError."
            ),
            examples=["2024", "2024-03", "2024-03-15", 2024],
        ),
    ] = None,
    end_date: Annotated[
        str | int | None,
        Field(
            description=(
                "Legacy alias for `end_period` — retained for backward "
                "compatibility. Prefer `end_period` for cross-sister "
                "consistency. Supplying both raises ValueError."
            ),
            examples=["2025", "2025-12", "2025-12-31", 2025],
        ),
    ] = None,
    format: Annotated[
        Literal["records", "series", "csv"],
        Field(
            description=(
                "Response shape. 'records' (default): flat list of observations. "
                "'series': observations grouped by series_id. 'csv': returns "
                "the table as a CSV string in the `csv` field."
            ),
            examples=["records", "series", "csv"],
        ),
    ] = "records",
) -> DataResponse:
    """Query an RBA F-table and return observations.

    Curated tables accept plain-English series keys that map to canonical
    RBA series IDs server-side. Omit `series` to get the table's headline
    series (e.g. F1.1 → cash rate target, F11/F11.1 → AUD/USD, F6 → owner-
    occupier outstanding variable rate). Pass an explicit list for a multi-
    series query.

    Examples:
        # Cash rate target since 2020 (portfolio-standard name)
        resp = await get_data("F1.1", series="cash_rate_target", start_period="2020")
        # → resp.records[0]: period='2020-01-01', value=0.25, series='cash_rate_target'

        # Headline default — no series arg returns the table's canonical series
        resp = await get_data("F11.1", start_period="2024-01-01", end_period="2024-12-31")
        # → resp.records: AUD/USD daily (the headline) for the period

        # Multiple FX rates — pass an explicit list
        resp = await get_data(
            "F11.1",
            series=["aud_usd", "aud_eur", "aud_jpy"],
            start_period="2024-01-01",
            end_period="2024-12-31",
        )

        # Mortgage rates as CSV
        resp = await get_data("F6", format="csv", start_period="2023")
        # → resp.csv = "date,series,value\n2023-01-01,..."

        # Raw (non-curated) F-table — pass raw RBA series IDs
        resp = await get_data("F1", series=["FIRMMCRTD", "FIRMMBAB30"])

        # Legacy alias still works (start_date / end_date)
        resp = await get_data("F11", series="aud_usd", start_date="2024")

    Parameter notes:
        - Prefer `start_period` / `end_period` (portfolio-standard names; 7
          of 9 sister MCPs use them).
        - `start_date` / `end_date` are retained as legacy aliases.
          Supplying both `start_period` and `start_date` (or `end_period`
          and `end_date`) raises ValueError — pick one per pair.

    When to use:
        - You want a time series of an RBA indicator (use latest() for current-only)
        - You want a multi-series comparison (e.g. all FX rates)
        - You want CSV for downstream charting

    Returns:
        DataResponse with records, unit, period bounds, RBA source URL,
        and CC-BY 4.0 attribution.
    """
    resolved_start = _resolve_period_alias(
        start_period, start_date, "start_period", "start_date"
    )
    resolved_end = _resolve_period_alias(
        end_period, end_date, "end_period", "end_date"
    )
    return await _get_data_impl(table_id, series, resolved_start, resolved_end, format)


@mcp.tool
async def latest(
    table_id: Annotated[
        str,
        Field(
            description="RBA F-table ID. Use search_tables() to discover.",
            examples=["F1.1", "F11", "F6", "F11.1"],
        ),
    ],
    series: Annotated[
        str | list[str] | None,
        Field(
            description=(
                "Which series to return. For curated tables: plain-English keys. "
                "Pass None (default) to get the table's headline series — e.g. "
                "F1.1 returns the cash rate target, F11/F11.1 returns AUD/USD. "
                "Pass an explicit list to get multiple series in one snapshot."
            ),
            examples=[
                "cash_rate_target",
                "aud_usd",
                ["aud_usd", "aud_eur", "aud_jpy"],
            ],
        ),
    ] = None,
) -> DataResponse:
    """Return the most recent observation for each series in an RBA F-table.

    Wraps get_data with last_n=1 (and a shorter cache TTL). Use this for
    "what's the current X?" questions — it's a cheap, fast call.

    Examples:
        # Current cash rate target (explicit)
        resp = await latest("F1.1", series="cash_rate_target")
        # → resp.records[0]: period='2026-05-06', value=3.85, unit='Per cent per annum'

        # Headline default — no series arg returns the table's canonical series.
        # F1.1 → cash rate target; F11/F11.1 → AUD/USD; F6 → average mortgage rate.
        resp = await latest("F1.1")
        # → resp.records[0]: cash_rate_target only (the table's headline)

        # Snapshot multiple FX rates in one call
        resp = await latest("F11.1", series=["aud_usd", "aud_eur", "aud_jpy"])

        # Latest owner-occupier variable mortgage rate
        resp = await latest("F6", series="owner_occupier_variable_existing")

    When to use:
        - You want the current value of an RBA indicator
        - You want a current-snapshot of multiple series in one call
          (pass an explicit list — e.g. all FX rates)
        - You want sub-50ms warm-cache latency for chat integration

    Returns:
        DataResponse with one most-recent observation per requested series.
    """
    return await _get_data_impl(table_id, series, None, None, "records", last_n=1)


@mcp.tool
def list_curated() -> list[str]:
    """List the 5 RBA F-table IDs with hand-curated plain-English support.

    These are the tables where get_data and latest accept plain-English
    series keys (like 'cash_rate_target', 'aud_usd'). Other F-tables are
    still queryable via raw RBA series IDs.

    The 5 curated F-tables:
        - F1.1 — Interest Rates and Yields: Money Market (incl. cash rate target)
        - F4 — Money Market Operations
        - F6 — Housing Lending Rates (standard variable, fixed, etc.)
        - F11 — Exchange Rates (AUD vs major currencies, daily)
        - F11.1 — Exchange Rate Indices (TWI, real TWI)

    Example:
        ids = list_curated()
        # → ['F1.1', 'F11', 'F11.1', 'F4', 'F6']

    When to use:
        - You want to know which tables have plain-English support
        - You're building a UI / agent that needs the supported set up front
        - You want to plan which F-tables to call without inspecting each

    Returns:
        Sorted list of F-table IDs. Always 5 entries today.
    """
    return curated.list_ids()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
