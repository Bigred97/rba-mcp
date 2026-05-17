"""Tests for the RBA release-calendar tool."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from rba_mcp.cache import Cache
from rba_mcp.release_calendar import (
    _classify,
    _parse_time,
    _sydney_offset_for,
    fetch_release_calendar,
    parse_entries,
)


FIXTURE = Path(__file__).parent / "fixtures" / "rba_schedule.html"


@pytest.fixture
def html_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cache.db"


# ----- _sydney_offset_for (shape parity with abs-mcp) -----


def test_sydney_offset_aest_in_june():
    assert _sydney_offset_for(datetime(2026, 6, 15, tzinfo=timezone.utc)) == timedelta(hours=10)


def test_sydney_offset_aedt_in_january():
    assert _sydney_offset_for(datetime(2026, 1, 15, tzinfo=timezone.utc)) == timedelta(hours=11)


# ----- _parse_time -----


def test_parse_time_canonical_am():
    assert _parse_time("11.30 am").isoformat() == "11:30:00"


def test_parse_time_canonical_pm():
    assert _parse_time("4.30 pm").isoformat() == "16:30:00"


def test_parse_time_strips_after_prefix():
    """'After 4.30 pm' parses as 4.30 pm."""
    assert _parse_time("After 4.30 pm").isoformat() == "16:30:00"


def test_parse_time_handles_nbsp():
    """RBA's HTML uses &nbsp; between number and am/pm."""
    assert _parse_time("11.30\xa0am").isoformat() == "11:30:00"


def test_parse_time_falls_back_for_variable():
    """'Variable' rows default to 11:30 (RBA's most common release slot)."""
    assert _parse_time("Variable").isoformat() == "11:30:00"


def test_parse_time_falls_back_for_empty():
    assert _parse_time("").isoformat() == "11:30:00"


# ----- _classify -----


def test_classify_curated_f_tables():
    """Curated F-table refreshes populate dataset_id."""
    pub, ds, ev = _classify("Financial Aggregates")
    assert pub == "D1"
    assert ds == "D1"
    assert ev == "data_release"


def test_classify_narrative_statements():
    """Narrative releases get event_type='statement', dataset_id=null."""
    for title in (
        "Statement on Monetary Policy",
        "Minutes of Monetary Policy Meeting",
        "Financial Stability Review",
        "Reserve Bank of Australia Bulletin",
    ):
        pub, ds, ev = _classify(title)
        assert pub is not None, title
        assert ds is None, title
        assert ev == "statement", title


def test_classify_non_curated_data_release():
    """Non-curated stat publications get publication_id but null dataset_id."""
    pub, ds, ev = _classify("Index of Commodity Prices")
    assert pub == "I2"
    assert ds is None
    assert ev == "data_release"


def test_classify_unknown_title_defaults_to_data_release():
    pub, ds, ev = _classify("Some New Publication")
    assert pub is None
    assert ds is None
    assert ev == "data_release"


# ----- parse_entries (full HTML → list[ReleaseEntry]) -----


def test_parse_entries_returns_non_empty_list(html_fixture: str):
    entries = parse_entries(html_fixture)
    assert len(entries) >= 20, f"expected >=20, got {len(entries)}"


def test_parse_entries_all_have_release_at_with_tz(html_fixture: str):
    entries = parse_entries(html_fixture)
    assert all(e.release_at.tzinfo is not None for e in entries)
    offsets = {e.release_at.utcoffset() for e in entries}
    assert offsets.issubset({timedelta(hours=10), timedelta(hours=11)})


def test_parse_entries_dedupes_by_url_and_release_at(html_fixture: str):
    """A row appearing in both 'This Week' and the month-grid shouldn't
    produce two ReleaseEntry instances with the same (url, release_at)."""
    entries = parse_entries(html_fixture)
    keys = [(e.source_url, e.release_at.isoformat()) for e in entries]
    assert len(keys) == len(set(keys))


def test_parse_entries_classifies_statements(html_fixture: str):
    """At least one Chart Pack / SoMP / Minutes / Bulletin / FSR row
    should be tagged event_type='statement'."""
    entries = parse_entries(html_fixture)
    statement_titles = {e.title for e in entries if e.event_type == "statement"}
    assert statement_titles, "expected at least one statement entry"


def test_parse_entries_skips_weekdays_recurring_rows(html_fixture: str):
    """'Weekdays' rows in Table 1 are deliberately skipped (would emit
    one entry per weekday in the window, drowning out actual events)."""
    entries = parse_entries(html_fixture)
    titles = [e.title for e in entries]
    assert "Open Market Operations - Current - A3" not in titles


# ----- fetch_release_calendar -----


async def test_fetch_release_calendar_serves_cache_when_live_503s(
    html_fixture: str, db_path: Path
) -> None:
    cache = Cache(db_path)
    await cache.set(
        "https://www.rba.gov.au/schedules-events/",
        html_fixture.encode("utf-8"),
        kind="calendar",
    )
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream broke")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        from rba_mcp import cache as cache_mod
        original_ttl = cache_mod.TTL["calendar"]
        cache_mod.TTL["calendar"] = timedelta(seconds=0)
        try:
            entries, stale, reason = await fetch_release_calendar(http, cache, days_ahead=3650)
        finally:
            cache_mod.TTL["calendar"] = original_ttl

    assert stale is True
    assert reason is not None
    assert "503" in reason


async def test_fetch_release_calendar_sorted_ascending(
    html_fixture: str, db_path: Path
) -> None:
    cache = Cache(db_path)
    await cache.set(
        "https://www.rba.gov.au/schedules-events/",
        html_fixture.encode("utf-8"),
        kind="calendar",
    )
    async with httpx.AsyncClient() as http:
        entries, _, _ = await fetch_release_calendar(http, cache, days_ahead=365)
    assert [e.release_at for e in entries] == sorted(e.release_at for e in entries)


async def test_fetch_release_calendar_horizon_filters(
    html_fixture: str, db_path: Path
) -> None:
    cache = Cache(db_path)
    await cache.set(
        "https://www.rba.gov.au/schedules-events/",
        html_fixture.encode("utf-8"),
        kind="calendar",
    )
    async with httpx.AsyncClient() as http:
        e30, _, _ = await fetch_release_calendar(http, cache, days_ahead=30)
        e365, _, _ = await fetch_release_calendar(http, cache, days_ahead=365)
    assert len(e365) >= len(e30)


# ----- Tool surface -----


async def test_release_calendar_tool_rejects_bad_days_ahead():
    from rba_mcp import server

    with pytest.raises(ValueError, match="days_ahead must be"):
        await server.release_calendar(0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="days_ahead must be"):
        await server.release_calendar(366)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be an int"):
        await server.release_calendar("30")  # type: ignore[arg-type]
