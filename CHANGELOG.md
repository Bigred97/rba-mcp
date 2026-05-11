# Changelog

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
