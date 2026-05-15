import pytest

from rba_mcp import curated, tables


@pytest.fixture(autouse=True)
def reset():
    curated.reset_registry()
    tables.reset_registry()
    yield
    curated.reset_registry()
    tables.reset_registry()


def test_list_tables_returns_19():
    summaries = tables.list_tables()
    assert len(summaries) == 19


def test_curated_tables_marked_curated():
    summaries = tables.list_tables()
    by_id = {s.id: s for s in summaries}
    for cid in ("F1.1", "F2", "F2.1", "F4", "F5", "F6", "F7", "F8", "F11", "F11.1"):
        assert by_id[cid].is_curated, f"{cid} should be marked curated"
    assert not by_id["F3"].is_curated  # F3 (corporate bonds) is not curated


def test_get_csv_filename_for_dot_id():
    """F1.1 → f1.1-data.csv (not f1_1, not f1-1)."""
    assert tables.get_csv_filename("F1.1") == "f1.1-data.csv"
    assert tables.get_csv_filename("F11.1") == "f11.1-data.csv"
    assert tables.get_csv_filename("F11") == "f11-data.csv"


def test_get_csv_filename_unknown():
    assert tables.get_csv_filename("F999") is None


@pytest.mark.parametrize("query,expected_top_id", [
    ("cash rate", "F1.1"),
    ("cash rate target", "F1.1"),
    ("monetary policy", "F1.1"),
    ("aud usd", "F11.1"),
    ("exchange rate today", "F11.1"),
    ("mortgage", "F6"),
    ("home loan", "F6"),
    ("term deposit", "F4"),
    ("savings", "F4"),
    ("trade weighted index", "F11"),
])
def test_search_finds_curated_at_top(query, expected_top_id):
    """Common AU-finance queries must hit the right curated F-table at #1 or #2."""
    results = tables.search_tables(query, limit=5)
    top_ids = [r.id for r in results[:3]]
    assert expected_top_id in top_ids, (
        f"'{query}' should find {expected_top_id} in top 3; got {top_ids}"
    )


def test_search_empty_query_raises():
    with pytest.raises(ValueError, match="query is required"):
        tables.search_tables("")
