"""Cache-layer tests (offline)."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from rba_mcp.cache import Cache


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cache.db"


async def test_basic_set_get_roundtrip(db_path):
    cache = Cache(db_path)
    await cache.set("k", b"v", kind="data")
    assert await cache.get("k", timedelta(hours=1)) == b"v"


async def test_ttl_expiry(db_path):
    """A row written longer ago than the requested TTL must return None."""
    cache = Cache(db_path)
    await cache.set("k", b"v", kind="data")
    # Zero-second TTL: the row is older than 0 seconds, so it should miss.
    await asyncio.sleep(0.01)
    assert await cache.get("k", timedelta(seconds=0)) is None


async def test_clear_by_kind(db_path):
    cache = Cache(db_path)
    await cache.set("a", b"x", kind="data")
    await cache.set("b", b"y", kind="latest")
    await cache.clear(kind="latest")
    assert await cache.get("a", timedelta(hours=1)) == b"x"
    assert await cache.get("b", timedelta(hours=1)) is None


async def test_corrupt_cache_file_self_heals(db_path):
    """A pre-existing corrupt cache.db must auto-recover, not leak sqlite3.DatabaseError.

    Regression for a real bug: previously, `Cache._ensure_init()` propagated
    `sqlite3.DatabaseError("file is not a database")` to the caller whenever
    cache.db was corrupt (e.g. partial write after a crash, schema from an
    older version, or user accident). The cache is an optimisation layer,
    not a source of truth — corruption should be silently recovered, not
    surfaced as a raw library exception (gate 4).
    """
    db_path.write_bytes(b"definitely not a sqlite database")
    cache = Cache(db_path)
    # First op triggers init; corruption should be silently healed.
    assert await cache.get("k", timedelta(hours=1)) is None
    await cache.set("k", b"v", kind="data")
    assert await cache.get("k", timedelta(hours=1)) == b"v"


async def test_zero_byte_cache_file_self_heals(db_path):
    """An empty (0-byte) cache.db must also be recovered."""
    db_path.touch()
    assert db_path.stat().st_size == 0
    cache = Cache(db_path)
    await cache.set("k", b"v", kind="data")
    assert await cache.get("k", timedelta(hours=1)) == b"v"
