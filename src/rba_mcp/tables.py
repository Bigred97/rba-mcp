"""Registry of RBA F-tables + fuzzy search.

Replaces abs-mcp's `catalog.py`. The table list is hand-maintained in
`data/tables.yaml` because RBA has no machine-readable index endpoint.

Curated tables (those with a YAML in `data/curated/`) get a +25 score boost
in search ranking, the same pattern that worked for abs-mcp.
"""
from __future__ import annotations

from importlib import resources
from pathlib import Path

import yaml
from rapidfuzz import fuzz, process

from .models import TableSummary

_REGISTRY: list[dict] | None = None


def _data_dir() -> Path:
    """Locate data/ both during dev and after install."""
    try:
        ref = resources.files("rba_mcp").joinpath("data")
        if ref.is_dir():
            return Path(str(ref))
    except (ModuleNotFoundError, AttributeError):
        pass
    here = Path(__file__).resolve().parent / "data"
    if here.is_dir():
        return here
    raise FileNotFoundError("Could not locate rba_mcp/data/ directory")


def _load_registry() -> list[dict]:
    raw = yaml.safe_load((_data_dir() / "tables.yaml").read_text(encoding="utf-8"))
    return list(raw.get("tables") or [])


def reset_registry() -> None:
    global _REGISTRY
    _REGISTRY = None


def _registry() -> list[dict]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _load_registry()
    return _REGISTRY


def list_tables(curated_ids: set[str] | None = None) -> list[TableSummary]:
    """All 19 F-tables as TableSummary objects. `curated_ids` flags which are curated."""
    from . import curated as curated_mod
    if curated_ids is None:
        curated_ids = set(curated_mod.list_ids())
    out: list[TableSummary] = []
    for entry in _registry():
        cid = entry["id"]
        keywords = " ".join(entry.get("search_keywords") or [])
        # Fold curated description + curated series descriptions into the
        # description so the fuzzy haystack has rich keywords.
        cd = curated_mod.get(cid)
        extras_parts: list[str] = [keywords]
        if cd is not None:
            # Curated YAMLs have richer search_keywords than the registry; fold both in.
            extras_parts.append(" ".join(cd.search_keywords))
            extras_parts.append(cd.description or "")
            for series in cd.series.values():
                if series.description:
                    extras_parts.append(series.description)
        description = " ".join(filter(None, [entry.get("name", ""), *extras_parts]))
        out.append(
            TableSummary(
                id=cid,
                name=entry["name"],
                description=description,
                frequency=entry.get("frequency"),
                is_curated=cid in curated_ids,
            )
        )
    return out


def get_table(table_id: str) -> TableSummary | None:
    table_id = table_id.strip().upper()
    for s in list_tables():
        if s.id.upper() == table_id:
            return s
    return None


def get_csv_filename(table_id: str) -> str | None:
    table_id = table_id.strip().upper()
    for entry in _registry():
        if entry["id"].upper() == table_id:
            return entry["csv_filename"]
    return None


def search_in_memory(
    summaries: list[TableSummary], query: str, limit: int = 10
) -> list[TableSummary]:
    """Fuzzy search with curated +25 boost."""
    if not query.strip():
        raise ValueError(
            "query is required. Try 'cash rate', 'mortgage', 'aud usd', "
            "'inflation', 'deposit rates', or any other RBA topic."
        )
    haystack = {
        i: f"{s.id} {s.name} {s.description or ''}" for i, s in enumerate(summaries)
    }
    pool_size = max(limit * 4, 30)
    matches = process.extract(query, haystack, scorer=fuzz.WRatio, limit=pool_size)
    # Curated tables get a meaningful boost so they outrank substring-matching
    # non-curated (e.g. "exchange rate" matches F12 "US Dollar Exchange Rates"
    # at 85.5; F11.1 needs the +30 to clear that gap on common queries).
    CURATED_BONUS = 30
    # Phrase-match bonus: if the full query phrase appears as a substring in
    # the haystack entry, give a +20 boost. Lets strong non-curated matches
    # (e.g. "yield curve" → F2/F2.1) compete with the curated bonus when a
    # query is highly specific to a non-curated table. Curated tables that
    # also phrase-match get both bonuses, so common queries still route
    # correctly. (0.1.9 addition.)
    PHRASE_BONUS = 20
    q_lower = query.strip().lower()
    rescored = []
    for _hay, score, idx in matches:
        bonus = CURATED_BONUS if summaries[idx].is_curated else 0
        haystack_lower = haystack[idx].lower()
        if q_lower and q_lower in haystack_lower:
            bonus += PHRASE_BONUS
        rescored.append((score + bonus, score, idx))
    rescored.sort(key=lambda t: (-t[0], -t[1]))
    # Attach the adjusted score (with curated + phrase bonuses) to each
    # summary so direct-MCP callers can order their UI. Cap at 100 — the
    # bonuses can take a strong fuzzy match above 100, but the ausdata-api
    # gateway and most consumers expect a 0-100 scale.
    return [
        summaries[idx].model_copy(update={"relevance": round(min(float(adj), 100.0), 1)})
        for adj, _score, idx in rescored[:limit]
    ]


def search_tables(query: str, limit: int = 10) -> list[TableSummary]:
    return search_in_memory(list_tables(), query, limit)
