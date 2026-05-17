import pytest

from rba_mcp import curated


@pytest.fixture(autouse=True)
def reset():
    curated.reset_registry()
    yield
    curated.reset_registry()


def test_list_ids_returns_sixteen():
    # F1 (daily) added in 0.8.5 so customers asking for "current cash rate"
    # get the freshest daily value, not F1.1's month-end snapshot.
    assert set(curated.list_ids()) == {
        "F1", "F1.1", "F2", "F2.1", "F4", "F5",
        "F6", "F7", "F8", "F11", "F11.1",
        "D1", "D2", "C1", "G3", "E2",
    }


def test_get_f11_loads_series():
    f11 = curated.get("F11")
    assert f11 is not None
    assert f11.csv_filename == "f11-data.csv"
    assert "aud_usd" in f11.series
    assert f11.series["aud_usd"].series_id == "FXRUSD"
    assert f11.search_keywords  # non-empty


def test_get_d1_loads_growth_series():
    d1 = curated.get("D1")
    assert d1 is not None
    assert d1.csv_filename == "d1-data.csv"
    assert "housing_credit_yoy" in d1.series
    assert d1.series["housing_credit_yoy"].series_id == "DGFACH12"
    assert d1.series["total_credit_yoy"].series_id == "DGFAC12"


def test_get_d2_loads_level_series():
    d2 = curated.get("D2")
    assert d2 is not None
    assert d2.csv_filename == "d2-data.csv"
    # Current RBA headline series — non-financial business credit
    assert "business_credit_sa" in d2.series
    assert d2.series["business_credit_sa"].series_id == "DLCACNFBS"
    # Current RBA headline total credit (excl. financial businesses)
    assert "total_credit_excl_financial_sa" in d2.series
    assert d2.series["total_credit_excl_financial_sa"].series_id == "DLCACFS"
    assert d2.series["business_credit_sa"].unit == "$ billion"


def test_get_c1_loads_card_series():
    c1 = curated.get("C1")
    assert c1 is not None
    assert c1.csv_filename == "c1-data.csv"
    assert "value_of_purchases" in c1.series
    assert c1.series["value_of_purchases"].series_id == "CCCCSTPVSA"
    assert c1.series["total_balances"].series_id == "CCCCSBTSA"
    assert c1.series["value_of_purchases"].unit == "$ million"


def test_get_g3_loads_inflation_expectations():
    g3 = curated.get("G3")
    assert g3 is not None
    assert g3.csv_filename == "g3-data.csv"
    assert "consumer_expectations_1yr" in g3.series
    assert g3.series["consumer_expectations_1yr"].series_id == "GCONEXP"
    assert g3.series["break_even_10yr"].series_id == "GBONYLD"


def test_get_e2_loads_household_ratios():
    e2 = curated.get("E2")
    assert e2 is not None
    assert e2.csv_filename == "e2-data.csv"
    assert "household_debt_to_income" in e2.series
    assert e2.series["household_debt_to_income"].series_id == "BHFDDIT"
    assert e2.series["owner_occupier_debt_to_income"].series_id == "BHFDDIO"
    assert e2.series["household_debt_to_income"].unit == "Per cent"


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


def test_translate_series_none_returns_headline_when_defined():
    """series=None now resolves to the curated table's headline series
    (Item 6 — the 'no filter = which series?' bug).

    F11's headline is aud_usd (FXRUSD). Pre-0.8.0 this returned every
    curated series, which looked like duplicate/garbage data to LLMs.
    Callers wanting the full set must pass an explicit list.
    """
    f11 = curated.get("F11")
    assert f11.headline_series == "aud_usd"
    out = curated.translate_series(f11, None)
    assert out == ["FXRUSD"], (
        f"series=None should resolve to the headline (FXRUSD), got {out}"
    )


def test_translate_series_none_returns_all_when_no_headline():
    """If a table has no headline_series defined, fall back to "all curated"
    so this code path keeps working for future tables that don't have a
    single dominant headline."""
    from dataclasses import replace
    f11 = curated.get("F11")
    fake = replace(f11, headline_series=None)
    out = curated.translate_series(fake, None)
    # Order preserved from the YAML
    assert "FXRUSD" in out
    assert "FXRTWI" in out
    assert len(out) == len(fake.series)


def test_all_curated_tables_have_headline_series():
    """Item 6 acceptance: every curated table must declare a headline_series
    so `latest("X")` and `get_data("X")` with no series return a single
    canonical observation rather than a 14-series soup."""
    missing = []
    for cid in curated.list_ids():
        c = curated.get(cid)
        if c.headline_series is None:
            missing.append(cid)
    assert not missing, f"curated tables without headline_series: {missing}"


def test_headline_series_resolves_to_a_known_key():
    """Each headline_series must reference a key defined under `series:` in the
    same file — otherwise the load layer raises, but lock the invariant in
    pytest too so regressions surface in CI rather than at first call."""
    for cid in curated.list_ids():
        c = curated.get(cid)
        if c.headline_series is not None:
            assert c.headline_series in c.series, (
                f"{cid}: headline_series {c.headline_series!r} not in series keys "
                f"{sorted(c.series)}"
            )


def test_headline_series_invalid_key_raises_at_load(tmp_path, monkeypatch):
    """A curated YAML whose headline_series doesn't match any defined series
    must fail loudly at registry load, not silently at query time."""
    bad_yaml = tmp_path / "BADTABLE.yaml"
    bad_yaml.write_text(
        "id: BADTABLE\n"
        "name: Bad table\n"
        "csv_filename: bad-data.csv\n"
        "headline_series: not_a_real_key\n"
        "series:\n"
        "  some_key:\n"
        "    series_id: FAKE\n"
    )
    from rba_mcp import curated as curated_mod

    def fake_yaml_dir():
        return tmp_path
    monkeypatch.setattr(curated_mod, "_yaml_dir", fake_yaml_dir)
    curated_mod.reset_registry()
    with pytest.raises(ValueError, match="headline_series 'not_a_real_key'"):
        curated_mod.list_ids()
    curated_mod.reset_registry()


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
