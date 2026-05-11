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
