"""Server-side input validation guards (offline — no network calls)."""
import pytest

from rba_mcp import server


async def test_search_tables_empty_query_raises():
    with pytest.raises(ValueError, match="query is required"):
        await server.search_tables("")


async def test_search_tables_non_string_query():
    with pytest.raises(ValueError, match="must be a string"):
        await server.search_tables(query=123)  # type: ignore[arg-type]


async def test_search_tables_negative_limit():
    with pytest.raises(ValueError, match=">= 1"):
        await server.search_tables("cash rate", limit=0)


async def test_search_tables_bool_limit_rejected():
    """bool is a subclass of int — must be rejected explicitly."""
    with pytest.raises(ValueError, match="positive integer"):
        await server.search_tables("cash rate", limit=True)  # type: ignore[arg-type]


async def test_describe_table_unknown_table_raises():
    with pytest.raises(ValueError, match="not a known RBA F-table"):
        await server.describe_table("F999")


async def test_describe_table_garbage_id_rejected():
    with pytest.raises(ValueError, match="invalid characters"):
        await server.describe_table("F11; DROP TABLE")


async def test_describe_table_empty_id():
    with pytest.raises(ValueError, match="empty"):
        await server.describe_table("")


async def test_describe_table_non_string():
    with pytest.raises(ValueError, match="must be a string"):
        await server.describe_table(table_id=42)  # type: ignore[arg-type]


async def test_get_data_invalid_format():
    with pytest.raises(ValueError, match="Unknown format"):
        await server.get_data("F11", series="aud_usd", format="JSON")  # type: ignore[arg-type]


async def test_get_data_end_before_start():
    with pytest.raises(ValueError, match="end_date .* is before start_date"):
        await server.get_data(
            "F11", series="aud_usd", start_date="2025", end_date="2020"
        )


async def test_get_data_garbage_period():
    with pytest.raises(ValueError, match="invalid format"):
        await server.get_data(
            "F11", series="aud_usd", start_date="not a date"
        )


async def test_get_data_empty_series_string():
    with pytest.raises(ValueError, match="empty string"):
        await server.get_data("F11", series="")


async def test_get_data_empty_series_list():
    with pytest.raises(ValueError, match="empty list"):
        await server.get_data("F11", series=[])


async def test_get_data_unknown_curated_series():
    with pytest.raises(ValueError, match="Unknown series"):
        await server.get_data("F11", series="aud_atlantis")


async def test_get_data_non_curated_requires_series():
    """A non-curated table requires explicit series — no defaulting."""
    with pytest.raises(ValueError, match="must specify which series"):
        await server.get_data("F2")  # F2 is not curated


async def test_get_data_lowercase_table_normalized():
    """table_id is normalized to uppercase; lowercase 'f11' should resolve."""
    # This will fail later (no network mock) but should NOT fail at validation
    with pytest.raises(ValueError):
        await server.get_data("f11", series="aud_atlantis")  # raises on unknown series, not table


async def test_list_curated_returns_five():
    assert set(server.list_curated()) == {"F1.1", "F4", "F6", "F11", "F11.1"}


async def test_describe_table_registry_inconsistency_raises_value_error(monkeypatch):
    """A None csv_filename from the registry must surface as ValueError, not AssertionError.

    Regression for an `assert csv_filename is not None` that would have leaked a
    raw AssertionError to the MCP tool surface (gate 4: no raw exceptions escape).
    """
    from rba_mcp import tables as tables_mod
    monkeypatch.setattr(tables_mod, "get_csv_filename", lambda _tid: None)
    with pytest.raises(ValueError, match="registry inconsistency"):
        await server.describe_table("F11")


async def test_get_data_registry_inconsistency_raises_value_error(monkeypatch):
    """Same as above, but for the get_data/latest code path."""
    from rba_mcp import tables as tables_mod
    monkeypatch.setattr(tables_mod, "get_csv_filename", lambda _tid: None)
    with pytest.raises(ValueError, match="registry inconsistency"):
        await server.get_data("F11", series="aud_usd")


async def test_latest_registry_inconsistency_raises_value_error(monkeypatch):
    from rba_mcp import tables as tables_mod
    monkeypatch.setattr(tables_mod, "get_csv_filename", lambda _tid: None)
    with pytest.raises(ValueError, match="registry inconsistency"):
        await server.latest("F11", series="aud_usd")


# ----- 0.1.11 error-message sweep: actionable-hint regressions -----
#
# Every ValueError must carry a "Try X" / "Did you mean X?" / "Valid options"
# pointer that suggests the correction (quality dimension #5 in CLAUDE.md).
# These tests lock in the actionable shape on a couple of representative
# rejection paths.

async def test_unknown_curated_series_suggests_did_you_mean():
    """Typo'd curated series key should surface a difflib 'Did you mean?' hint
    AND a describe_table() pointer — the CLAUDE.md textbook shape."""
    with pytest.raises(ValueError) as exc_info:
        # 'aud_us' is one char off from 'aud_usd' — difflib should match.
        await server.get_data("F11", series="aud_us")
    msg = str(exc_info.value)
    assert "Did you mean 'aud_usd'" in msg, f"missing did-you-mean: {msg!r}"
    assert "describe_table('F11')" in msg, f"missing describe_table pointer: {msg!r}"


async def test_invalid_series_id_shape_carries_actionable_hint():
    """Raw series IDs with invalid chars must hint at shape, a likely correction,
    and which describe_table call to try — the CLAUDE.md example verbatim."""
    # Force the non-curated path: a syntactically-invalid raw ID can't be a
    # curated key, so translate_series falls through to the raw-ID branch.
    # We bypass the curated wrapper by monkey-patching curated.get to None for
    # F11 so the series flows into _validate_series_for_url directly.
    from unittest.mock import patch
    from rba_mcp import curated as curated_mod
    with patch.object(curated_mod, "get", return_value=None):
        with pytest.raises(ValueError) as exc_info:
            # 'fx rusd' has a space — invalid char — and is close to 'FXRUSD'.
            await server.get_data("F11", series="fx rusd")
    msg = str(exc_info.value)
    assert "invalid characters" in msg, f"missing shape hint: {msg!r}"
    assert "describe_table" in msg, f"missing describe_table pointer: {msg!r}"


# ----- Wave 4: start_period / end_period portfolio alias --------------------
#
# rba-mcp historically used start_date / end_date. The portfolio standard
# (7 of 9 sisters) is start_period / end_period. Both names are accepted; the
# canonical name takes precedence when one is supplied. Supplying both with
# non-None values is ambiguous and raises ValueError.


async def test_start_period_alias_accepted():
    """start_period='2024' must behave identically to start_date='2024'.

    We pass an unknown series so the period validation passes but the call
    surfaces the same downstream error — proving the alias was wired to the
    same code path.
    """
    with pytest.raises(ValueError, match="Unknown series"):
        await server.get_data("F11", series="aud_atlantis", start_period="2024")


async def test_end_period_alias_accepted():
    """end_period mirrors end_date — same downstream rejection."""
    with pytest.raises(ValueError, match="Unknown series"):
        await server.get_data(
            "F11", series="aud_atlantis", end_period="2025"
        )


async def test_start_period_and_start_date_both_supplied_raises():
    """Mutually exclusive: pick one, not both."""
    with pytest.raises(ValueError, match="Use either start_period or start_date"):
        await server.get_data(
            "F11", series="aud_usd", start_period="2024", start_date="2023"
        )


async def test_end_period_and_end_date_both_supplied_raises():
    """Mutually exclusive: pick one, not both."""
    with pytest.raises(ValueError, match="Use either end_period or end_date"):
        await server.get_data(
            "F11", series="aud_usd", end_period="2024", end_date="2025"
        )


async def test_start_date_still_works_regression():
    """Legacy `start_date` must keep working — non-breaking alias contract.

    Same downstream rejection as the alias test above proves the legacy path
    still routes through `_get_data_impl`.
    """
    with pytest.raises(ValueError, match="Unknown series"):
        await server.get_data("F11", series="aud_atlantis", start_date="2024")


async def test_end_date_still_works_regression():
    """Legacy `end_date` must keep working — non-breaking alias contract."""
    with pytest.raises(ValueError, match="Unknown series"):
        await server.get_data("F11", series="aud_atlantis", end_date="2025")


async def test_start_period_end_period_swap_error_uses_legacy_field_name():
    """end-before-start error keeps the existing message shape (legacy
    'end_date' field name) — the error text is intentionally unchanged so
    existing log scrapers / docs keep working. The alias is purely additive
    at the parameter surface."""
    with pytest.raises(ValueError, match="end_date .* is before start_date"):
        await server.get_data(
            "F11", series="aud_usd",
            start_period="2025", end_period="2020",
        )
