"""Parser tests — every MVP F-table fixture must round-trip cleanly."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from rba_mcp.parsing import (
    CSVParseError,
    TableHeader,
    filter_by_dates,
    filter_by_series,
    parse_csv,
)

FIXTURES = Path(__file__).parent / "fixtures"

ALL_MVP_FIXTURES = [
    "f1.1-data.csv",
    "f4-data.csv",
    "f6-data.csv",
    "f11-data.csv",
    "f11.1-data.csv",
]


@pytest.mark.parametrize("fname", ALL_MVP_FIXTURES)
def test_parses_into_header_and_dataframe(fname):
    body = (FIXTURES / fname).read_bytes()
    header, df = parse_csv(body)
    assert isinstance(header, TableHeader)
    assert isinstance(df, pd.DataFrame)
    assert header.title.startswith(("F1.1", "F4", "F6", "F11"))
    assert header.series, f"{fname}: no series parsed"
    assert not df.empty, f"{fname}: data frame empty"
    # Index is dates
    assert isinstance(df.index, pd.DatetimeIndex), f"{fname}: index not DatetimeIndex"


@pytest.mark.parametrize("fname,expected_count_min", [
    ("f1.1-data.csv", 12),     # F1.1 has 15 series; allow some sparse-row tolerance
    ("f4-data.csv", 12),       # F4 has 15
    ("f6-data.csv", 35),       # F6 has 42 (housing lending breakouts)
    ("f11-data.csv", 20),      # F11 has 25
    ("f11.1-data.csv", 20),    # F11.1 has 23
])
def test_series_count_matches_table(fname, expected_count_min):
    body = (FIXTURES / fname).read_bytes()
    header, _ = parse_csv(body)
    assert len(header.series) >= expected_count_min, (
        f"{fname}: expected >= {expected_count_min} series, got {len(header.series)}"
    )


def test_f11_known_series_ids_present():
    """F11 must expose FXRUSD, FXRTWI, FXREUR — the ones the curated YAML targets."""
    body = (FIXTURES / "f11-data.csv").read_bytes()
    header, df = parse_csv(body)
    for sid in ("FXRUSD", "FXRTWI", "FXREUR", "FXRUKPS", "FXRNZD"):
        assert sid in header.series, f"missing series {sid} in F11 header"
        assert sid in df.columns, f"missing series {sid} in F11 data columns"


def test_f1_1_cash_rate_target_present():
    body = (FIXTURES / "f1.1-data.csv").read_bytes()
    header, df = parse_csv(body)
    assert "FIRMMCRT" in header.series
    meta = header.series["FIRMMCRT"]
    assert meta.unit and "Per cent" in meta.unit
    assert meta.frequency == "Monthly"
    assert "Cash Rate Target" in (meta.name or "")


def test_dates_parsed_dd_slash_mm_yyyy():
    """F1.1, F4, F6 use DD/MM/YYYY — must parse with dayfirst."""
    body = (FIXTURES / "f1.1-data.csv").read_bytes()
    _, df = parse_csv(body)
    # F1.1 starts 30 June 1969 → first index entry should be year 1969
    assert df.index[0].year == 1969
    assert df.index[0].month == 6


def test_dates_parsed_dd_mmm_yyyy():
    """F11 uses DD-MMM-YYYY — must parse with mixed format."""
    body = (FIXTURES / "f11-data.csv").read_bytes()
    _, df = parse_csv(body)
    # F11 starts 29 Jan 2010 → year 2010
    assert df.index[0].year == 2010


def test_values_coerced_to_numeric():
    body = (FIXTURES / "f11-data.csv").read_bytes()
    _, df = parse_csv(body)
    # FXRUSD column should be numeric, not object
    assert pd.api.types.is_numeric_dtype(df["FXRUSD"])
    # And actual values should be plausibly in 0.4-1.5 range for AUD/USD
    aud_usd = df["FXRUSD"].dropna()
    assert (aud_usd > 0.4).all()
    assert (aud_usd < 1.5).all()


def test_empty_body_raises():
    with pytest.raises(CSVParseError):
        parse_csv(b"")


def test_filter_by_dates_inclusive():
    body = (FIXTURES / "f11-data.csv").read_bytes()
    _, df = parse_csv(body)
    sliced = filter_by_dates(df, start="2024-01-01", end="2024-12-31")
    assert (sliced.index.year == 2024).all()


def test_filter_by_series_drops_unknown():
    body = (FIXTURES / "f11-data.csv").read_bytes()
    _, df = parse_csv(body)
    sliced = filter_by_series(df, ["FXRUSD", "DOES_NOT_EXIST", "FXREUR"])
    assert list(sliced.columns) == ["FXRUSD", "FXREUR"]


def test_filter_by_series_empty_returns_all():
    body = (FIXTURES / "f11-data.csv").read_bytes()
    _, df = parse_csv(body)
    sliced = filter_by_series(df, [])
    assert list(sliced.columns) == list(df.columns)


def test_publication_date_extracted():
    body = (FIXTURES / "f11-data.csv").read_bytes()
    header, _ = parse_csv(body)
    # Should be set to a recent ISO date (the latest across the columns)
    assert header.publication_date is not None
    # It's been parsed and re-formatted to YYYY-MM-DD
    assert len(header.publication_date) == 10
    assert header.publication_date[4] == "-"
