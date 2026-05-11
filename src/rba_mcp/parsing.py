"""RBA F-table CSV parser.

RBA serves statistical tables as CSV with a label-prefixed metadata block
(rows 1-10 of column 0 carry labels like "Title", "Description", "Frequency",
"Type", "Units", "Source", "Publication date", "Series ID") then a data
section starting at row 11.

This file does the parsing once. Everything downstream (shaping, server)
consumes the (TableHeader, DataFrame) tuple returned by `parse_csv`.

Why label-driven and not positional: RBA can add a "Notes" row tomorrow
without warning, and we want the parser to keep working with any unknown
labels logged-and-skipped rather than collapsing.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from io import BytesIO, StringIO

import pandas as pd

# Lower-cased label in column 0 → metadata field name. Anything not in this
# map is logged (in test fixtures) and skipped at runtime.
_LABEL_TO_FIELD: dict[str, str] = {
    "title": "name",                # row 1: per-series long names
    "description": "description",   # row 2: longer per-series descriptions
    "frequency": "frequency",       # row 3: "Daily" / "Monthly" / "See notes"
    "type": "type",                 # row 4: "Original" / "Indicative" / etc.
    "units": "unit",                # row 5: "Per cent" / "USD" / "Index" / etc.
    "source": "source",             # row 8: data provider
    "publication date": "publication_date",  # row 9: latest release date per series
    "series id": "series_id",       # row 10: canonical RBA machine key (e.g. "FXRUSD")
}

_REQUIRED_FIELDS = ("series_id",)  # without this we can't index data columns


@dataclass(frozen=True)
class SeriesMeta:
    """Per-series metadata extracted from the CSV header block."""
    series_id: str
    name: str | None = None
    description: str | None = None
    frequency: str | None = None
    type: str | None = None
    unit: str | None = None
    source: str | None = None
    publication_date: str | None = None


@dataclass(frozen=True)
class TableHeader:
    """Metadata for one F-table.

    `series` is keyed by canonical RBA series ID (the value in CSV row 10).
    """
    title: str                                  # row 0 col 0, e.g. "F11 EXCHANGE RATES"
    series: dict[str, SeriesMeta] = field(default_factory=dict)
    publication_date: str | None = None         # max across series, useful as a single value


class CSVParseError(Exception):
    """Raised when an RBA CSV doesn't match the expected metadata-block shape."""


def parse_csv(body: bytes) -> tuple[TableHeader, pd.DataFrame]:
    """Parse an RBA F-table CSV into (header, data).

    The returned DataFrame has a DatetimeIndex (named "date") and one column
    per series, named by the canonical RBA series ID. Missing observations
    are NaN (pandas-native).
    """
    if not body:
        raise CSVParseError("empty CSV body")

    # Pass 1: metadata block. Use stdlib csv — pandas is hostile to the
    # variable column counts in RBA's header (row 0 has 1 col, row 1 has N).
    text = body.decode("utf-8-sig", errors="replace")
    reader = csv.reader(StringIO(text))
    raw_rows: list[list[str]] = []
    for i, row in enumerate(reader):
        raw_rows.append(row)
        if i >= 30:                # well past the header on every observed table
            break

    if not raw_rows:
        raise CSVParseError("CSV had no rows")

    title = (raw_rows[0][0].strip() if raw_rows[0] else "")
    n_data_cols = max((len(r) for r in raw_rows), default=0)

    by_field: dict[str, list[str]] = {}
    data_start_row = 11                # default; corrected below if a date appears earlier

    for row_idx in range(1, len(raw_rows)):
        row = raw_rows[row_idx]
        if not row:
            continue
        label_raw = row[0] if row else ""
        label = label_raw.strip().lower()
        # If this row's first cell parses as a date, we've reached the data block.
        if label and _looks_like_date(label):
            data_start_row = row_idx
            break
        if not label:
            continue
        field_name = _LABEL_TO_FIELD.get(label)
        if field_name is None:
            continue
        # Pad the row to n_data_cols
        padded = [row[i] if i < len(row) else "" for i in range(n_data_cols)]
        by_field[field_name] = [v.strip() for v in padded]

    for required in _REQUIRED_FIELDS:
        if required not in by_field:
            raise CSVParseError(
                f"RBA CSV missing required metadata row '{required}' "
                f"(found rows: {sorted(by_field)})"
            )

    series_ids_row = by_field["series_id"]
    series: dict[str, SeriesMeta] = {}
    # Position 0 of every metadata row holds the *label* (e.g. "Series ID"),
    # so the actual data columns start at position 1.
    for col_idx in range(1, n_data_cols):
        sid = series_ids_row[col_idx] if col_idx < len(series_ids_row) else ""
        if not sid:
            continue
        series[sid] = SeriesMeta(
            series_id=sid,
            name=_get(by_field, "name", col_idx),
            description=_get(by_field, "description", col_idx),
            frequency=_get(by_field, "frequency", col_idx),
            type=_get(by_field, "type", col_idx),
            unit=_get(by_field, "unit", col_idx),
            source=_get(by_field, "source", col_idx),
            publication_date=_get(by_field, "publication_date", col_idx),
        )

    # Latest publication date across all series (single response-level value)
    pub_dates = [s.publication_date for s in series.values() if s.publication_date]
    latest_pub: str | None = None
    if pub_dates:
        try:
            parsed = pd.to_datetime(pub_dates, format="mixed", dayfirst=True, errors="coerce")
            latest_ts = parsed.max()
            if pd.notna(latest_ts):
                latest_pub = latest_ts.strftime("%Y-%m-%d")
        except Exception:
            latest_pub = max(pub_dates)  # lexicographic fallback

    header = TableHeader(title=title, series=series, publication_date=latest_pub)

    # Pass 2: data rows. We know data_start_row from the pass-1 walk.
    # `names=range(n_data_cols)` forces pandas to expect a wide row even when
    # a sparse data row is missing trailing columns.
    data = pd.read_csv(
        BytesIO(body),
        skiprows=data_start_row,
        header=None,
        names=list(range(n_data_cols)),
        encoding="utf-8-sig",
        encoding_errors="replace",
        na_values=["", "NA", "n/a", " "],
        engine="python",           # stays robust to ragged rows
        on_bad_lines="skip",
    )

    if data.empty:
        return header, data

    # First column is the date; rest are the data values. Coerce the date
    # column with mixed format (RBA uses both DD-MMM-YYYY and DD/MM/YYYY).
    date_col = data.columns[0]
    data[date_col] = pd.to_datetime(
        data[date_col], format="mixed", dayfirst=True, errors="coerce"
    )
    data = data.dropna(subset=[date_col])
    data = data.set_index(date_col)
    data.index.name = "date"

    # Rename data columns to the canonical series IDs from the header. We do
    # it positionally, but only assign IDs that we actually parsed.
    new_cols: list[str] = []
    series_ids_aligned = series_ids_row[1:]   # drop the label cell at position 0
    for i in range(len(data.columns)):
        if i < len(series_ids_aligned) and series_ids_aligned[i]:
            new_cols.append(series_ids_aligned[i])
        else:
            new_cols.append(f"_unknown_{i}")
    data.columns = new_cols

    # Coerce all series columns to numeric (some legacy tables include "n/a"
    # strings or other junk — coerce to NaN rather than crashing).
    for col in data.columns:
        data[col] = pd.to_numeric(data[col], errors="coerce")

    return header, data


def _get(by_field: dict[str, list[str]], field_name: str, col_idx: int) -> str | None:
    row = by_field.get(field_name)
    if row is None or col_idx >= len(row):
        return None
    val = row[col_idx]
    return val if val else None


def _looks_like_date(s: str) -> bool:
    """Cheap pre-check to distinguish a date row from a label row in the header walk.

    Only the first character matters: dates start with a digit, labels start
    with a letter. RBA dates are `DD-MMM-YYYY` or `DD/MM/YYYY` — both
    digit-first.
    """
    return bool(s) and s[0].isdigit()


def _end_of_period(end_str: str) -> pd.Timestamp:
    """Expand a partial date string to the LAST instant of its period.

    'YYYY'       → that year's 31 December 23:59:59
    'YYYY-MM'    → that month's last day 23:59:59
    'YYYY-MM-DD' → that day 23:59:59

    Without this expansion, `end_date="2024"` would be parsed as 2024-01-01
    and the inclusive comparison would exclude all of 2024 after Jan 1.
    """
    n = len(end_str)
    if n == 4:
        freq = "Y"
    elif n == 7:
        freq = "M"
    else:
        freq = "D"
    return pd.Period(end_str, freq=freq).end_time


def filter_by_dates(
    data: pd.DataFrame, start: str | None = None, end: str | None = None
) -> pd.DataFrame:
    """Slice the data DataFrame by an inclusive date range.

    Accepts ISO-like strings: 'YYYY', 'YYYY-MM', 'YYYY-MM-DD'.
    Lenient — invalid strings fall through unfiltered.

    `start` snaps to the FIRST instant of its period; `end` snaps to the
    LAST instant of its period. So `start='2024', end='2024'` means
    "all of 2024", not just 1 January.
    """
    if data.empty or (start is None and end is None):
        return data
    out = data
    if start:
        try:
            out = out[out.index >= pd.to_datetime(start)]
        except (ValueError, TypeError):
            pass
    if end:
        try:
            out = out[out.index <= _end_of_period(end)]
        except (ValueError, TypeError):
            pass
    return out


def filter_by_series(data: pd.DataFrame, series_ids: list[str]) -> pd.DataFrame:
    """Slice the data DataFrame to only the requested series IDs.

    Silently drops requested IDs that aren't present in the data — the caller
    is responsible for reporting "unknown series".
    """
    if not series_ids:
        return data
    keep = [s for s in series_ids if s in data.columns]
    if not keep:
        return data.iloc[:, 0:0]    # empty frame, preserves index type
    return data[keep]
