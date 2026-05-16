"""End-to-end MCP-protocol smoke tests using FastMCP's in-process Client."""
from __future__ import annotations

import pytest
from fastmcp import Client

from rba_mcp import server

pytestmark = pytest.mark.live


async def test_tools_list_exposes_five_tools():
    async with Client(server.mcp) as c:
        tools = await c.list_tools()
        names = {t.name for t in tools}
    assert names == {
        "search_tables",
        "describe_table",
        "get_data",
        "latest",
        "list_curated",
    }


async def test_each_tool_has_input_schema():
    async with Client(server.mcp) as c:
        tools = await c.list_tools()
        for t in tools:
            assert t.inputSchema is not None
            assert t.description, f"{t.name} has no description"


async def test_call_list_curated_returns_twelve():
    async with Client(server.mcp) as c:
        result = await c.call_tool("list_curated", {})
    assert isinstance(result.data, list)
    assert set(result.data) == {
        "F1.1", "F2", "F2.1", "F4", "F5", "F6", "F7", "F8", "F11", "F11.1",
        "D1", "D2",
    }


async def test_call_search_finds_cash_rate():
    async with Client(server.mcp) as c:
        result = await c.call_tool("search_tables", {"query": "cash rate", "limit": 5})
    ids = [item.id for item in result.data]
    assert "F1.1" in ids


async def test_call_latest_aud_usd_via_mcp():
    """End-to-end MCP call for the headline FX query."""
    async with Client(server.mcp) as c:
        result = await c.call_tool(
            "latest", {"table_id": "F11.1", "series": "aud_usd"}
        )
    payload = result.data
    assert payload.table_id == "F11.1"
    assert len(payload.records) == 1
    obs = payload.records[0]
    # Sanity: AUD/USD plausibly between 0.4 and 1.5
    assert 0.4 <= obs.value <= 1.5
    assert obs.unit == "USD per AUD"


async def test_call_describe_curated_via_mcp():
    async with Client(server.mcp) as c:
        result = await c.call_tool("describe_table", {"table_id": "F11"})
    detail = result.data
    assert detail.is_curated is True
    keys = {s.key for s in detail.series}
    assert "aud_usd" in keys
    assert "twi" in keys
