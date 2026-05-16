"""Live integration tests against the real RBA CDN.

Marked `live` so they only run with `pytest -m live`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from rba_mcp import server
from rba_mcp.cache import Cache
from rba_mcp.client import RBAClient

pytestmark = pytest.mark.live


@pytest.fixture
async def live_client(tmp_path: Path):
    client = RBAClient(cache=Cache(tmp_path / "live_cache.db"))
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
def patch_server_client(live_client, monkeypatch):
    async def _get_live_client():
        return live_client
    monkeypatch.setattr(server, "_get_client", _get_live_client)


async def test_search_cash_rate_finds_f1_1():
    results = await server.search_tables("cash rate", limit=10)
    assert any(r.id == "F1.1" for r in results[:3])


async def test_get_data_aud_usd_2024_returns_records():
    resp = await server.get_data(
        table_id="F11", series="aud_usd", start_date="2024"
    )
    assert len(resp.records) >= 12
    for r in resp.records:
        if r.value is not None:
            assert 0.4 <= r.value <= 1.5


async def test_latest_cash_rate_target():
    resp = await server.latest(table_id="F1.1", series="cash_rate_target")
    assert len(resp.records) == 1
    obs = resp.records[0]
    # Cash rate plausibly in [0, 20]
    assert 0 <= obs.value <= 20
    assert obs.unit == "Per cent per annum"


async def test_describe_curated_returns_plain_english():
    detail = await server.describe_table("F11.1")
    assert detail.is_curated is True
    keys = {s.key for s in detail.series}
    assert "aud_usd" in keys


async def test_describe_non_curated_returns_raw_metadata():
    """F3 is not curated; describe should fetch + parse the CSV and return raw IDs."""
    detail = await server.describe_table("F3")
    assert detail.is_curated is False
    assert len(detail.series) >= 1
    # Series IDs are raw RBA codes, not plain English
    assert all(s.key == s.series_id for s in detail.series)


async def test_list_curated_returns_fifteen_via_server():
    assert set(server.list_curated()) == {
        "F1.1", "F2", "F2.1", "F4", "F5", "F6", "F7", "F8", "F11", "F11.1",
        "D1", "D2", "C1", "G3", "E2",
    }


@pytest.mark.parametrize("table, series, low, high, unit", [
    ("F1.1", "cash_rate_target", 0.0, 20.0, "Per cent per annum"),
    ("F4", "term_deposit_12m", 0.0, 20.0, "Per cent per annum"),
    ("F6", "owner_occupier_variable_existing", 1.0, 15.0, "Per cent per annum"),
    ("F11", "aud_usd", 0.4, 1.5, "USD per AUD"),
    ("F11", "twi", 40.0, 110.0, "Index"),
    ("F11.1", "aud_usd", 0.4, 1.5, "USD per AUD"),
    ("F11.1", "aud_eur", 0.3, 1.0, "EUR per AUD"),
    # D-series — credit aggregates. YoY growth typically -5% to +25%; level in $B.
    ("D1", "housing_credit_yoy", -5.0, 30.0, "Per cent"),
    ("D1", "business_credit_yoy", -10.0, 30.0, "Per cent"),
    ("D2", "total_credit_excl_financial_sa", 500.0, 10000.0, "$ billion"),
    ("D2", "owner_occupier_housing_sa", 500.0, 5000.0, "$ billion"),
    ("D2", "business_credit_sa", 100.0, 5000.0, "$ billion"),
    # C1 — credit card statistics (AU monthly aggregates).
    ("C1", "value_of_purchases", 1000.0, 200000.0, "$ million"),
    ("C1", "total_balances", 10000.0, 200000.0, "$ million"),
    # G3 — inflation expectations (typically 0.5%–8%).
    ("G3", "consumer_expectations_1yr", 0.0, 12.0, "Per cent"),
    ("G3", "break_even_10yr", -2.0, 10.0, "Per cent"),
    # E2 — household leverage ratios. DTI 100-250%, assets-to-income 800-1500%.
    ("E2", "household_debt_to_income", 100.0, 250.0, "Per cent"),
    ("E2", "owner_occupier_debt_to_income", 50.0, 200.0, "Per cent"),
    ("E2", "household_assets_to_income", 800.0, 1500.0, "Per cent"),
])
async def test_curated_table_returns_plausible_value(table, series, low, high, unit):
    resp = await server.latest(table_id=table, series=series)
    assert len(resp.records) == 1
    obs = resp.records[0]
    assert low <= obs.value <= high, (
        f"{table}.{series} = {obs.value} out of range [{low}, {high}]"
    )
    assert obs.unit == unit
    assert obs.dimensions["table"] == table


async def test_query_echo_clean_no_default_keys():
    resp = await server.latest(
        table_id="F11.1", series="aud_usd"
    )
    assert resp.query == {"series": "aud_usd"}
    assert all(not k.startswith("_default") for k in resp.query)


async def test_get_data_csv_format():
    resp = await server.get_data(
        table_id="F11", series="aud_usd", start_date="2024", format="csv"
    )
    assert resp.csv is not None
    assert "FXRUSD" in resp.csv
    assert resp.records == []
    assert resp.period["start"] is not None
