# rba-mcp

[![tests](https://github.com/Bigred97/rba-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/Bigred97/rba-mcp/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/rba-mcp.svg)](https://pypi.org/project/rba-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/rba-mcp.svg)](https://pypi.org/project/rba-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Glama MCP server quality](https://glama.ai/mcp/servers/Bigred97/rba-mcp/badges/score.svg)](https://glama.ai/mcp/servers/Bigred97/rba-mcp)

**Ask Claude about Australian interest rates, exchange rates, and lending rates and get real, current numbers** — not "I don't have access to that data." This MCP server gives Claude (and other MCP clients like Cursor) live access to the [Reserve Bank of Australia's statistical tables](https://www.rba.gov.au/statistics/tables/), with curated mappings for the most-asked indicators.

![rba-mcp answering "Show me AUD against USD, EUR, GBP and the trade-weighted index since 2024" in Claude Desktop — four metric cards, rebased line chart, macro-context analysis](docs/demo.png)

Companion to [abs-mcp](https://github.com/Bigred97/abs-mcp) — together they cover the most-asked Australian economic data.

## What you can ask

Once installed, your LLM can answer questions like:

| Question | Real response |
|---|---|
| What's the current RBA cash rate? | RBA Cash Rate Target, latest month |
| What's AUD/USD today? | Latest daily exchange rate |
| Show me AUD vs major currencies in 2024 | Multi-series query with monthly observations |
| What's the average mortgage rate? | Owner-occupier variable, outstanding loans |
| What rate are 12-month term deposits at? | Latest from F4 |
| What's the trade-weighted index trend? | TWI series back to 1983 |

Every answer comes with the period, units (Per cent per annum, USD per AUD, etc.), and a link back to the RBA source.

## Install

```bash
# After publish:
uvx rba-mcp

# Local dev:
uv pip install -e .
```

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "rba": {
      "command": "uvx",
      "args": ["rba-mcp"]
    }
  }
}
```

If you also have `abs-mcp` installed, both servers run side-by-side. Claude disambiguates with the server prefix (`rba:get_data` vs `abs:get_data`).

For local dev (pre-PyPI):

```json
{
  "mcpServers": {
    "rba": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/rba-mcp", "rba-mcp"]
    }
  }
}
```

### Cursor

Add to `~/.cursor/mcp.json` (or workspace `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "rba": {
      "command": "uvx",
      "args": ["rba-mcp"]
    }
  }
}
```

## Tools

| Tool | What it does |
|---|---|
| `search_tables(query, limit=10)` | Fuzzy-search RBA F-tables by name or topic. |
| `describe_table(table_id)` | Plain-English series listing for one F-table. |
| `get_data(table_id, series, start_date, end_date, format)` | Query data. `series=None` returns all curated series; format = records / series / csv. |
| `latest(table_id, series)` | Most-recent observation for the requested series. |
| `list_curated()` | The 5 F-table IDs with hand-curated plain-English support. |

## Curated F-tables

For these five, `series` accepts plain-English keys (e.g. `"aud_usd"` instead of `"FXRUSD"`):

- **F1.1** — Money Market — Monthly: cash rate target, cash rate, bank bills, OIS rates, treasury notes
- **F4** — Retail Deposit & Investment Rates: transaction accounts, savings, term deposits, cash management trusts
- **F6** — Housing Lending Rates: owner-occupier vs investor, variable vs fixed, outstanding vs new loans
- **F11** — Exchange Rates — Monthly History (1983+): AUD/USD, AUD/EUR, AUD/GBP, AUD/JPY, AUD/CNY, AUD/NZD, TWI
- **F11.1** — Exchange Rates — Daily (2023+): same series, daily resolution

Any other F-table works too — pass raw RBA series IDs (e.g. `"FXRUSD"`) instead of curated keys.

## Worked examples

**"What's the current RBA cash rate?"**

```
latest(table_id="F1.1", series="cash_rate_target")
```

**"AUD to USD over the last year"**

```
get_data(table_id="F11.1", series="aud_usd", start_date="2024")
```

**"Compare AUD against USD, EUR and GBP since 2020"**

```
get_data(
  table_id="F11",
  series=["aud_usd", "aud_eur", "aud_gbp"],
  start_date="2020"
)
```

## Period formats

RBA series use ISO-style date formats. Pass `start_date` / `end_date` as:

| Format | Example | Use for |
|---|---|---|
| `YYYY` | `"2024"` | Year start |
| `YYYY-MM` | `"2024-03"` | Month start |
| `YYYY-MM-DD` | `"2024-03-15"` | Specific day (daily tables only) |

## Development

```bash
git clone https://github.com/Bigred97/rba-mcp.git
cd rba-mcp
uv sync --extra dev
uv pip install -e .

# Unit tests (no network)
uv run pytest

# Live integration tests (hits RBA CDN)
uv run pytest -m live
```

The SQLite cache lives at `~/.rba-mcp/cache.db`. Data refreshes every 6h, latest 15min. Delete to force a refresh.

## How it works

Claude picks the right tool, fills in the curated series keys, calls the live RBA CDN, and synthesises the answer. When the curated table is stale relative to a rate decision (RBA's monthly F1.1 publishes around the 5th business day, but Board meetings can hike between publications), Claude fluidly composes web-search results with this server's data:

![Claude querying rba-mcp for cash rate, noticing the F1.1 monthly is at 4.10%, and web-searching to learn the Board hiked 25bp at the 5 May meeting → reports 4.35% effective 6 May 2026 with citations](docs/cash-rate.png)

You don't have to know what `FIRMMCRT` or `FXRUSD` mean — and neither does Claude. The server's curated YAMLs map plain-English keys (`cash_rate_target`, `aud_usd`) to RBA series IDs and surface unit attribution + the CC-BY 4.0 attribution string in every response.

## Companion server

[abs-mcp](https://github.com/Bigred97/abs-mcp) covers the Australian Bureau of Statistics side — labour force, CPI, GDP, wages, housing approvals, lending, population. Install both for the AU macro stack; they run side-by-side in any MCP client and Claude disambiguates via the server prefix (`rba:latest` vs `abs:latest`). See [examples/claude_desktop_config_both.json](examples/claude_desktop_config_both.json).

## Data attribution

RBA data is licensed under [Creative Commons Attribution 4.0 International (CC BY 4.0)](https://www.rba.gov.au/copyright/). Every `DataResponse` from this server includes an `attribution` field with the required notice. If you redistribute responses, credit the RBA.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release history.

## License

MIT — Harry Vass, 2026.
