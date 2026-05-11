"""Regression tests for the four bugs surfaced by the 0.1.3 stress-test pass.

The stress test was a real customer probing the MCP tool surface end-to-end
in Claude Desktop; these tests lock in the fixes so they can't regress.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from rba_mcp import server
from rba_mcp.cache import Cache
from rba_mcp.client import RBAClient
from rba_mcp.parsing import filter_by_dates, parse_csv

FIXTURES = Path(__file__).parent / "fixtures"


# Bug-fix verification fixtures: spin up the server with a per-test cache
# pointed at a fresh tmp_path, and inject our offline-fixture RBA CDN.
@pytest.fixture
async def offline_client(tmp_path):
    """RBAClient that serves the fixture F11.1 / F1.1 / F2 from disk."""
    import httpx

    fixture_map = {
        "/statistics/tables/csv/f11.1-data.csv": (FIXTURES / "f11.1-data.csv").read_bytes(),
        "/statistics/tables/csv/f1.1-data.csv": (FIXTURES / "f1.1-data.csv").read_bytes(),
        "/statistics/tables/csv/f11-data.csv": (FIXTURES / "f11-data.csv").read_bytes(),
        "/statistics/tables/csv/f4-data.csv": (FIXTURES / "f4-data.csv").read_bytes(),
        "/statistics/tables/csv/f6-data.csv": (FIXTURES / "f6-data.csv").read_bytes(),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        for path, body in fixture_map.items():
            if request.url.path == path:
                return httpx.Response(200, content=body)
        return httpx.Response(404, text=f"no fixture for {request.url.path}")

    client = RBAClient(
        cache=Cache(tmp_path / "cache.db"),
        transport=httpx.MockTransport(handler),
    )
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
def patch_server_client(offline_client, monkeypatch):
    async def _get(): return offline_client
    monkeypatch.setattr(server, "_get_client", _get)


# ----- Bug #1: duplicate series IDs leak series ID into period field -----

async def test_latest_dedupes_curated_key_and_raw_id_for_same_series():
    """Bug #1 regression: passing ['aud_usd', 'FXRUSD'] (both → FXRUSD) used to
    return 4 duplicate records with `period='FXRUSD'` because the duplicate
    column made `df[sid]` return a DataFrame, breaking the to_records loop."""
    resp = await server.latest(table_id="F11.1", series=["aud_usd", "FXRUSD"])
    assert len(resp.records) == 1, f"expected 1 deduped record, got {len(resp.records)}"
    obs = resp.records[0]
    # Period must be an actual date, not the series ID
    assert obs.period and obs.period[:4].isdigit(), f"period polluted: {obs.period!r}"
    assert obs.dimensions["table"] == "F11.1"


async def test_get_data_dedupes_repeated_curated_keys():
    """Same dedupe applies to get_data — `['aud_usd', 'aud_usd']`."""
    resp = await server.get_data(
        table_id="F11", series=["aud_usd", "aud_usd"], start_date="2024", end_date="2024-06"
    )
    # Each (date, series) should appear once
    pairs = {(r.period, r.dimensions["series"]) for r in resp.records}
    assert len(pairs) == len(resp.records), "duplicate (period, series) pairs in output"


# ----- Bug #2: end_date partial-period silently collapses to first day -----

def test_filter_by_dates_end_yyyy_includes_full_year():
    """Bug #2 regression: end='2024' should include all of 2024, not just Jan 1."""
    idx = pd.date_range("2023-06-01", "2025-06-01", freq="MS")
    df = pd.DataFrame({"x": range(len(idx))}, index=idx)
    filtered = filter_by_dates(df, start="2024", end="2024")
    months = sorted(d.month for d in filtered.index)
    assert months == list(range(1, 13)), f"expected all 12 months of 2024, got {months}"


def test_filter_by_dates_end_yyyy_mm_includes_full_month():
    """Bug #2 regression: end='2024-06' should include all of June, not just June 1."""
    idx = pd.date_range("2024-01-01", "2024-12-31", freq="D")
    df = pd.DataFrame({"x": range(len(idx))}, index=idx)
    filtered = filter_by_dates(df, start="2024-01", end="2024-06")
    months = sorted(set(d.month for d in filtered.index))
    assert months == [1, 2, 3, 4, 5, 6], f"expected Jan-June, got {months}"
    assert filtered.index.max().month == 6
    assert filtered.index.max().day == 30  # last day of June


def test_filter_by_dates_end_yyyy_mm_dd_unchanged():
    """Full ISO date used as-is — no expansion."""
    idx = pd.date_range("2024-01-01", "2024-12-31", freq="D")
    df = pd.DataFrame({"x": range(len(idx))}, index=idx)
    filtered = filter_by_dates(df, start="2024-06-01", end="2024-06-15")
    assert filtered.index.max().day == 15
    assert filtered.index.max().month == 6


async def test_get_data_yyyy_range_returns_full_year():
    """End-to-end: get_data with start_date='2024', end_date='2024' returns ≥12 months."""
    resp = await server.get_data(
        table_id="F1.1", series="cash_rate_target", start_date="2024", end_date="2024"
    )
    assert len(resp.records) >= 12, (
        f"expected ≥12 monthly records for full 2024, got {len(resp.records)}"
    )
    months = sorted({r.period[:7] for r in resp.records})
    assert months[0].endswith("01") and months[-1].endswith("12"), (
        f"expected Jan-Dec coverage, got months: {months}"
    )


async def test_get_data_yyyy_mm_range_includes_end_month():
    """End-to-end: get_data with end_date='2024-06' includes all of June."""
    resp = await server.get_data(
        table_id="F1.1", series="cash_rate_target",
        start_date="2024-01", end_date="2024-06",
    )
    assert len(resp.records) >= 6, f"expected ≥6 records (Jan-Jun), got {len(resp.records)}"
    months = sorted({r.period[:7] for r in resp.records})
    assert "2024-06" in months, f"June missing from result: {months}"


# ----- Bug #3: non-curated invalid series silently returns empty -----

async def test_get_data_non_curated_unknown_series_raises_value_error():
    """Bug #3 regression: F2 (non-curated) with a typo'd series ID used to
    return [] instead of erroring. Pipelines couldn't distinguish a typo
    from "no data in range"."""
    # We don't have F2 fixture but we can prove the validation triggers
    # by using a curated table's CSV with a bogus series ID (since the
    # validation is "is this ID in the parsed CSV header?", which any
    # CSV header will fail for a fake ID).
    # Use F11 (curated) but with raw IDs to bypass the curated path:
    # the test relies on the offline_client serving the F11 fixture.
    with pytest.raises(ValueError, match="Unknown series"):
        # Force the non-curated path by querying a non-curated table.
        # F2 has no fixture so the fetch would fail; use F4 (curated)
        # but call get_data with a raw ID that's not in F4's header.
        # Actually F4 IS curated, so this hits the curated path which
        # raises a different error. The proper regression is: use a
        # non-curated table from the registry — F5 indicator lending
        # rates is non-curated but has no fixture either.
        #
        # Workaround: monkey-patch curated.get to return None for F4,
        # which forces the non-curated code path against the F4 fixture.
        from rba_mcp import curated as curated_mod
        from unittest.mock import patch
        with patch.object(curated_mod, "get", return_value=None):
            await server.get_data(
                table_id="F4", series=["FAKESERIES_THAT_DOES_NOT_EXIST"]
            )


# ----- Bug #4: calendar-invalid dates (verifying it's already fixed) -----

async def test_calendar_invalid_end_date_rejected():
    """Bug #4 was already fixed in 0.1.2 via _is_valid_period — lock it in."""
    with pytest.raises(ValueError, match="not a valid date"):
        await server.get_data(
            table_id="F11.1", series="aud_usd",
            start_date="2026-02-01", end_date="2026-02-30",
        )


async def test_calendar_invalid_month_rejected():
    with pytest.raises(ValueError, match="not a valid date"):
        await server.get_data(
            table_id="F11.1", series="aud_usd", end_date="2026-13-01"
        )
