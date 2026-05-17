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
from rapidfuzz import fuzz

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
    """Two-pool ranker for F-table search.

    High-signal pool: id + name (token_set_ratio). Token-strict, so a
    query like 'cash rate history' doesn't fuzzy-match C1 (Credit Card
    Statistics) just because 'cash' is a substring of 'cash advances'
    in C1's description.

    Description pool: capped WRatio. Curated tables get a small
    CURATED_BONUS only when the high-signal pool also has a non-trivial
    match — gates the bonus so over-broad curated boosts don't flood
    unrelated queries with rel=99 ties.

    Phrase-match bonus when the full query appears as a substring of
    id+name (the focused haystack, not description).
    """
    if not query.strip():
        raise ValueError(
            "query is required. Try 'cash rate', 'mortgage', 'aud usd', "
            "'inflation', 'deposit rates', or any other RBA topic."
        )
    DESCRIPTION_CAP = 30
    CURATED_BONUS = 20
    PHRASE_BONUS = 15
    HIGH_SIGNAL_GATE = 40
    # Per-token coverage matters more than fuzzy similarity. A query
    # like 'cash rate history' loses to F11 if we go on token_set_ratio
    # alone because F11's 'exchange rate history' partially overlaps;
    # but F1.1 is the ONLY table whose description contains 'cash' as a
    # token. Reward coverage of distinct query tokens across the
    # full haystack (name + description + keywords).
    TOKEN_COVERAGE_WEIGHT = 35  # per query token, max contribution
    # Stopwords filtered from the query before token-coverage scoring —
    # generic terms that appear in nearly every dataset's haystack.
    STOPWORDS = frozenset({
        "australia", "australian", "data", "statistics", "stats",
        "the", "and", "of", "by", "in", "on", "for", "to", "a", "an",
        "with", "from", "annual", "monthly", "quarterly", "daily",
        "history", "table", "rba", "current",
    })

    q_lower = query.strip().lower()
    q_tokens = [t for t in q_lower.split() if t and t not in STOPWORDS]
    # Use the stopword-filtered query for the high-signal match too —
    # otherwise tokens like "history" or "monthly" (which appear in
    # nearly every table name) drag unrelated tables into high
    # token_set_ratio scores ('cash rate history' → F11 'Exchange
    # Rates – Monthly History' wins because of the literal 'history'
    # overlap, even though F1.1 is the right answer on 'cash rate').
    q_filtered = " ".join(q_tokens) if q_tokens else q_lower

    candidates: list[tuple[float, float, int]] = []  # (raw_adjusted, high, idx)
    for i, s in enumerate(summaries):
        name_str = f"{s.id} {s.name}".lower()
        desc_str = (s.description or "").lower()
        # rba's TableSummary.description folds in curated YAML keywords +
        # series descriptions, so include it in the high-signal token
        # match. Without this, F1.1 (name=Money Market — Monthly) doesn't
        # match "cash rate" queries — even though F1.1's keywords list
        # "cash rate" and "cash rate target".
        high_str = f"{name_str} {desc_str}"
        full_hay = high_str
        high = fuzz.token_set_ratio(q_filtered, high_str)
        desc_raw = fuzz.WRatio(q_lower, desc_str) if desc_str else 0
        desc = min(desc_raw, DESCRIPTION_CAP)
        # Token-coverage: count distinct non-stopword query tokens that
        # appear as substrings in the haystack. Each contributes up to
        # TOKEN_COVERAGE_WEIGHT / len(q_tokens) to the score, so a full
        # match across 3 tokens adds the full 35 points.
        covered = 0
        if q_tokens:
            covered = sum(1 for t in q_tokens if t in full_hay)
            coverage_score = (covered / len(q_tokens)) * TOKEN_COVERAGE_WEIGHT
        else:
            coverage_score = 0
        bonus = 0
        if high >= HIGH_SIGNAL_GATE:
            if s.is_curated:
                bonus += CURATED_BONUS
            # Phrase bonus fires on the STOPWORD-FILTERED query so multi-
            # token queries like "cash rate history" reward the table
            # whose haystack contains "cash rate" as a contiguous
            # substring (F1.1) over one that just has "cash" and "rate"
            # as separate tokens (F4: cash management trust + retail
            # deposit rates). Without this, token_set_ratio treats both
            # haystacks as full matches at 100.
            if q_filtered and " " in q_filtered and q_filtered in high_str:
                bonus += PHRASE_BONUS
        # DO NOT clamp here — clamping pre-sort collapses the score
        # distribution. F-table names almost all contain "rate" so high
        # alone sits 80-100, then desc*0.5 + coverage + bonus pushes
        # every table to 100, and we lose the relative ordering customers
        # need (F1.1 should clearly beat F4 for 'cash rate history').
        raw_adjusted = high + desc * 0.5 + coverage_score + bonus
        candidates.append((raw_adjusted, high, i))
    candidates.sort(key=lambda t: (-t[0], -t[1]))
    top_pool = candidates[:limit]
    out: list[TableSummary] = []
    if top_pool:
        leader_adj = top_pool[0][0]
        # Proportional scaling: leader maps to its raw score (capped at
        # 100), others scale relative to the leader. This preserves
        # ordering AND visible separation between the top hit and the
        # noise tail. Same pattern abs uses to fix its rel=100 ties.
        scale_ref = max(leader_adj, 100.0)
        for raw, _high, idx in top_pool:
            rel = round(max(0.0, (raw / scale_ref) * 100.0), 1)
            out.append(summaries[idx].model_copy(update={"relevance": rel}))
    return out


def search_tables(query: str, limit: int = 10) -> list[TableSummary]:
    return search_in_memory(list_tables(), query, limit)
