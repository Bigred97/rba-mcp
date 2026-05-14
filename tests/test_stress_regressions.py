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
from rba_mcp.parsing import filter_by_dates

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


# ----- Round-2 regression tests (post-0.1.4 stress test) -----

async def test_200_duplicate_series_dedup_returns_full_data():
    """0.1.4 dedup must hold under high-cardinality duplicate input.

    A pre-0.1.4 wheel returned empty for `["aud_usd"]*200` because the duplicate
    columns broke `to_records`. After dedup it must return the full series.
    """
    resp = await server.get_data(
        table_id="F11.1", series=["aud_usd"] * 200
    )
    assert len(resp.records) > 100, (
        f"200×duplicate-series should dedup to 1 and return all F11.1 daily "
        f"observations; got {len(resp.records)}"
    )
    # And the period field must be a real ISO date, not the series ID.
    assert all(r.period[:4].isdigit() for r in resp.records[:10])


async def test_absurd_valid_future_end_date_is_inclusive_not_filtered():
    """end_date='9999-12-31' is a valid date strictly after all real data —
    so the result is identical to passing no end_date. That's correct
    semantics (not a silent bypass)."""
    resp_with = await server.get_data(
        table_id="F11.1", series="aud_usd", end_date="9999-12-31"
    )
    await server.reset_client_for_tests()
    resp_without = await server.get_data(
        table_id="F11.1", series="aud_usd"
    )
    assert len(resp_with.records) == len(resp_without.records), (
        "end_date in the future of all data should be equivalent to no end_date"
    )


async def test_composite_invalid_start_valid_end_errors_on_start():
    """The composite `start='2026-99-99', end='9999-12-31'` from the stress
    test must error on the invalid start rather than silently fall through
    to a full-dataset return."""
    with pytest.raises(ValueError, match="not a valid date"):
        await server.get_data(
            table_id="F11.1",
            series="aud_usd",
            start_date="2026-99-99",
            end_date="9999-12-31",
        )


async def test_response_includes_server_version():
    """`server_version` echoed in every DataResponse so testers can verify
    which wheel served the call (uvx caches per-version)."""
    resp = await server.latest(table_id="F11.1", series="aud_usd")
    assert resp.server_version
    # Should be a valid PEP-440 version (or our editable-install sentinel)
    assert resp.server_version[0].isdigit()


# ----- Round-3 regression test (post-0.1.5 audit) -----

async def test_get_data_non_string_format_rejects_cleanly():
    """Bug #9 regression (0.1.6): `format=42` used to crash with raw
    `AttributeError: 'int' object has no attribute 'lower'` because the
    coercion `(fmt or "records").lower()` ran before the type-validation
    guard. Now: clean ValueError with a useful hint."""
    import pytest
    with pytest.raises(ValueError, match="format must be a string"):
        await server.get_data(table_id="F11.1", series="aud_usd", format=42)  # type: ignore[arg-type]


async def test_get_data_other_non_string_format_types():
    """Same guard — covers list, dict, None-coerced-from-list etc."""
    import pytest
    for bad in [42, 3.14, ["records"], {"format": "records"}, True]:
        with pytest.raises(ValueError, match="format must be a string"):
            await server.get_data(
                table_id="F11.1", series="aud_usd", format=bad  # type: ignore[arg-type]
            )
        await server.reset_client_for_tests()


# ----- Round-4 regression tests (post-0.1.7 customer-flow audit) -----

async def test_get_data_accepts_int_year_for_dates():
    """Bug #10 regression (0.1.8): MCP / LLM clients often send a year as
    a JSON number (`start_date=2024`) rather than a string (`"2024"`).
    Pre-0.1.8 this errored at the Pydantic boundary with a verbose
    'Input should be a valid string' message. Now: int years are
    coerced to strings transparently."""
    resp = await server.get_data(
        table_id="F1.1",
        series="cash_rate_target",
        start_date=2024,  # type: ignore[arg-type]
        end_date=2024,    # type: ignore[arg-type]
    )
    # 12 monthly observations across calendar year 2024
    assert len(resp.records) >= 12
    months = sorted({r.period[:7] for r in resp.records})
    assert months[0].endswith("01"), f"first month wrong: {months[0]}"
    assert months[-1].endswith("12"), f"last month wrong: {months[-1]}"


async def test_get_data_int_dates_match_string_dates():
    """Equivalence: passing int 2024 must produce the same result as
    passing the string '2024'."""
    resp_int = await server.get_data(
        table_id="F1.1", series="cash_rate_target",
        start_date=2024, end_date=2024,  # type: ignore[arg-type]
    )
    await server.reset_client_for_tests()
    resp_str = await server.get_data(
        table_id="F1.1", series="cash_rate_target",
        start_date="2024", end_date="2024",
    )
    assert len(resp_int.records) == len(resp_str.records)
    int_periods = [r.period for r in resp_int.records]
    str_periods = [r.period for r in resp_str.records]
    assert int_periods == str_periods


async def test_get_data_rejects_bool_for_dates():
    """`isinstance(True, int) is True` in Python — but `True` is not a
    sensible year. The 0.1.8 int-coerce explicitly excludes bools so
    True/False still raise a clean type error."""
    import pytest
    for bad in [True, False]:
        with pytest.raises(ValueError, match="must be a string"):
            await server.get_data(
                table_id="F11.1", series="aud_usd",
                start_date=bad,  # type: ignore[arg-type]
            )
        await server.reset_client_for_tests()


async def test_describe_table_curated_populates_start_date():
    """Bug #11 regression (0.1.8): describe_table for curated tables used
    to leave SeriesDetail.start_date null because the curated branch
    skipped the CSV-fetch + first_valid_index step the non-curated
    branch did. Now both branches populate it. The LLM needs this to
    pick a sensible date range without trial-and-error queries."""
    detail = await server.describe_table("F11.1")
    assert detail.is_curated
    # Every series should have a real ISO date now, not null
    null_starts = [s.key for s in detail.series if s.start_date is None]
    assert not null_starts, f"curated series with null start_date: {null_starts}"
    # F11.1 daily data starts in 2023 — sanity-check the range
    for s in detail.series:
        assert s.start_date and s.start_date[:4].isdigit()
        assert "2020" <= s.start_date[:4] <= "2026"


# ----- Round-5 regression tests (post-0.1.8 customer-value audit, 0.1.9) -----

async def test_describe_table_populates_end_date_for_curated():
    """0.1.9: SeriesDetail now carries `end_date` (latest non-null
    observation) so an LLM can check data freshness from describe_table
    alone, without a separate latest() round-trip."""
    detail = await server.describe_table("F11.1")
    assert detail.is_curated
    null_ends = [s.key for s in detail.series if s.end_date is None]
    assert not null_ends, f"curated series with null end_date: {null_ends}"
    # F11.1 is daily and should be fresh — every end_date should be in the
    # current decade and not before the corresponding start_date.
    for s in detail.series:
        assert s.end_date and s.end_date[:4].isdigit()
        assert s.start_date <= s.end_date, (
            f"{s.key}: end_date {s.end_date} before start_date {s.start_date}"
        )
        assert s.end_date[:4] >= "2024"


async def test_describe_table_populates_end_date_for_non_curated():
    """Same end_date population for raw (non-curated) tables — bypass the
    curated registry to force the non-curated branch. Some header series
    may have no data column (genuinely empty in the CSV); those keep
    end_date=None. Verify that series with start_date ALSO have end_date."""
    from rba_mcp import curated as curated_mod
    from unittest.mock import patch
    with patch.object(curated_mod, "get", return_value=None):
        detail = await server.describe_table("F11.1")
    assert not detail.is_curated
    populated = [s for s in detail.series if s.start_date is not None]
    assert populated, "expected at least one series with populated dates"
    for s in populated:
        assert s.end_date and s.end_date[:4].isdigit(), (
            f"series {s.key} has start_date={s.start_date} but end_date={s.end_date}"
        )
        assert s.start_date <= s.end_date


async def test_search_yield_curve_routes_to_f2():
    """0.1.9 added 'yield curve' / 'bond yields' keywords to F2 / F2.1
    so a natural query routes to the right capital-market table.
    Pre-0.1.9 the keywords were only 'government bonds, yields,
    treasury bonds, capital market, 10 year bond' which missed
    'yield curve' as a phrase."""
    results = await server.search_tables(query="yield curve", limit=10)
    ids = [r.id for r in results[:5]]
    assert "F2" in ids or "F2.1" in ids, (
        f"yield curve query should surface F2/F2.1 in top 5, got: {ids}"
    )


async def test_search_bond_yields_routes_to_f2():
    """Companion to the yield-curve test — 'bond yields' must also route."""
    results = await server.search_tables(query="bond yields", limit=10)
    ids = [r.id for r in results[:5]]
    assert "F2" in ids or "F2.1" in ids, (
        f"bond yields query should surface F2/F2.1 in top 5, got: {ids}"
    )
