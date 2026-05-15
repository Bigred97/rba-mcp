import pytest

from rba_mcp import curated


@pytest.fixture(autouse=True)
def reset():
    curated.reset_registry()
    yield
    curated.reset_registry()


def test_list_ids_returns_ten():
    assert set(curated.list_ids()) == {
        "F1.1", "F2", "F2.1", "F4", "F5",
        "F6", "F7", "F8", "F11", "F11.1",
    }


def test_get_f11_loads_series():
    f11 = curated.get("F11")
    assert f11 is not None
    assert f11.csv_filename == "f11-data.csv"
    assert "aud_usd" in f11.series
    assert f11.series["aud_usd"].series_id == "FXRUSD"
    assert f11.search_keywords  # non-empty


def test_get_case_insensitive():
    assert curated.get("f11") is not None
    assert curated.get("F11") is not None


def test_get_returns_none_for_unknown():
    assert curated.get("F99") is None


def test_translate_series_plain_english():
    f11 = curated.get("F11")
    assert curated.translate_series(f11, "aud_usd") == ["FXRUSD"]
    assert curated.translate_series(f11, ["aud_usd", "aud_eur"]) == ["FXRUSD", "FXREUR"]


def test_translate_series_raw_id_passes_through():
    f11 = curated.get("F11")
    assert curated.translate_series(f11, "FXRUSD") == ["FXRUSD"]
    assert curated.translate_series(f11, ["FXRUSD", "FXRTWI"]) == ["FXRUSD", "FXRTWI"]


def test_translate_series_none_returns_all_curated():
    f11 = curated.get("F11")
    out = curated.translate_series(f11, None)
    assert "FXRUSD" in out
    assert "FXRTWI" in out
    assert len(out) == len(f11.series)


def test_translate_series_unknown_raises_with_hint():
    f11 = curated.get("F11")
    with pytest.raises(ValueError, match="Unknown series 'aud_atlantis'"):
        curated.translate_series(f11, "aud_atlantis")


def test_translate_series_empty_list_raises():
    f11 = curated.get("F11")
    with pytest.raises(ValueError, match="empty list"):
        curated.translate_series(f11, [])


def test_translate_series_empty_value_raises():
    f11 = curated.get("F11")
    with pytest.raises(ValueError, match="Empty series value"):
        curated.translate_series(f11, "   ")


def test_translate_series_strips_whitespace():
    f11 = curated.get("F11")
    assert curated.translate_series(f11, "  aud_usd  ") == ["FXRUSD"]


def test_f1_1_cash_rate_target_resolves():
    """The most-asked AU finance question must work via the curated key."""
    f1_1 = curated.get("F1.1")
    assert curated.translate_series(f1_1, "cash_rate_target") == ["FIRMMCRT"]


def test_f6_owner_occupier_variable_existing_resolves():
    """The 'average mortgage rate' question must work."""
    f6 = curated.get("F6")
    assert curated.translate_series(f6, "owner_occupier_variable_existing") == ["FLRHOOVA"]


def test_all_curated_have_csv_filename():
    for cid in curated.list_ids():
        c = curated.get(cid)
        assert c.csv_filename and c.csv_filename.endswith(".csv")


def test_all_curated_have_at_least_one_series():
    for cid in curated.list_ids():
        c = curated.get(cid)
        assert c.series, f"{cid}: no series defined"
