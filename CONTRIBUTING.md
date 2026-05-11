# Contributing to rba-mcp

Thanks for considering a contribution. This is an indie open-source project — every PR is read.

## Quick start

```bash
git clone https://github.com/Bigred97/rba-mcp.git
cd rba-mcp
uv sync --extra dev
uv pip install -e .

# Unit tests (no network)
uv run pytest

# Live integration tests (hits the RBA CDN)
uv run pytest -m live
```

## What kind of contribution helps?

| Most welcome | Be cautious |
|---|---|
| Bug fixes (with a regression test) | Adding new tools to the MCP surface |
| New curated F-tables (one YAML per table in `src/rba_mcp/data/curated/`) | Refactors that touch >3 modules |
| Better error messages with actionable hints | Changes that break the public response shape |
| Docs / README improvements | Pulling in new dependencies |
| Performance fixes (with a benchmark) | Changes to the YAML schema |

## Adding a curated F-table

1. Pick the F-table from the [RBA statistics index](https://www.rba.gov.au/statistics/tables/) — note the exact ID and the CSV filename
2. `curl -sS https://www.rba.gov.au/statistics/tables/csv/{filename}.csv | head -12` — confirm the metadata-block schema (Title / Description / Frequency / Type / Units / Source / Publication date / Series ID rows). The label-driven parser handles minor variations.
3. Identify the canonical RBA series IDs in row 8 of the CSV
4. Hand-write the YAML under `src/rba_mcp/data/curated/{ID}.yaml`. F11.yaml is the cleanest reference.
5. Add the table to `src/rba_mcp/data/tables.yaml` if it's not already there (it usually is — check the 19-table registry)
6. Add a parametrised entry in `tests/test_integration.py::test_curated_table_returns_plausible_value` with the expected unit and a plausibility range
7. Run `uv run pytest -m live` and confirm green

## PR checklist

- [ ] All tests pass (`uv run pytest -m "not live"` minimum; `uv run pytest -m live` if you touched the API surface or added curation)
- [ ] New code has tests
- [ ] No new dependencies (or they're justified in the PR body)
- [ ] CHANGELOG.md updated under the Unreleased section
- [ ] If you changed default behaviour, the README "Worked examples" still produces the documented values
- [ ] CC-BY 4.0 attribution still surfaces in `DataResponse.attribution`

## Style

- Python 3.11+, `from __future__ import annotations` at file top
- Pydantic v2 models — use `Field(default_factory=...)` for mutable defaults
- Docstrings in module-level summary; functions only when non-obvious
- No comments restating the code; comments explain *why*

## Filing bugs

Use the bug-report issue template. Bugs filed via the template get triaged within a week; freeform issues may sit longer.

## Discussions vs Issues

- **Issue**: bug, feature request, security report
- **Discussion**: question, idea you're not sure about, sharing how you're using the package

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Be kind.
