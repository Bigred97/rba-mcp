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
import time
from contextvars import ContextVar
from typing import Any

import httpx

from .cache import TTL, Cache, CacheKind

DEFAULT_BASE_URL = "https://www.rba.gov.au"
DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


# ─── stale signal (graceful-degradation reporting per CLAUDE.md dim #4) ─
# When the upstream RBA CDN is unreachable, fetch_table_csv falls back to
# the cached payload regardless of TTL and records the staleness in this
# ContextVar. Server-side tool wrappers read it after the request chain
# and set DataResponse.stale / .stale_reason. ContextVar (not instance
# attr) so concurrent MCP tool calls each see their own state.
_stale_signal: ContextVar[tuple[bool, str | None]] = ContextVar(
    "rba_mcp_stale_signal", default=(False, None)
)


def reset_stale_signal() -> None:
    """Clear the stale state. Call once at the start of each tool call."""
    _stale_signal.set((False, None))


def get_stale_signal() -> tuple[bool, str | None]:
    """Return (stale, reason) for the most recent fetch chain in this context."""
    return _stale_signal.get()


def _mark_stale(reason: str) -> None:
    """Record that a stale-cache fallback was served this context.

    If multiple fetches in one chain are stale, we keep the FIRST reason
    (it's usually the most informative — the originating upstream failure).
    """
    cur_stale, _ = _stale_signal.get()
    if not cur_stale:
        _stale_signal.set((True, reason))


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
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                # Graceful degradation: when upstream is unreachable, fall
                # back to the most-recent cached payload (regardless of TTL)
                # rather than raising and breaking the agent's chain of
                # reasoning. The staleness is surfaced via the _stale_signal
                # ContextVar and ends up in DataResponse.stale / stale_reason.
                fallback = await self.cache.get_stale(url)
                if fallback is not None:
                    payload, cached_at = fallback
                    age_min = max(0, int((time.time() - cached_at) / 60))
                    if isinstance(e, httpx.HTTPStatusError):
                        upstream = f"RBA CDN returned {e.response.status_code}"
                    else:
                        upstream = f"RBA CDN unreachable ({type(e).__name__})"
                    _mark_stale(
                        f"{upstream} for {url}; serving cached payload from "
                        f"~{age_min} minute(s) ago"
                    )
                    future.set_result(payload)
                    return payload
                # Genuinely no cache to fall back to — preserve original behaviour
                if isinstance(e, httpx.HTTPStatusError):
                    raise RBAAPIError(
                        f"RBA CDN returned {e.response.status_code} for {url}"
                    ) from e
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
