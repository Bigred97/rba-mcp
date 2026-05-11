# Changelog

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
