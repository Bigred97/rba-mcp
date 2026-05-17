"""Server-side input validation guards (offline — no network calls)."""
import ast
import pathlib
import re

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


async def test_describe_table_unknown_table_lists_curated_ids():
    """0.7.5: unknown table_id error must NAME the bad value, LIST the curated
    IDs in full, and suggest an action — matching the portfolio's quality
    dimension #5 (Deterministic Error Handling).

    Action phrasing is intentionally transport-agnostic per 0.7.3 — no MCP
    tool names like `search_tables(...)` (the bare-string ban is enforced by
    test_no_mcp_tool_refs_in_error_strings below).
    """
    with pytest.raises(ValueError) as exc_info:
        await server.describe_table("F999")
    msg = str(exc_info.value)
    # Names the rejected value
    assert "'F999'" in msg, f"missing bad-value name: {msg!r}"
    # Lists curated IDs verbatim — spot-check several
    for sample_id in ["F1.1", "F11", "C1", "D1", "E2", "G3"]:
        assert sample_id in msg, f"missing curated ID {sample_id!r}: {msg!r}"
    # Carries an actionable hint
    assert ("Search" in msg or "search" in msg) and "enumerate" in msg, (
        f"missing actionable hint: {msg!r}"
    )


async def test_get_data_unknown_table_lists_curated_ids():
    """Same shape as describe_table — the get_data path must surface the
    curated ID list + action hint, not just the F/D/C/G/E pattern."""
    with pytest.raises(ValueError) as exc_info:
        await server.get_data(table_id="F999")
    msg = str(exc_info.value)
    assert "'F999'" in msg, f"missing bad-value name: {msg!r}"
    for sample_id in ["F1.1", "F11", "C1", "D1", "E2", "G3"]:
        assert sample_id in msg, f"missing curated ID {sample_id!r}: {msg!r}"
    assert ("Search" in msg or "search" in msg) and "enumerate" in msg, (
        f"missing actionable hint: {msg!r}"
    )


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
    with pytest.raises(ValueError, match="must specify which raw"):
        await server.get_data("F3")  # F3 (corporate bond yields) is not curated


async def test_get_data_lowercase_table_normalized():
    """table_id is normalized to uppercase; lowercase 'f11' should resolve."""
    # This will fail later (no network mock) but should NOT fail at validation
    with pytest.raises(ValueError):
        await server.get_data("f11", series="aud_atlantis")  # raises on unknown series, not table


async def test_list_curated_returns_sixteen():
    # F1 (daily cash rate target) added in 0.8.5.
    assert set(server.list_curated()) == {
        "F1", "F1.1", "F2", "F2.1", "F4", "F5",
        "F6", "F7", "F8", "F11", "F11.1",
        "D1", "D2", "C1", "G3", "E2",
    }


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
    AND a list of valid keys — the CLAUDE.md textbook shape.

    The hint is intentionally transport-agnostic (no MCP-tool name like
    `describe_table()`) so it's usable whether the caller is an MCP client,
    a REST gateway, or a script. (0.7.3 — Item 3.)
    """
    with pytest.raises(ValueError) as exc_info:
        # 'aud_us' is one char off from 'aud_usd' — difflib should match.
        await server.get_data("F11", series="aud_us")
    msg = str(exc_info.value)
    assert "Did you mean 'aud_usd'" in msg, f"missing did-you-mean: {msg!r}"
    assert "Valid keys:" in msg, f"missing valid-keys list: {msg!r}"
    # The hint must NOT reference an MCP tool name — it should work for
    # callers behind any transport.
    assert "describe_table" not in msg, f"hint still references MCP tool: {msg!r}"


async def test_invalid_series_id_shape_carries_actionable_hint():
    """Raw series IDs with invalid chars must hint at shape and surface a
    likely correction — no MCP-tool reference required.

    (0.7.3 — Item 3: hints must be transport-agnostic.)
    """
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
    # Difflib should suggest 'FXRUSD' for 'fx rusd'.
    assert "FXRUSD" in msg, f"missing did-you-mean suggestion: {msg!r}"
    assert "describe_table" not in msg, f"hint still references MCP tool: {msg!r}"


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


# ----- 0.7.3 (Item 3): user-facing error hints are transport-agnostic -----
#
# Error messages must not reference MCP-tool names (e.g. `describe_table()`,
# `search_tables()`) or internal API URLs (`www.rba.gov.au/.../csv/...csv`).
# An error from the rba_mcp package should read the same whether the caller
# is an MCP client, a REST gateway, or a Python script calling the functions
# directly.

_SRC_ROOT = pathlib.Path(__file__).resolve().parent.parent / "src" / "rba_mcp"


def _extract_user_facing_strings() -> list[tuple[pathlib.Path, int, str]]:
    """Walk every .py under src/rba_mcp/, parse the AST, and yield only the
    string arguments to `raise <SomeExc>(...)` calls — these are the strings
    users actually see in error reports.

    Skips: docstrings, Field(description=..., examples=...), comments,
    return values. Just rejection-path messages.
    """
    out: list[tuple[pathlib.Path, int, str]] = []
    for py in _SRC_ROOT.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            call = node.exc if isinstance(node.exc, ast.Call) else None
            if call is None:
                continue
            for arg in call.args:
                # We support both Constant strings and JoinedStr (f-strings)
                # by reducing each to a "literal parts only" string.
                pieces: list[str] = []
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    pieces.append(arg.value)
                elif isinstance(arg, ast.JoinedStr):
                    for v in arg.values:
                        if isinstance(v, ast.Constant) and isinstance(v.value, str):
                            pieces.append(v.value)
                elif isinstance(arg, ast.BinOp):
                    # Concatenated strings like "foo" + ", bar" — walk
                    # the binop tree for any Constant string leaves.
                    stack: list[ast.AST] = [arg]
                    while stack:
                        cur = stack.pop()
                        if isinstance(cur, ast.Constant) and isinstance(cur.value, str):
                            pieces.append(cur.value)
                        elif isinstance(cur, ast.BinOp):
                            stack.append(cur.left)
                            stack.append(cur.right)
                        elif isinstance(cur, ast.JoinedStr):
                            for v in cur.values:
                                stack.append(v)
                if pieces:
                    out.append((py, node.lineno, "".join(pieces)))
    return out


def test_no_mcp_tool_refs_in_error_strings():
    """Item 3 acceptance: no error message references an MCP tool by name
    (`describe_table(...)`, `search_tables(...)`, `list_curated(...)`).
    The hint must suggest what to do (look up valid keys, retry, etc.)
    without naming a specific transport's API surface.
    """
    pat = re.compile(r"\b(describe_table|search_tables|list_curated)\s*\(")
    offenders: list[str] = []
    for path, lineno, text in _extract_user_facing_strings():
        if pat.search(text):
            offenders.append(f"{path.relative_to(_SRC_ROOT.parent.parent)}:{lineno}: {text!r}")
    assert not offenders, (
        "User-facing error messages reference MCP tool names — "
        "these are transport-specific and shouldn't leak through ValueError. "
        "Replace with transport-agnostic hints (e.g. 'See the valid-series list "
        f"for X').\n  {chr(10).join(offenders)}"
    )


def test_no_internal_csv_urls_in_error_strings():
    """Item 3 acceptance: no error message embeds the internal RBA CDN URL
    or CSV-filename — those are implementation details, not actionable
    customer-facing information.
    """
    bad = re.compile(r"(www\.rba\.gov\.au|\.rba\.gov\.au/[a-z]|\b[a-z0-9.]+-data\.csv\b)")
    offenders: list[str] = []
    for path, lineno, text in _extract_user_facing_strings():
        if bad.search(text):
            offenders.append(f"{path.relative_to(_SRC_ROOT.parent.parent)}:{lineno}: {text!r}")
    assert not offenders, (
        "User-facing error messages embed internal RBA CDN URLs or CSV "
        "filenames. Strip them — these are implementation details that "
        "don't help the caller.\n  " + "\n  ".join(offenders)
    )
