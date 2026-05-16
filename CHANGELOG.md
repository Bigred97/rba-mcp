# Changelog

## [0.7.5] - 2026-05-17

### Improved — F-table-not-found error message lists curated IDs

When `get_data(table_id='F999')` or `describe_table(table_id='F999')` is called with an unknown F-table ID, the ValueError now includes the full list of 15 curated table IDs and suggests "search by keyword or enumerate the curated set" — transport-agnostic, per the rba-specific convention enforced by `test_no_mcp_tool_refs_in_error_strings`. Previously the error described only the F/D/C/G/E + digits pattern, leaving customers to discover the curated set elsewhere.

Brings rba-mcp to portfolio parity with the 8 other sisters on "Deterministic Error Handling" (Try X / Did you mean / Valid options). Two new regressions in `test_server_validation.py` (`test_describe_table_unknown_table_lists_curated_ids`, `test_get_data_unknown_table_lists_curated_ids`) lock in the new shape. Tests 163/163.

## [0.7.4] - 2026-05-16

### Added — `test_resilience.py` perf-budget regressions

- New offline-only test module (`tests/test_resilience.py`) that locks in
  the hot-path characteristics of `latest()` / `get_data()` so a future
  parsing or shaping change can't silently regress the latency budget.
- Tests added:
  - `test_latest_warm_cache_under_2s` — `latest()` warm-cache call <2s
  - `test_latest_no_per_call_memory_balloon` — peak RSS grows by <30MB
    across 30 repeated `latest()` calls (catches DataFrame/record leaks)
  - `test_latest_explicit_series_does_not_pull_all_columns` — single-
    series queries return single-series records (no cross-leak)
  - `test_get_data_date_range_does_not_carry_out_of_range_rows` — date
    filter is applied before shaping, no leakage outside the window
  - `test_concurrent_latest_calls_complete` — 10 parallel warm-cache
    calls complete in <5s (in-flight coalescing intact)

### Notes — Item 1 from the sister-MCP playbook

- The playbook's hypothetical "5-10MB F-tables" doesn't match current
  RBA reality — measured live (2026-05-16), the largest curated tables
  are F11.1 (122KB), C1 (138KB), D2 (87KB), all comfortably below the
  threshold where filter-pushdown into the parse step would change
  user-observable latency. The existing pipeline already drops unused
  columns / out-of-range rows before record shaping, so the dominant
  cost is pandas' parse, not the filtering. Item 1 therefore lands as
  regression coverage rather than a parser rewrite — locking in the
  perf budget so a future change has to clear the bar.

### Tests

- 161 unit tests passing (was 156). 10× zero-flake gauntlet.

## [0.7.3] - 2026-05-16

### Fixed — `ValueError` hints are now transport-agnostic

- Stripped MCP-tool name references (`Try describe_table('F11')`,
  `Try search_tables()`, `Call list_curated()`) and internal RBA CDN URLs
  / CSV filenames from every user-facing error message in `server.py` and
  `curated.py`. Reasoning: when this server is fronted by a REST gateway
  or called from a generic script, an error suggesting a "describe_table"
  tool that doesn't exist in that transport is worse than no hint at all.
- The corrections still suggest *what* to look up (valid keys, retry,
  series-ID shape) without naming a specific transport's API surface.
- Internal RBA CSV-filename leakage in upstream-fetch errors removed —
  callers don't need to see `f11.1-data.csv` to recover from a 503; the
  table ID alone suffices.
- Added two AST-based regressions in `test_server_validation.py` that
  walk every `raise <Exc>(...)` in `src/rba_mcp/` and assert no message
  references `describe_table(`, `search_tables(`, `list_curated(`, an
  `*-data.csv` filename, or an `rba.gov.au/...` API URL. This prevents
  regressions in future error-message edits.

### Tests

- 156 unit tests passing (was 154). 10× zero-flake gauntlet.

## [0.7.2] - 2026-05-16

### Fixed — `latest()` / `get_data()` no-series default returns headline only

- **The "no filter = which series?" bug.** Previously, `latest("F1.1")` with
  no `series` argument returned every curated series in the table mixed
  together (11 series for F1.1: cash rate target, interbank, bank bills,
  Treasury notes, OIS — totalling ~11 records for a single date), which
  looked like duplicate / garbage data to LLM clients. Same issue affected
  every curated F-table (F11/F11.1: 8-9 FX rates; D1: 20 growth series;
  E2: 8 leverage ratios).
- **Fix.** Each curated YAML now declares a `headline_series:` field — the
  canonical "what does this table mean?" series. When the caller omits
  `series`, the server defaults to just that single series. Callers wanting
  the full set pass an explicit list of keys.
- Headline assignments: F1.1 → `cash_rate_target`, F2/F2.1 → `bond_10yr`,
  F4 → `term_deposit_12m`, F5 → `housing_variable_standard`, F6 →
  `owner_occupier_variable_existing`, F7 → `outstanding_small_business_total`,
  F8 → `outstanding_credit_card`, F11/F11.1 → `aud_usd`, D1 →
  `total_credit_excl_financial_yoy`, D2 → `total_credit_excl_financial_sa`,
  C1 → `value_of_purchases`, G3 → `consumer_expectations_1yr`, E2 →
  `household_debt_to_income`.
- Validated at load: a YAML whose `headline_series` references an undefined
  key fails with a clear `ValueError` rather than surfacing at query time.
- **No breaking change** for callers that already pass `series=` explicitly —
  the behaviour is identical. The change only affects callers that relied
  on the (broken) "give me everything" default.

### Tests

- 154 unit tests passing (was 145). 10× zero-flake gauntlet. Added
  regression coverage for the headline default and the `series=None`
  → headline resolution path.

## [0.7.1] - 2026-05-16

### Changed
- E2 (Household finances): removed hardcoded Q4 2025 snapshot values
  from description. The values would have gone stale when Q1 2026 data
  drops in ~June 2026. Callers should use `latest("E2")` to get current
  values rather than reading them from the dataset description.

## [0.7.0] - 2026-05-16

### Added — E2 Household Finances: Selected Ratios

- **`E2` curated table.** 8 quarterly series of Australian household
  leverage ratios — the canonical figures every macro analyst cites
  for household-debt-cycle narratives.
- Headline series: household debt-to-income (BHFDDIT), housing debt-to-
  income (BHFDDIH), owner-occupier debt-to-income SA (BHFDDIO),
  household debt-to-assets (BHFDA), household assets-to-income
  (BHFADIT), housing assets-to-income (BHFHDI), financial assets-to-
  income (BHFADIFA), housing debt-to-housing-assets (BHFHDHA).
- **Customer workflow extension** (macro persona — bank strategists,
  super fund chief economists, AFR journalists, Treasury): unifies the
  3 critical debt-to-income signals plus assets-to-income in one
  50-year time series. Previously these required cross-referencing ABS
  household balance sheets + APRA — now answered in one rba-mcp call.
- Joins naturally with: F6 (housing rates impact DTI), apra.ADI_PERFORMANCE
  (bank sector tracks household leverage), abs.HSI_M (consumer spending
  under rate pressure), abs.ANA_AGG (national accounts).

### Customer-value validation (live RBA fetch, 2026-05-16)

- `latest('E2', 'household_debt_to_income')` → 177.0% (Q4 2025).
  Peaked >190% pre-COVID, now deleveraging post-rate-hikes.
- `latest('E2', 'owner_occupier_debt_to_income')` → 99.6%.
- `latest('E2', 'household_assets_to_income')` → 1,102%.
- 6-year time series (2020+): DTI fell from 186.2% (Q1 2020) → 177.0%
  (Q4 2025) — the post-COVID rate-hike deleveraging story.
- Search routing: "household debt", "debt to income", "household
  leverage", "mortgage debt ratio" all hit E2 at #1.

### Tests

- 145 unit tests passing (was 141). 10× zero-flake.

## [0.6.0] - 2026-05-16

### Added — C1 credit-card statistics + G3 inflation expectations

- **`C1` — Credit and Charge Card Statistics (Monthly).** 13 curated
  series covering monthly card transactions: total number/value of
  transactions and purchases, domestic vs overseas splits, cash advances,
  repayments, total balances, balances accruing interest. The fastest
  way to track Australian consumer credit-card spend signal. Series
  back to mid-1980s. Realistic latest values verified: ~333M purchases
  per month, ~$41B value, ~$44B total outstanding balances (Mar 2026).
- **`G3` — Inflation Expectations (Quarterly).** All 7 series from RBA's
  inflation-expectations table: 1-year consumer expectations (Melbourne
  Institute), 3-month business expectations (NAB), 1-/2-year union
  officials, 1-/2-year market economists, plus the break-even 10-year
  inflation rate implied by AGS yields. Critical for bond traders
  pricing real rates and macro analysts tracking expectations anchoring.
- Both tables registered in `tables.yaml` (total catalogue now 23) and
  shipped with curated series mappings.

### Fixed — `latest()` skips trailing-null forecast rows

- `latest()` previously returned an all-null row when the file's most
  recent date had no value for the requested series. G3 surfaced this:
  RBA carries forward-dated empty rows (2026-Q3 / Q4 / 2027-Q1) for
  forecast-cadence series, and backward-looking series like consumer
  expectations don't populate those rows. The shaping path now drops
  all-null rows BEFORE taking `tail(last_n)`, so `latest()` always
  returns the most recent date with at least one non-null value for
  the requested series. F-tables that don't carry forward-dated empty
  rows are unaffected.

### Customer-value validation (live RBA fetch, 2026-05-16)

- Retail analyst: `latest('C1', 'value_of_purchases')` → $40,890m (Mar
  2026); `latest('C1', 'total_balances')` → $44,146m.
- Bond trader: `latest('G3', 'consumer_expectations_1yr')` → 5.2%
  (Mar 2026); `latest('G3', 'break_even_10yr')` → 2.4%.
- Search routing: "card transactions" → C1 at #1; "inflation
  expectations" / "break-even inflation" → G3 at #1.

### Tests

- 141 unit tests now (was 134). 10× zero-flake gauntlet. 30 live tests
  with new C1/G3 value-range assertions.

## [0.5.0] - 2026-05-16

### Added — D-series credit aggregates (Wave 1 portfolio expansion)

- **`D1` — Growth in Selected Financial Aggregates (Monthly).** 20 series
  covering monthly and 12-month-ended growth (%) for: total private-sector
  credit, housing credit (split owner-occupier / investor), non-financial
  business credit, other personal credit, M3 and broad money. This is the
  most-cited macro statistic after CPI for property and bank-credit commentary.
- **`D2` — Lending and Credit Aggregates (Monthly).** 20 series covering the
  $ billion outstanding stock of credit by category: total credit (excl.
  financial businesses), owner-occupier and investor housing, non-financial
  business credit, other personal, plus loans-and-advances by intermediary
  type. Where D1 reports growth rates, D2 reports the underlying levels.
- Both tables published from 1976 (D2) / 1977 (D1) to present, monthly cadence.
- Both registered in `tables.yaml` (total catalogue now 21) and added as
  curated YAMLs with series-mapping definitions.
- Series IDs map to the **current RBA headline definitions** post-2019
  redefinition: `business_credit_yoy` → DGFACBNF12 (non-financial business),
  `total_credit_excl_financial_sa` → DLCACFS. Legacy series IDs that RBA
  discontinued in 2019 (DGFACB12, DLCACS, DLCACBS, etc.) are not included.

### Tests

- 134 unit tests now (was 131); 10× zero-flake gauntlet.
- Live tests now cover D1 and D2 with realistic value-range assertions
  (housing credit YoY -5% to +30%, mortgage stock $500B-$5T, etc.).
- New `test_get_d1_loads_growth_series` and `test_get_d2_loads_level_series`
  curated-registry tests.
- Search-routing tests added for "credit growth", "credit aggregates",
  "housing credit growth", "business credit growth", and "total credit"
  hitting D1 / D2 at the top of search results.

### Customer-value validation (live RBA fetch, 2026-05-16)

- Property analyst: `latest('D1', 'housing_credit_yoy')` → 7.3% (Mar 2026).
- Bank credit analyst: `latest('D2', 'owner_occupier_housing_sa')` →
  $1,731B, `latest('D2', 'investor_housing_sa')` → $842B (Mar 2026).
- Macroeconomist: `latest('D1', 'business_credit_yoy')` → 9.9% (Mar 2026),
  `latest('D1', 'broad_money_yoy')` → 8.3%.
- Time-series query: `get_data('D1', 'business_credit_yoy', start_date='2024-01')`
  returns 27 monthly observations spanning Jan 2024 → Mar 2026.

## [0.4.1] - 2026-05-16

### Fixed

- `test_describe_non_curated_returns_raw_metadata` updated to use F3 (corporate
  bond yields, still non-curated) instead of F2 which is now curated.
- `test_list_curated_returns_five` / `test_call_list_curated_returns_five` in
  live and MCP-protocol test suites updated to expect all 10 curated tables.
- CLAUDE.md curated dataset list updated to reflect 10 tables.

## [0.4.0] - 2026-05-16

### Added

- **5 new curated F-tables**: F2 (government bond yields daily), F2.1 (government
  bond yields monthly), F5 (indicator lending rates), F7 (business lending rates),
  F8 (personal lending rates). Doubles curated coverage from 5 to 10 tables.
- F2/F2.1 expose 2yr, 3yr, 5yr, 10yr AGS yields and indexed bonds — the
  benchmark risk-free yield curve.
- F5 covers advertised housing (owner-occ + investor, variable + 3yr fixed),
  small/large business, and credit card indicator rates.
- F7 covers actual outstanding and new business loan rates by size (small /
  medium / large) and type (fixed / variable).
- F8 covers actual outstanding and new personal loan rates including credit
  cards, margin loans, and flexible term loans.

## [0.3.0] - 2026-05-15

### Added

- **`start_period` / `end_period` parameters** on `get_data` — additive,
  non-breaking aliases for the legacy `start_date` / `end_date`. Wave 4 of
  the portfolio interoperability pass: 7 of 9 sister MCPs already use
  `start_period` / `end_period` (abs, aemo, aihw, apra, asic, ato, wgea);
  rba-mcp now accepts the same name so cross-sister calling patterns
  match. Same format and semantics — `'YYYY'`, `'YYYY-MM'`, `'YYYY-MM-DD'`,
  or an int year. Supplying both `start_period` and `start_date` (or
  `end_period` and `end_date`) raises `ValueError` with a "Use either X or
  Y, not both" hint. The legacy names continue to work unchanged.

  ```python
  # New canonical names (preferred)
  await get_data("F11", series="aud_usd", start_period="2024")

  # Legacy alias (still works)
  await get_data("F11", series="aud_usd", start_date="2024")
  ```

- **+6 regression tests** in `test_server_validation.py` locking in the
  alias contract: canonical accepted, legacy still accepted, both-at-once
  raises, swap-error message unchanged.

- 127 unit tests now (was 120). 10× zero-flake green.
- No new dependencies. No exception-type changes. No envelope changes.

## [0.2.0] - 2026-05-15

### Added

- **DataResponse.source_url**: canonical click-through URL field, populated
  alongside the legacy `rba_url` alias. Cross-sister consumers can now read
  `.source_url` uniformly across the portfolio. `rba_url` remains populated
  with the same value for backward compatibility.
- **DataResponse.row_count**: number of observation rows in `records`
  (`int`, defaults to `0`). Brings rba-mcp in line with the canonical
  `DataResponse` envelope used by the rest of the portfolio.

## 0.1.11 (2026-05-15)

Error-message sweep — quality dimension #5 in CLAUDE.md. Rejection messages
now suggest the correction, not just describe the rejection.

Every weak `ValueError` raise site in `server.py` and `curated.py` was
rewritten to carry an actionable hint: "Try X", "Did you mean X?" (via
`difflib.get_close_matches`, stdlib — no new deps), "Valid options: ...",
and / or a `describe_table()` / `list_curated()` pointer. The CLAUDE.md
textbook example (invalid raw RBA series ID shape) now produces:

> `Series ID 'fx rusd' contains invalid characters. RBA series IDs are
> uppercase letters + digits, optionally with underscores or hyphens
> (e.g. 'FXRUSD', 'FIRMMCRT', 'FLRHOOVA'). Did you mean 'FXRUSD'? Try
> describe_table('F11.1') (FX) or describe_table('F1.1') (money market)
> to see valid series IDs for a table.`

- **server.py** — 8 raise sites rewritten: series-list type errors,
  `series` type fallback, `_validate_series_for_url` shape rejection (the
  prime example), `limit` lower-bound / type, the three `RBAAPIError` →
  `ValueError` wrappers in `describe_table` + `_get_data_impl`, and the
  `Unknown format` raise now adds a difflib "Did you mean?" alongside the
  existing "Valid options" list.
- **curated.py** — `Unknown series` raise now runs difflib against both
  curated keys and raw series IDs to suggest the likely correction;
  empty-list and empty-value raises now include a worked example key and
  `describe_table()` pointer.
- **+2 regression tests** in `test_server_validation.py` locking in the
  new actionable shape (Did-you-mean + describe_table pointer on curated
  typo, and shape-hint + describe_table pointer on invalid raw ID).
- 117 unit tests now (was 115). 10× zero-flake green.
- No new dependencies. No exception-type changes.

## 0.1.10 (2026-05-15)

Graceful degradation — quality dimension #4 in CLAUDE.md. Pattern ported
from abs-mcp 0.2.13.

When the upstream RBA CDN is unreachable (5xx, timeout, DNS failure,
connection refused), the client now falls back to the most-recent cached
payload regardless of TTL and surfaces the staleness in the response.
Agents see `DataResponse.stale=True` with a `stale_reason` like *"RBA CDN
returned 503; serving cached payload from ~17 minute(s) ago"* and can
continue reasoning, rather than the tool raising and breaking the chat.

Genuine no-cache-to-fall-back-to case still raises `RBAAPIError` — only
degrade gracefully when there's something to degrade to.

- **New: `Cache.get_stale(key) -> (payload, cached_at)`** — TTL-bypassing
  read, the building block for the fallback path.
- **New: `_stale_signal` ContextVar in `client.py`** — `reset_stale_signal()`
  + `get_stale_signal()` are the public API. The server resets at the
  start of each tool call and reads at the end to propagate `stale=True`
  into the response.
- **New: `DataResponse.stale: bool` and `DataResponse.stale_reason: str | None`** —
  echoed in every response when serving a stale cache.
- **New: `DataResponse.truncated_at: int | None`** — placeholder field
  matching the sister-MCP envelope (used by register-style MCPs like
  asic-mcp; remains `None` for time-series-shaped rba-mcp data).
- **+4 regression tests** in `test_client.py`:
  1. 503 + stale cache → fallback + stale flag set
  2. ConnectError + stale cache → same
  3. 503 + empty cache → raises `RBAAPIError` (unchanged behaviour)
  4. `Cache.get_stale()` round-trip + TTL bypass verification
- 115 unit tests now (was 111 in 0.1.9).

## 0.1.9 (2026-05-13)

Loop-audit value pass — three low-effort, high-value polish wins surfaced
by a focused review of the customer-facing surface.

- **New: `SeriesDetail.end_date`** — `describe_table` now reports the
  latest non-null observation per series alongside `start_date`. An
  LLM can answer "is this data fresh?" from a single `describe_table`
  call without a follow-up `latest()`. Populated via
  `df[col].last_valid_index()` in both curated and non-curated branches.
- **Search: F2 / F2.1 keywords expanded.** Added `yield curve`,
  `bond yields`, `australian government securities`, `ags`, and short
  bond tenors (`2 year bond`, `3 year bond`, `5 year bond`) to the
  `f2-data` and `f2.1-data` entries in `tables.yaml`. A user asking
  "what's the Australian yield curve?" now lands on F2 / F2.1
  immediately. Verified via two new search routing tests.
- **Search ranker: phrase-match bonus (+20).** Added alongside the
  existing curated +30 bonus. If the full query phrase appears as a
  substring in a table's haystack, that table gets +20. Lets strong
  non-curated matches (like "yield curve" → F2) compete with the
  curated boost when a query is highly specific to a non-curated
  table; curated tables that ALSO phrase-match still stack both
  bonuses, so common queries route correctly.
- **Tests**: +4 regressions — `end_date` populated for curated +
  non-curated, "yield curve" → F2 routing, "bond yields" → F2
  routing. 111 unit tests now (was 107).

## 0.1.8 (2026-05-12)

Customer-flow audit fixes — surfaced when running rba-mcp against Claude
Desktop end-to-end. Two real UX gaps + a major distribution-side warning.

- **Fix: `start_date` and `end_date` accept int years.** MCP / LLM clients
  often send a year as a JSON number (`start_date=2024`) instead of a
  string (`"2024"`). Pre-0.1.8 this errored at the Pydantic boundary
  with a verbose "Input should be a valid string" message. Now: the
  Annotated type is `str | int | None` and `_validate_period` coerces
  int → str transparently. Bool is explicitly excluded from coercion
  (since `isinstance(True, int)` is `True` in Python) so `True`/`False`
  still raise a clean type error rather than becoming "1"/"0".
- **Fix: `describe_table` populates `SeriesDetail.start_date` for
  curated tables.** The non-curated branch already fetched the CSV and
  ran `df[sid].first_valid_index()`; the curated branch shortcut to
  YAML metadata only and left `start_date` null on every curated
  series. The LLM needs this to pick a sensible date range without
  trial-and-error queries — e.g. F11 starts 2010, F11.1 starts 2023.
  Now both branches populate the field.
- **README + example configs: recommend `uvx --upgrade rba-mcp` for
  Claude Desktop / Cursor.** Plain `uvx rba-mcp` (no flag) uses
  whatever wheel is cached and the long-lived MCP child process holds
  it — new PyPI releases never reach a running install until the user
  manually `uvx --refresh`es and fully quits Claude Desktop. Found in
  the wild during a customer-flow audit: an install was running 0.1.2
  against PyPI 0.1.7, five releases of fixes never adopted. `--upgrade`
  makes uvx check PyPI on each launch. Same fix shipped to abs-mcp's
  example configs.
- **Tests**: +4 regressions in `tests/test_stress_regressions.py`
  (int year accepted; int = str equivalence; bool still rejected;
  curated `start_date` populated). 107 unit tests now (was 103).

## 0.1.7 (2026-05-12)

Glama Tool Definition Quality pass — every parameter on every tool now
carries an explicit `description` and `examples` in the MCP JSON schema,
and every docstring carries an `Examples:` + `When to use:` + `Returns:`
block. Targets the Glama sub-scores that were sitting at 2/5 on
Usage Guidelines across `list_curated`, `latest`, `describe_table`, and
`get_data`. Parity with abs-mcp 0.2.11.

- **Annotated parameter schemas.** All 5 tools (`search_tables`,
  `describe_table`, `get_data`, `latest`, `list_curated`) now use
  `Annotated[Type, Field(description=…, examples=[…])]` for every
  parameter. JSON schemas exposed to clients now include human-readable
  descriptions and 2–4 worked examples per parameter.
- **Richer docstrings.** Each tool gains worked code examples (real
  series keys, expected response shape), an explicit "When to use"
  section, and a Returns block. The 5 curated F-tables are listed by
  topic in `list_curated`'s docstring so an LLM can plan a multi-tool
  call without needing to invoke it first.
- No behavioural changes. All 103 unit tests still green.

## 0.1.6 (2026-05-11)

First release published via **PyPI Trusted Publishing** (no API token in CI).

- **Fix: `format` type validation order.** `get_data(..., format=42)` used
  to crash with `AttributeError: 'int' object has no attribute 'lower'`
  because `(fmt or "records").lower()` was called before the type-check
  guard. Now: type-check first, then coerce. Non-string `format` errors
  with a useful hint listing valid options. Covers int, float, list,
  dict, bool. Bug surfaced by a real-customer audit pass.
- **+2 regression tests** locking in the type guard (103 unit tests).

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
