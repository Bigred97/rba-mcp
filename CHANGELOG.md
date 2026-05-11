# Changelog

## 0.1.5 (2026-05-11)

Round-2 stress-test follow-up. Two "new bugs" reported in the second customer
probe pass turned out to be **stale `uvx` cache artifacts** — the tester's
Claude Desktop was running a pre-0.1.4 wheel, so 0.1.4's dedup fix and 0.1.2's
`_is_valid_period` calendar check weren't in effect. Both reproducers verified
to NOT reproduce on 0.1.4+. Hardening shipped anyway:

- **New: `DataResponse.server_version`** field, echoed in every response.
  Set from `importlib.metadata.version("rba-mcp")`. The previous stale-cache
  confusion happened twice; surfacing the running version in every response
  makes it trivial for testers to verify which wheel served their call.
- **Tests**: +4 round-2 regressions in `test_stress_regressions.py` —
  200-duplicate-series dedup, absurd-but-valid future end_date (correct
  inclusive semantics, not a silent bypass), composite invalid-start +
  valid-end (must error on the start), and that `server_version` appears
  in every response. 101 unit tests now (was 97).

**To refresh a cached uvx install:** `uvx --refresh rba-mcp --help`, or
delete the wheel cache (`rm -rf ~/.cache/uv/archive-v0/*rba*`), then
restart Claude Desktop.

## 0.1.4 (2026-05-11)

Real-user stress-test fixes. A customer probed the tool surface in Claude
Desktop and surfaced three correctness bugs that unit tests missed.

- **Fix: `latest()` / `get_data()` dedupe series.** Passing both a curated
  key and the underlying raw series ID for the same series — e.g.
  `series=["aud_usd", "FXRUSD"]` — used to return 4 duplicate records
  with `period="FXRUSD"` (the series ID literal in the date field). Root
  cause: duplicate column names made `df[sid]` return a DataFrame
  instead of a Series, breaking the `to_records` iteration. Now dedupes
  series IDs while preserving order before filtering.
- **Fix: `end_date` partial-period expansion.** `end_date="2024"` used
  to be parsed as `2024-01-01` and the inclusive comparison excluded all
  of 2024 after Jan 1 — so a "full year" query returned zero records.
  Same for `YYYY-MM` (excluded the rest of the month). `end_date` now
  expands to the LAST instant of its period: `YYYY` → 31 Dec, `YYYY-MM`
  → last day of month, `YYYY-MM-DD` → that day. `start_date` semantics
  unchanged (still snaps to the FIRST instant — that's correct for the
  lower bound).
- **Fix: validate non-curated series against CSV header.** Calling
  `get_data("F2", series=["FAKESERIES"])` on a non-curated table used
  to silently return `[]` instead of erroring (curated tables already
  rejected unknown series with a hint). Pipelines couldn't distinguish
  a typo from "no data in date range". Now non-curated paths validate
  requested series against the parsed CSV header and raise `ValueError`
  with the first 10 valid IDs as a hint.
- **Tests**: +10 regression tests in `tests/test_stress_regressions.py`,
  one per bug-class. 97 unit tests passing (was 87).

Note: Bug #4 from the stress-test report (calendar-invalid dates like
`2026-02-30` being accepted) was already fixed in 0.1.2's `_is_valid_period`
semantic check. If you saw that bug, your Claude Desktop was running a
cached older wheel via uvx; refresh with `uvx --refresh rba-mcp` or
restart Claude Desktop.

## 0.1.3 (2026-05-11)

Docs polish — same artifact every successful MCP launch had.

- **Hero screenshot in README** showing Claude Desktop answering "Show me AUD against USD, EUR, GBP and the trade-weighted index since 2024" — four metric cards (AUD/USD 0.7231, AUD/EUR 0.6154, AUD/GBP 0.5323, TWI 66.9), rebased line chart, and macro-context analysis identifying the April 2025 tariff-risk-off trough.
- **"How it works" section** with a second screenshot showing Claude querying the F1.1 monthly cash-rate table (4.10%), noticing it's stale relative to the 5 May 2026 Board hike, and web-searching to surface the post-meeting rate (4.35%). Demonstrates clean composition of MCP-tool + web-search synthesis.
- **`docs/demo.png`** + **`docs/cash-rate.png`** committed to the repo for embedding.

## 0.1.2 (2026-05-11)

Cache-corruption recovery; no behaviour changes for valid inputs.

- **Fix**: `Cache._ensure_init()` now catches `sqlite3.DatabaseError` on
  initial schema setup and self-heals by deleting the corrupt file and
  recreating it. Previously, a corrupt `~/.rba-mcp/cache.db` (from a partial
  write after a crash, an older-version schema, or user accident) would leak
  `sqlite3.DatabaseError("file is not a database")` to the caller — a raw
  library exception escaping the tool surface, against gate 4. The cache is
  a performance optimisation, not a source of truth, so silently recreating
  it is always safe.
- **Tests**: +5 in a new `tests/test_cache.py` (set/get roundtrip, TTL
  expiry, clear-by-kind, corrupt-file self-heal, zero-byte-file self-heal).
  Total: 87 unit + 21 live = 108.

## 0.1.1 (2026-05-11)

Audit-driven hardening pass; no behaviour changes for existing valid inputs.

- **Fix**: replace two `assert csv_filename is not None` statements in
  `server.py` with `ValueError` raises. The asserts would have leaked a raw
  `AssertionError` to the MCP tool surface if the table registry ever fell
  out of sync; the new `ValueError` carries an actionable hint pointing to
  the issue tracker.
- **Fix**: coalesce concurrent identical fetches in `RBAClient`. A burst of
  parallel `latest()` / `get_data()` calls for the same table now shares
  one in-flight HTTP request against the RBA CDN instead of N — followers
  await the owner's future and receive the same body (or the same error).
- **Tests**: +6 (3 thundering-herd / in-flight cleanup tests in
  `test_client.py`, 3 registry-inconsistency tests in `test_server_validation.py`).
  Total: 82 unit + 21 live = 103.

## 0.1.0 (2026-05-11)

Initial release. MCP server for RBA F-tables; companion to `abs-mcp`.

- 5 MCP tools: `search_tables`, `describe_table`, `get_data`, `latest`, `list_curated`
- 5 curated F-tables with plain-English series mappings: **F1.1** (money market — cash rate), **F4** (deposit rates), **F6** (housing lending rates), **F11** (FX monthly history), **F11.1** (FX daily current)
- 14 non-curated F-tables accessible via raw RBA series IDs
- Label-driven CSV header parser (resilient to RBA adding new metadata rows)
- SQLite-backed cache (6h data TTL, 15min latest TTL)
- Unit attribution per series (Per cent per annum / USD per AUD / Index / etc.)
- CC-BY 4.0 attribution surfaced in every response
- Input validation guards (URL-injection-safe)
- 76 unit tests + 21 live integration tests
