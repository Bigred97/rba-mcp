from pathlib import Path

import pandas as pd
import pytest

from rba_mcp import curated as curated_mod
from rba_mcp.parsing import parse_csv
from rba_mcp.shaping import build_response, to_csv, to_records, to_series

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def reset():
    curated_mod.reset_registry()
    yield
    curated_mod.reset_registry()


def test_to_records_unit_attribution():
    body = (FIXTURES / "f11-data.csv").read_bytes()
    header, df = parse_csv(body)
    f11 = curated_mod.get("F11")
    records = to_records(df[["FXRUSD"]], header, "F11", curated=f11)
    assert records
    obs = records[0]
    # Curated YAML overrides unit string
    assert obs.unit == "USD per AUD"
    assert obs.dimensions["table"] == "F11"
    # Display name comes from curated description
    assert "US dollar" in obs.dimensions["series"].lower() or "usd" in obs.dimensions["series"].lower()


def test_to_records_skips_nan():
    body = (FIXTURES / "f4-data.csv").read_bytes()
    header, df = parse_csv(body)
    # F4 has many sparse cells in early dates
    records = to_records(df, header, "F4", curated=curated_mod.get("F4"))
    # No record should have a None value
    assert all(r.value is not None for r in records)


def test_to_csv_returns_string_with_header():
    body = (FIXTURES / "f11-data.csv").read_bytes()
    _, df = parse_csv(body)
    csv = to_csv(df[["FXRUSD"]])
    assert "FXRUSD" in csv
    assert "date" in csv.lower()


def test_to_series_groups_by_series():
    body = (FIXTURES / "f11-data.csv").read_bytes()
    header, df = parse_csv(body)
    f11 = curated_mod.get("F11")
    records = to_records(df[["FXRUSD", "FXREUR"]], header, "F11", curated=f11)
    grouped = to_series(records)
    assert len(grouped) == 2
    assert all("observations" in g for g in grouped)


def test_build_response_records_format():
    body = (FIXTURES / "f11-data.csv").read_bytes()
    header, df = parse_csv(body)
    f11 = curated_mod.get("F11")
    resp = build_response(
        table_id="F11",
        table_name="Exchange Rates",
        df=df[["FXRUSD"]],
        header=header,
        curated=f11,
        fmt="records",
        user_query={"series": "aud_usd"},
        rba_url="https://www.rba.gov.au/statistics/tables/#f11",
    )
    assert resp.table_id == "F11"
    assert resp.unit == "USD per AUD"
    assert resp.records
    assert resp.period["start"] is not None
    assert resp.period["end"] is not None
    assert resp.csv is None
    assert resp.attribution.startswith("Data sourced from")


def test_build_response_csv_format():
    body = (FIXTURES / "f11-data.csv").read_bytes()
    header, df = parse_csv(body)
    f11 = curated_mod.get("F11")
    resp = build_response(
        table_id="F11",
        table_name="Exchange Rates",
        df=df[["FXRUSD"]],
        header=header,
        curated=f11,
        fmt="csv",
        user_query={},
        rba_url="https://www.rba.gov.au/statistics/tables/#f11",
    )
    assert resp.csv is not None
    assert resp.records == []
    assert resp.period["start"] is not None  # derived from underlying records
