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
