"""Hand-curated metadata for the top-N RBA F-tables.

Each YAML in `data/curated/` defines a table's plain-English series → RBA
series-ID mapping. Curated tables get hidden defaults? — no, RBA F-tables
are flat (one table = independent series), so there's no defaulting.

Series IDs (FXRUSD, FIRMMCRT, etc.) are the canonical machine keys from row
8 of each F-table CSV, verified against the live download.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml


@dataclass(frozen=True)
class CuratedSeries:
    series_id: str
    description: str | None = None
    unit: str | None = None    # optional override of CSV's unit string


@dataclass(frozen=True)
class CuratedTable:
    id: str
    name: str
    description: str
    source_url: str | None
    update_frequency: str | None
    csv_filename: str
    search_keywords: tuple[str, ...] = ()
    series: dict[str, CuratedSeries] = field(default_factory=dict)


_REGISTRY: dict[str, CuratedTable] | None = None


def _yaml_dir() -> Path:
    try:
        ref = resources.files("rba_mcp").joinpath("data/curated")
        if ref.is_dir():
            return Path(str(ref))
    except (ModuleNotFoundError, AttributeError):
        pass
    here = Path(__file__).resolve().parent / "data" / "curated"
    if here.is_dir():
        return here
    raise FileNotFoundError("Could not locate rba_mcp/data/curated/")


def _parse_series(raw: dict) -> CuratedSeries:
    return CuratedSeries(
        series_id=str(raw["series_id"]),
        description=raw.get("description"),
        unit=raw.get("unit"),
    )


def _load_one(path: Path) -> CuratedTable:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    series = {
        key: _parse_series(s_raw)
        for key, s_raw in (raw.get("series") or {}).items()
    }
    return CuratedTable(
        id=str(raw["id"]),
        name=str(raw["name"]),
        description=str(raw.get("description", "")),
        source_url=raw.get("source_url"),
        update_frequency=raw.get("update_frequency"),
        csv_filename=str(raw["csv_filename"]),
        search_keywords=tuple(raw.get("search_keywords") or ()),
        series=series,
    )


def _load_all() -> dict[str, CuratedTable]:
    out: dict[str, CuratedTable] = {}
    for path in sorted(_yaml_dir().glob("*.yaml")):
        ct = _load_one(path)
        out[ct.id] = ct
    return out


def get(table_id: str) -> CuratedTable | None:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _load_all()
    return _REGISTRY.get(table_id.upper())


def list_ids() -> list[str]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _load_all()
    return sorted(_REGISTRY.keys())


def reset_registry() -> None:
    global _REGISTRY
    _REGISTRY = None


def translate_series(
    curated: CuratedTable, requested: str | list[str] | None
) -> list[str]:
    """Translate plain-English keys → RBA series IDs.

    - None / empty → all curated series IDs (the default behaviour)
    - "key" → [series_id]
    - ["key1", "key2"] → [series_id1, series_id2]
    - Raw RBA series IDs pass through (escape hatch for power users).
    Empty list / empty string raises ValueError with a useful hint.
    """
    if requested is None:
        return [s.series_id for s in curated.series.values()]
    items: list[str]
    if isinstance(requested, list):
        if not requested:
            raise ValueError(
                f"series filter is an empty list. "
                f"Pass at least one series, or omit `series` to query all "
                f"curated series for {curated.id}."
            )
        items = requested
    else:
        items = [requested]

    out: list[str] = []
    valid_keys = sorted(curated.series.keys())
    known_ids = {s.series_id for s in curated.series.values()}
    for v in items:
        v_str = str(v).strip()
        if not v_str:
            raise ValueError(
                f"Empty series value. Try one of: {', '.join(valid_keys[:15])}"
                + ("..." if len(valid_keys) > 15 else "")
            )
        if v_str in curated.series:
            out.append(curated.series[v_str].series_id)
        elif v_str in known_ids:
            out.append(v_str)               # raw ID escape hatch
        else:
            raise ValueError(
                f"Unknown series '{v}' for table '{curated.id}'. "
                f"Try one of: {', '.join(valid_keys[:15])}"
                + ("..." if len(valid_keys) > 15 else "")
                + ". Or pass a raw RBA series ID directly."
            )
    return out
