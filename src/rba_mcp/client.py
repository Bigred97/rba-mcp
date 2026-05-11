"""Async RBA CSV fetcher.

Owns the httpx call so cache keys are just URLs. Returns raw bytes; parsing
lives in `parsing.py` so the cache layer stays protocol-agnostic.

RBA serves CSVs from a static CDN — no auth, no rate limiting documented.
We send a courteous User-Agent in case they ever throttle.

Concurrent callers for the same URL share one in-flight HTTP request — see
`_in_flight` — so a burst of parallel `latest()` calls hits the CDN once,
not N times.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .cache import TTL, Cache, CacheKind

DEFAULT_BASE_URL = "https://www.rba.gov.au"
DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class RBAAPIError(Exception):
    """Raised when the RBA CDN returns a non-2xx response or the request fails."""


class RBAClient:
    def __init__(
        self,
        cache: Cache | None = None,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache = cache or Cache()
        self._http = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            transport=transport,
            headers={"User-Agent": "rba-mcp/0.1 (+https://github.com/Bigred97/rba-mcp)"},
            follow_redirects=True,
        )
        self._in_flight: dict[str, asyncio.Future[bytes]] = {}
        self._in_flight_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "RBAClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def fetch_table_csv(
        self, csv_filename: str, *, kind: CacheKind = "data"
    ) -> bytes:
        """Fetch one F-table CSV by filename (e.g. 'f11-data.csv'). Cached.

        Concurrent callers for the same URL share one in-flight HTTP request.
        """
        url = f"{self.base_url}/statistics/tables/csv/{csv_filename}"
        cached = await self.cache.get(url, ttl=TTL[kind])
        if cached is not None:
            return cached

        async with self._in_flight_lock:
            existing = self._in_flight.get(url)
            if existing is None:
                future: asyncio.Future[bytes] = (
                    asyncio.get_running_loop().create_future()
                )
                self._in_flight[url] = future

        if existing is not None:
            return await existing

        try:
            try:
                resp = await self._http.get(url)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise RBAAPIError(
                    f"RBA CDN returned {e.response.status_code} for {url}"
                ) from e
            except httpx.RequestError as e:
                raise RBAAPIError(f"RBA CDN request failed: {e}") from e
            await self.cache.set(url, resp.content, kind=kind)
            future.set_result(resp.content)
            return resp.content
        except BaseException as e:
            if not future.done():
                future.set_exception(e)
            raise
        finally:
            async with self._in_flight_lock:
                self._in_flight.pop(url, None)
