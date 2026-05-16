"""Resilience / perf-budget regressions.

Mostly Item 1 from the sister-MCP playbook: prove `latest()` is fast
and doesn't balloon memory after warm cache. RBA F-tables are
relatively small (16KB-140KB published as of 2026-05) so the heavy
lifting is parsing not network — these tests pin the hot-path
characteristics so a future regression (e.g. accidentally loading
the whole CSV into a list of lists, calling `to_records` twice,
etc.) shows up in CI as a perf failure rather than a slow customer
report.
"""
from __future__ import annotations

import asyncio
import gc
import resource
import sys
import time
from pathlib import Path

import httpx
import pytest

from rba_mcp import server
from rba_mcp.cache import Cache
from rba_mcp.client import RBAClient

FIXTURES = Path(__file__).parent / "fixtures"


def _peak_rss_mb() -> float:
    """Peak RSS in MB. ru_maxrss is bytes on macOS, KB on Linux."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / 1024 / 1024 if sys.platform == "darwin" else rss / 1024


@pytest.fixture
async def offline_client(tmp_path):
    """RBAClient that serves the fixture CSVs from disk — no network."""
    fixture_map = {
        "/statistics/tables/csv/f1.1-data.csv": (FIXTURES / "f1.1-data.csv").read_bytes(),
        "/statistics/tables/csv/f11.1-data.csv": (FIXTURES / "f11.1-data.csv").read_bytes(),
        "/statistics/tables/csv/f11-data.csv": (FIXTURES / "f11-data.csv").read_bytes(),
        "/statistics/tables/csv/f4-data.csv": (FIXTURES / "f4-data.csv").read_bytes(),
        "/statistics/tables/csv/f6-data.csv": (FIXTURES / "f6-data.csv").read_bytes(),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in fixture_map:
            return httpx.Response(200, content=fixture_map[path])
        return httpx.Response(404, text=f"no fixture for {path}")

    client = RBAClient(
        cache=Cache(tmp_path / "cache.db"),
        transport=httpx.MockTransport(handler),
    )
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
def patch_server_client(offline_client, monkeypatch):
    async def _get():
        return offline_client
    monkeypatch.setattr(server, "_get_client", _get)


async def test_latest_warm_cache_under_2s():
    """`latest()` against a warm cache must complete in well under 2 seconds.

    Acceptance test for Item 1 — filter pushdown matters for keeping the
    chat-integration latency budget. Even though RBA F-tables are small
    (16KB-140KB), parsing + DataFrame ops add ~100ms cold and we need
    to keep that from drifting up over time as we add curated tables.
    """
    # Cold call to warm the cache + import path
    await server.latest("F1.1", series="cash_rate_target")

    t0 = time.perf_counter()
    resp = await server.latest("F1.1", series="cash_rate_target")
    elapsed = time.perf_counter() - t0

    assert resp.records, "expected at least one record"
    assert elapsed < 2.0, (
        f"latest(F1.1) warm-cache took {elapsed:.3f}s — perf budget is <2s. "
        "Did parse_csv start doing extra work?"
    )


async def test_latest_no_per_call_memory_balloon():
    """Calling `latest()` many times must not balloon memory per call.

    We can't enforce an absolute "<50MB" budget because pandas alone owns
    ~70MB at import. What we CAN catch is a per-call leak: if peak RSS
    grows by more than ~30MB across N repeated calls, something is
    holding refs (a DataFrame in a module-level cache, a record list
    that keeps growing, etc.).
    """
    # Warm the import path + cache.
    await server.latest("F1.1", series="cash_rate_target")
    gc.collect()
    rss_before = _peak_rss_mb()

    # Run a batch of cheap repeated calls. If anything leaks, the peak
    # tracker will move; warm-cache calls should be essentially free.
    # 30 is enough to surface a 1MB-per-call leak while keeping the
    # test under ~2s.
    for _ in range(30):
        resp = await server.latest("F1.1", series="cash_rate_target")
        assert resp.records

    gc.collect()
    rss_after = _peak_rss_mb()
    growth = rss_after - rss_before

    assert growth < 30.0, (
        f"latest() grew peak RSS by {growth:.1f}MB across 30 calls — "
        f"({rss_before:.1f}MB → {rss_after:.1f}MB). "
        "Something is retaining DataFrames or records across calls."
    )


async def test_latest_explicit_series_does_not_pull_all_columns():
    """When the caller asks for one series, the response must NOT carry
    records for every series in the table.

    This is the closest behavioural check we have for "filter pushdown
    worked" — even though the underlying parse loads all columns into a
    pandas DataFrame (cheap at RBA's file sizes), the slim filtered
    frame is what reaches `to_records`, so only the requested series'
    observations appear in the response.
    """
    resp = await server.latest("F1.1", series="cash_rate_target")
    # Exactly one record (a single date, a single series).
    assert len(resp.records) == 1, (
        f"expected 1 record for one series, got {len(resp.records)}"
    )
    # And the only series in the response must be the one we asked for.
    distinct_series = {r.dimensions["series"] for r in resp.records}
    assert len(distinct_series) == 1


async def test_get_data_date_range_does_not_carry_out_of_range_rows():
    """`get_data(start, end)` must drop out-of-range rows before they
    reach the response, regardless of how the CSV is fetched.

    Without this, every call would return the full history every time,
    plus the user's slice — a huge token bloat.
    """
    resp = await server.get_data(
        table_id="F11",
        series="aud_usd",
        start_date="2024-01",
        end_date="2024-03",
    )
    assert resp.records
    # Every record's period must fall inside the requested window.
    periods = [r.period for r in resp.records]
    assert all("2024-01" <= p[:7] <= "2024-03" for p in periods), (
        f"records leaked outside [2024-01, 2024-03]: {sorted(set(periods))[:5]}"
    )


async def test_concurrent_latest_calls_complete():
    """A burst of concurrent `latest()` calls completes promptly.

    The client's in-flight coalescing should keep this from queuing,
    and the cache short-circuit should keep the total runtime O(1) not
    O(N). Pin the bound at 5s for 10 parallel calls so a future
    regression in the parsing/shaping pipeline (e.g. someone holds a
    global lock) surfaces here.
    """
    await server.latest("F11.1", series="aud_usd")  # warm

    t0 = time.perf_counter()
    results = await asyncio.gather(*[
        server.latest("F11.1", series="aud_usd") for _ in range(10)
    ])
    elapsed = time.perf_counter() - t0

    assert all(r.records for r in results)
    assert elapsed < 5.0, (
        f"10 concurrent warm-cache latest() calls took {elapsed:.2f}s — "
        "perf budget is <5s. Check for new locking in the hot path."
    )
