"""Scrape + parse the RBA schedule-of-releases HTML.

Source: https://www.rba.gov.au/schedules-events/
Cached via the standard `Cache` with kind="calendar" (24h TTL — see cache.py).

The RBA page renders two tables:

- **Table 1 ("This Week")** — week-commencing caption + rows with day-name +
  time. Most rows are "Weekdays" recurring (daily file refreshes); we skip
  those and emit only specific-day rows ("Thursday, 21 May") within the
  caption's week.

- **Table 2 ("Releases Expected")** — month-grid covering ~4 months ahead.
  Each row is one publication; columns are months; cells hold either
  "Released" (past), "&ndash;" (not this month), or a day-of-month integer.

We parse both, dedupe by (source_url, release_at), and return entries
sorted ascending by release_at.

Shape parity with `abs_mcp.release_calendar` — same `ReleaseEntry` field
names so the gateway's webhook poller can dispatch uniformly.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone

import httpx

from .cache import TTL, Cache
from .client import RBAAPIError
from .models import ReleaseEntry

CALENDAR_URL = "https://www.rba.gov.au/schedules-events/"


def _sydney_offset_for(dt_utc: datetime) -> timedelta:
    """AEST/AEDT switch — first-Sunday approximation. See abs-mcp for the
    full rationale; same code path on both sisters."""
    m, d = dt_utc.month, dt_utc.day
    if 5 <= m <= 9:
        return timedelta(hours=10)
    if m in (11, 12, 1, 2, 3):
        return timedelta(hours=11)
    if m == 4:
        return timedelta(hours=11) if d <= 7 else timedelta(hours=10)
    if m == 10:
        return timedelta(hours=10) if d <= 7 else timedelta(hours=11)
    return timedelta(hours=10)


# Publication title → (publication_id, dataset_id, event_type) tuple.
# `dataset_id` populates only when the release refreshes one of rba-mcp's
# 15 curated F-tables. `event_type` is "data_release" for regular
# statistical publications, "statement" for narrative releases (SoMP,
# Minutes, Bulletin, FSR), "policy_decision" for cash-rate decisions
# (which don't appear in this table directly — they're a different page).
_TITLE_MAP: list[tuple[re.Pattern[str], str, str | None, str]] = [
    # Curated F-table refreshes (where the release name maps to one of the 15)
    (re.compile(r"Financial Aggregates", re.IGNORECASE), "D1", "D1", "data_release"),
    (re.compile(r"Retail Payments", re.IGNORECASE), "C1", "C1", "data_release"),
    (re.compile(r"Inflation Expectations", re.IGNORECASE), "G3", "G3", "data_release"),
    # Narrative / event releases — no dataset_id but informative event_type
    (re.compile(r"Statement on Monetary Policy", re.IGNORECASE), "SMP", None, "statement"),
    (re.compile(r"Minutes of (the )?Monetary Policy Meeting", re.IGNORECASE), "MINUTES", None, "statement"),
    (re.compile(r"Financial Stability Review", re.IGNORECASE), "FSR", None, "statement"),
    (re.compile(r"Reserve Bank of Australia Bulletin", re.IGNORECASE), "BULLETIN", None, "statement"),
    (re.compile(r"Chart Pack", re.IGNORECASE), "CHART_PACK", None, "statement"),
    # Other RBA statistical publications — publication_id only
    (re.compile(r"Index of Commodity Prices", re.IGNORECASE), "I2", None, "data_release"),
    (re.compile(r"Official Reserve Assets|International Reserves", re.IGNORECASE), "A4", None, "data_release"),
    (re.compile(r"RBA Balance Sheet|Reserve Bank of Australia Balance Sheet", re.IGNORECASE), "A1", None, "data_release"),
    (re.compile(r"Foreign Exchange Transactions", re.IGNORECASE), "A4", None, "data_release"),
    (re.compile(r"Open Market Operations", re.IGNORECASE), "A3", None, "data_release"),
    (re.compile(r"Exchange Rates - Daily|Exchange Rates$", re.IGNORECASE), "F11.1", "F11.1", "data_release"),
    (re.compile(r"Weights for the TWI", re.IGNORECASE), "TWI_WEIGHTS", None, "data_release"),
]


def _classify(title: str) -> tuple[str | None, str | None, str]:
    """Return (publication_id, dataset_id, event_type) for a release title."""
    for pattern, pub_id, dataset_id, event_type in _TITLE_MAP:
        if pattern.search(title):
            return pub_id, dataset_id, event_type
    return None, None, "data_release"


_TIME_RE = re.compile(
    r"(?:After\s+)?(\d{1,2})[.:](\d{2})\s*(am|pm)", re.IGNORECASE
)
_DEFAULT_RELEASE_TIME = time(11, 30)  # most common RBA cadence


def _parse_time(s: str) -> time:
    """Coerce '11.30 am' / '4.30 pm' / 'After 4.30 pm' → time().

    Falls back to 11:30 (the common RBA release slot) when the cell is
    unparseable (e.g. 'Variable')."""
    if not s:
        return _DEFAULT_RELEASE_TIME
    cleaned = s.replace("&nbsp;", " ").replace("\xa0", " ").strip()
    m = _TIME_RE.search(cleaned)
    if not m:
        return _DEFAULT_RELEASE_TIME
    hour = int(m.group(1))
    minute = int(m.group(2))
    ampm = m.group(3).lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour < 24) or not (0 <= minute < 60):
        return _DEFAULT_RELEASE_TIME
    return time(hour, minute)


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_NBSP_RE = re.compile(r"&nbsp;|\xa0")
_WHITESPACE_RE = re.compile(r"\s+")
_HTML_ENTITY_FIXES = (
    ("&amp;", "&"),
    ("&ndash;", "–"),
    ("&mdash;", "—"),
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&quot;", '"'),
)


def _strip_html(s: str) -> str:
    """Remove tags + comments + entities + collapse whitespace."""
    s = _HTML_COMMENT_RE.sub("", s)
    s = _HTML_TAG_RE.sub(" ", s)
    s = _NBSP_RE.sub(" ", s)
    for ent, repl in _HTML_ENTITY_FIXES:
        s = s.replace(ent, repl)
    return _WHITESPACE_RE.sub(" ", s).strip()


_HREF_RE = re.compile(r'<a[^>]*href="([^"#?]+)"', re.IGNORECASE)


def _extract_href(cell_html: str) -> str | None:
    """First absolute-or-relative URL in the cell, normalised to rba.gov.au."""
    m = _HREF_RE.search(cell_html)
    if not m:
        return None
    href = m.group(1)
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return "https://www.rba.gov.au" + href
    return None


_MONTH_HEADER_RE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*"
    r"\s*(?:<br\s*/?>)?\s*(\d{4})",
    re.IGNORECASE,
)
_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_month_headers(thead_html: str) -> list[tuple[int, int]]:
    """Return [(year, month), ...] in column order from the thead."""
    out: list[tuple[int, int]] = []
    for m in _MONTH_HEADER_RE.finditer(thead_html):
        mon = _MONTH_NAMES.get(m.group(1).lower())
        if mon:
            out.append((int(m.group(2)), mon))
    return out


_DAY_CELL_RE = re.compile(r"^\d{1,2}$")


def _parse_month_grid(table_html: str) -> list[ReleaseEntry]:
    """Parse Table 2 — the month-grid 'Releases Expected' section."""
    thead_m = re.search(r"<thead>(.+?)</thead>", table_html, re.DOTALL)
    tbody_m = re.search(r"<tbody>(.+?)</tbody>", table_html, re.DOTALL)
    if not thead_m or not tbody_m:
        return []
    month_cols = _parse_month_headers(thead_m.group(1))
    if not month_cols:
        return []

    out: list[ReleaseEntry] = []
    row_blocks = re.findall(r"<tr>(.+?)</tr>", tbody_m.group(1), re.DOTALL)
    for row_html in row_blocks:
        # Drop comments first so commented-out cells don't get captured.
        row_clean = _HTML_COMMENT_RE.sub("", row_html)
        # Extract <th> (title) and <td>s (frequency, time, then month columns)
        th_m = re.search(r"<th[^>]*>(.+?)</th>", row_clean, re.DOTALL)
        if not th_m:
            continue
        th_html = th_m.group(1)
        title = _strip_html(th_html)
        if not title:
            continue
        href = _extract_href(th_html)
        if not href:
            continue
        publication_id, dataset_id, event_type = _classify(title)

        td_blocks = re.findall(r"<td[^>]*>(.+?)</td>", row_clean, re.DOTALL)
        if len(td_blocks) < 2 + len(month_cols):
            continue  # malformed row
        # First td = frequency, second = time, then one td per month
        time_str = _strip_html(td_blocks[1])
        release_time = _parse_time(time_str)
        month_cells = td_blocks[2:2 + len(month_cols)]

        for (year, month), cell_html in zip(month_cols, month_cells):
            cell_text = _strip_html(cell_html)
            if not cell_text or cell_text.lower() == "released" or cell_text in {"–", "—", "-"}:
                continue
            if not _DAY_CELL_RE.match(cell_text):
                continue
            day = int(cell_text)
            try:
                naive = datetime(year, month, day, release_time.hour, release_time.minute)
            except ValueError:
                continue
            offset = _sydney_offset_for(naive.replace(tzinfo=timezone.utc))
            release_at = naive.replace(tzinfo=timezone(offset))
            out.append(
                ReleaseEntry(
                    release_at=release_at,
                    title=title,
                    event_type=event_type,
                    dataset_id=dataset_id,
                    publication_id=publication_id,
                    source_url=href,
                    reference_period=None,
                )
            )
    return out


_WEEK_CAPTION_RE = re.compile(
    r"Week commencing\s+(\d{1,2})\s+(\w+)\s+(\d{4})", re.IGNORECASE
)
_DAY_NAME_RE = re.compile(
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,\s*"
    r"(\d{1,2})\s+(\w+)",
    re.IGNORECASE,
)


def _parse_this_week(table_html: str) -> list[ReleaseEntry]:
    """Parse Table 1 — the 'This Week' day-resolved schedule.

    Skips 'Weekdays' rows (recurring daily file refreshes — too noisy for
    a notification feed). Emits one entry per specific-day row.
    """
    caption_m = re.search(r"<caption[^>]*>(.+?)</caption>", table_html, re.DOTALL)
    if not caption_m:
        return []
    cap_text = _strip_html(caption_m.group(1))
    week_m = _WEEK_CAPTION_RE.search(cap_text)
    if not week_m:
        return []
    week_day = int(week_m.group(1))
    week_mon_name = week_m.group(2)[:3].lower()
    week_mon = _MONTH_NAMES.get(week_mon_name)
    week_year = int(week_m.group(3))
    if not week_mon:
        return []
    try:
        week_start = datetime(week_year, week_mon, week_day)
    except ValueError:
        return []

    tbody_m = re.search(r"<tbody>(.+?)</tbody>", table_html, re.DOTALL)
    if not tbody_m:
        return []

    out: list[ReleaseEntry] = []
    for row_html in re.findall(r"<tr>(.+?)</tr>", tbody_m.group(1), re.DOTALL):
        row_clean = _HTML_COMMENT_RE.sub("", row_html)
        th_m = re.search(r"<th[^>]*>(.+?)</th>", row_clean, re.DOTALL)
        if not th_m:
            continue
        th_html = th_m.group(1)
        title = _strip_html(th_html)
        if not title:
            continue
        href = _extract_href(th_html)
        if not href:
            continue
        publication_id, dataset_id, event_type = _classify(title)

        td_blocks = re.findall(r"<td[^>]*>(.+?)</td>", row_clean, re.DOTALL)
        if len(td_blocks) < 2:
            continue
        date_cell = _strip_html(td_blocks[0])
        if date_cell.lower().startswith("weekday"):
            # Recurring daily file — skip to avoid burying real events.
            continue
        day_m = _DAY_NAME_RE.search(date_cell)
        if not day_m:
            continue
        day = int(day_m.group(2))
        mon_name = day_m.group(3)[:3].lower()
        mon = _MONTH_NAMES.get(mon_name)
        if not mon:
            continue
        # Calendar tables sometimes span a month-end; pick the year that
        # keeps the date within ~14 days of the week-commencing date.
        for candidate_year in (week_year, week_year + 1, week_year - 1):
            try:
                candidate = datetime(candidate_year, mon, day)
            except ValueError:
                continue
            if abs((candidate - week_start).days) <= 14:
                break
        else:
            continue

        time_str = _strip_html(td_blocks[1])
        release_time = _parse_time(time_str)
        naive = candidate.replace(hour=release_time.hour, minute=release_time.minute)
        offset = _sydney_offset_for(naive.replace(tzinfo=timezone.utc))
        release_at = naive.replace(tzinfo=timezone(offset))
        out.append(
            ReleaseEntry(
                release_at=release_at,
                title=title,
                event_type=event_type,
                dataset_id=dataset_id,
                publication_id=publication_id,
                source_url=href,
                reference_period=None,
            )
        )
    return out


def parse_entries(html: str) -> list[ReleaseEntry]:
    """Parse both schedule tables and dedupe.

    Dedup key is (source_url, release_at). When the same publication
    appears in both 'This Week' and the month-grid the dated entry wins.
    """
    tables = re.findall(r"<table[^>]*>(.+?)</table>", html, re.DOTALL)
    out: list[ReleaseEntry] = []
    for tbl in tables:
        caption_m = re.search(r"<caption[^>]*>(.+?)</caption>", tbl, re.DOTALL)
        if not caption_m:
            continue
        caption = _strip_html(caption_m.group(1)).lower()
        if "week commencing" in caption:
            out.extend(_parse_this_week(tbl))
        elif "releases expected" in caption:
            out.extend(_parse_month_grid(tbl))

    seen: set[tuple[str, str]] = set()
    deduped: list[ReleaseEntry] = []
    for e in out:
        key = (e.source_url, e.release_at.isoformat())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    return deduped


async def _fetch_html(http_client: httpx.AsyncClient, cache: Cache) -> tuple[bytes, bool, str | None]:
    """Return (html, stale, reason). 5xx falls back to last-good cached."""
    cached = await cache.get(CALENDAR_URL, ttl=TTL["calendar"])
    if cached is not None:
        return cached, False, None
    try:
        resp = await http_client.get(CALENDAR_URL, headers={"Accept": "text/html"})
        resp.raise_for_status()
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        fallback = await cache.get_stale(CALENDAR_URL)
        if fallback is not None:
            payload, _ = fallback
            if isinstance(e, httpx.HTTPStatusError):
                reason = (
                    f"RBA schedule returned {e.response.status_code}; "
                    "serving last-good cached payload."
                )
            else:
                reason = (
                    f"RBA schedule unreachable ({type(e).__name__}); "
                    "serving last-good cached payload."
                )
            return payload, True, reason
        if isinstance(e, httpx.HTTPStatusError):
            raise RBAAPIError(
                f"RBA schedule returned {e.response.status_code}"
            ) from e
        raise RBAAPIError(
            f"RBA schedule request failed ({type(e).__name__})"
        ) from e
    await cache.set(CALENDAR_URL, resp.content, kind="calendar")
    return resp.content, False, None


async def fetch_release_calendar(
    http_client: httpx.AsyncClient,
    cache: Cache,
    days_ahead: int,
) -> tuple[list[ReleaseEntry], bool, str | None]:
    """Return (entries_within_horizon, stale, stale_reason)."""
    html_bytes, stale, stale_reason = await _fetch_html(http_client, cache)
    try:
        html = html_bytes.decode("utf-8", errors="replace")
    except Exception:
        html = ""
    entries = parse_entries(html)
    now_utc = datetime.now(timezone.utc)
    horizon = now_utc + timedelta(days=days_ahead)
    in_window = [
        e for e in entries
        if e.release_at >= now_utc and e.release_at <= horizon
    ]
    in_window.sort(key=lambda e: e.release_at)
    return in_window, stale, stale_reason
