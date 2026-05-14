import asyncio
from pathlib import Path

import httpx
import pytest

from rba_mcp.cache import Cache
from rba_mcp.client import RBAAPIError, RBAClient


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cache.db"


async def test_fetch_table_csv_caches(db_path):
    fixture = (Path(__file__).parent / "fixtures" / "f11-data.csv").read_bytes()
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, content=fixture)

    transport = httpx.MockTransport(handler)
    cache = Cache(db_path)
    async with RBAClient(cache=cache, transport=transport) as client:
        body1 = await client.fetch_table_csv("f11-data.csv")
        body2 = await client.fetch_table_csv("f11-data.csv")
    assert call_count["n"] == 1, "second call should hit cache"
    assert body1 == body2 == fixture


async def test_4xx_raises_rba_api_error(db_path):
    transport = httpx.MockTransport(lambda req: httpx.Response(404, text="not found"))
    cache = Cache(db_path)
    async with RBAClient(cache=cache, transport=transport) as client:
        with pytest.raises(RBAAPIError, match="404"):
            await client.fetch_table_csv("does-not-exist.csv")


async def test_url_built_correctly(db_path):
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, content=b"x")

    transport = httpx.MockTransport(handler)
    cache = Cache(db_path)
    async with RBAClient(cache=cache, transport=transport) as client:
        await client.fetch_table_csv("f1.1-data.csv")
    assert captured["url"] == "https://www.rba.gov.au/statistics/tables/csv/f1.1-data.csv"


class _GatedTransport(httpx.AsyncBaseTransport):
    """Async transport that blocks at `request_started` until `request_can_finish` is set."""

    def __init__(self, fixture: bytes) -> None:
        self.fixture = fixture
        self.call_count = 0
        self.request_started = asyncio.Event()
        self.request_can_finish = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        self.request_started.set()
        await self.request_can_finish.wait()
        return httpx.Response(200, content=self.fixture)


async def test_concurrent_fetches_coalesce_to_single_request(db_path):
    """Two concurrent fetches for the same URL share one HTTP request (thundering-herd guard).

    Regression test for the case where N parallel `latest()` calls would each
    hit the RBA CDN independently on a cold cache.
    """
    fixture = (Path(__file__).parent / "fixtures" / "f11-data.csv").read_bytes()
    transport = _GatedTransport(fixture)
    cache = Cache(db_path)
    async with RBAClient(cache=cache, transport=transport) as client:
        t1 = asyncio.create_task(client.fetch_table_csv("f11-data.csv"))
        await transport.request_started.wait()
        t2 = asyncio.create_task(client.fetch_table_csv("f11-data.csv"))
        t3 = asyncio.create_task(client.fetch_table_csv("f11-data.csv"))
        # Give followers a chance to enter the in-flight wait state.
        await asyncio.sleep(0.02)
        transport.request_can_finish.set()
        bodies = await asyncio.gather(t1, t2, t3)
    assert transport.call_count == 1, (
        f"expected 1 HTTP call, got {transport.call_count} — followers should "
        "share the owner's in-flight future"
    )
    assert bodies[0] == bodies[1] == bodies[2] == fixture


async def test_in_flight_cleared_after_failure(db_path):
    """A failed fetch must clear its in-flight slot so the next caller can retry."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(503, text="busy")

    transport = httpx.MockTransport(handler)
    cache = Cache(db_path)
    async with RBAClient(cache=cache, transport=transport) as client:
        with pytest.raises(RBAAPIError):
            await client.fetch_table_csv("f11-data.csv")
        # After the first failure, the second call should attempt a fresh fetch
        # (not hang awaiting the prior owner's dead future).
        with pytest.raises(RBAAPIError):
            await client.fetch_table_csv("f11-data.csv")
    assert call_count["n"] == 2, "in-flight slot should clear after failure"


async def test_concurrent_failures_share_exception(db_path):
    """Followers awaiting an in-flight request must surface the owner's error."""
    fixture = b"unused"

    class FailingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.call_count = 0
            self.started = asyncio.Event()
            self.can_finish = asyncio.Event()

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.call_count += 1
            self.started.set()
            await self.can_finish.wait()
            return httpx.Response(500, text="boom")

    transport = FailingTransport()
    cache = Cache(db_path)
    async with RBAClient(cache=cache, transport=transport) as client:
        t1 = asyncio.create_task(client.fetch_table_csv("f11-data.csv"))
        await transport.started.wait()
        t2 = asyncio.create_task(client.fetch_table_csv("f11-data.csv"))
        await asyncio.sleep(0.02)
        transport.can_finish.set()
        results = await asyncio.gather(t1, t2, return_exceptions=True)
    assert isinstance(results[0], RBAAPIError)
    assert isinstance(results[1], RBAAPIError)
    assert transport.call_count == 1, "followers should not trigger a second request"


# ─── stale-fallback graceful degradation (CLAUDE.md quality dim #4) ──────

async def _prime_stale_cache(db_path: Path, url: str, payload: bytes, age_hours: float) -> None:
    """Put `payload` into the cache as if it was fetched `age_hours` ago.
    Used to test the stale-fallback path: a regular cache.get() with a normal
    TTL will miss this row (because cached_at is older than the TTL window),
    but cache.get_stale() will still return it.
    """
    import time
    import aiosqlite
    from rba_mcp.cache import Cache
    cache = Cache(db_path)
    await cache._ensure_init()
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO http_cache (cache_key, payload, cached_at, kind) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET "
            "payload=excluded.payload, cached_at=excluded.cached_at",
            (url, payload, time.time() - age_hours * 3600, "data"),
        )
        await conn.commit()


async def test_stale_fallback_serves_cached_payload_on_5xx(db_path):
    """When upstream RBA returns 5xx and we have a cached payload past its
    TTL, serve the cached payload and mark the response as stale. Agents
    continue reasoning rather than crashing."""
    from rba_mcp.client import get_stale_signal, reset_stale_signal

    fixture = (Path(__file__).parent / "fixtures" / "f11-data.csv").read_bytes()
    url = "https://www.rba.gov.au/statistics/tables/csv/f11-data.csv"

    # Prime a 24h-old cache entry — past the 6h data TTL, so cache.get()
    # misses but cache.get_stale() will still return it.
    await _prime_stale_cache(db_path, url, fixture, age_hours=24)

    reset_stale_signal()
    cache = Cache(db_path)
    async with RBAClient(
        cache=cache,
        transport=httpx.MockTransport(lambda req: httpx.Response(503, text="Service Unavailable")),
    ) as client:
        body = await client.fetch_table_csv("f11-data.csv")
        assert body == fixture, "fallback payload must match what was primed"
        stale, reason = get_stale_signal()
        assert stale is True, "stale flag must be set after 5xx fallback"
        assert reason and "503" in reason, f"stale_reason should mention the 5xx: {reason}"
        assert "minute" in reason.lower(), f"stale_reason should report age: {reason}"


async def test_stale_fallback_serves_cached_on_request_error(db_path):
    """Same as 5xx test but for httpx.RequestError (DNS / connection refused / etc.)."""
    from rba_mcp.client import get_stale_signal, reset_stale_signal

    fixture = (Path(__file__).parent / "fixtures" / "f11-data.csv").read_bytes()
    url = "https://www.rba.gov.au/statistics/tables/csv/f11-data.csv"
    await _prime_stale_cache(db_path, url, fixture, age_hours=24)

    reset_stale_signal()
    def raise_request_error(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated DNS failure")

    cache = Cache(db_path)
    async with RBAClient(cache=cache, transport=httpx.MockTransport(raise_request_error)) as client:
        body = await client.fetch_table_csv("f11-data.csv")
        assert body == fixture
        stale, reason = get_stale_signal()
        assert stale is True
        assert reason and "ConnectError" in reason


async def test_raises_when_no_stale_cache_to_fall_back_to(db_path):
    """Empty cache + upstream 5xx → still raises RBAAPIError (original behaviour
    when there's nothing to gracefully degrade to)."""
    from rba_mcp.client import reset_stale_signal

    reset_stale_signal()
    cache = Cache(db_path)
    async with RBAClient(
        cache=cache,
        transport=httpx.MockTransport(lambda req: httpx.Response(503, text="Service Unavailable")),
    ) as client:
        with pytest.raises(RBAAPIError, match="503"):
            await client.fetch_table_csv("f11-data.csv")


async def test_cache_get_stale_returns_payload_and_timestamp(db_path):
    """Cache.get_stale() returns (payload, cached_at) regardless of TTL —
    the building block for client's stale-fallback path."""
    from datetime import timedelta

    cache = Cache(db_path)
    await cache.set("https://example.org/x", b"hello", kind="data")
    # Normal `get` with a tiny TTL should miss
    fresh = await cache.get("https://example.org/x", ttl=timedelta(seconds=0))
    assert fresh is None
    # `get_stale` should return regardless of TTL
    stale = await cache.get_stale("https://example.org/x")
    assert stale is not None
    payload, cached_at = stale
    assert payload == b"hello"
    assert cached_at > 0
    # Non-existent key → None
    miss = await cache.get_stale("https://example.org/missing")
    assert miss is None
