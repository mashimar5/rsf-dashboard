"""Today's RSF opening hours, scraped from the RecWell hours page.

The page carries up to three kinds of table and the right one depends on the
date:

  1. per-date rows ("RSF - 8/23"), used for one-off closures and event hours
  2. a table whose header names a date range ("Summer 2026 (5/16 - 8/22)")
  3. an undated table, which is the standing schedule

They are consulted in that order: a specific date beats a seasonal range,
which beats the default.
"""

import html as html_module
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

HOURS_URL = "https://recwell.berkeley.edu/facilities/recreational-sports-facility-rsf/rsf-hours/"
CACHE_PATH = Path(os.environ.get("RSF_HOURS_CACHE", Path(__file__).parent / "hours_cache.json"))
MAX_AGE = timedelta(hours=12)
TIMEOUT = 15

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
DASH = r"[–—-]"
TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(a\.m\.|p\.m\.|am|pm)", re.I)
MD_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})\b")


@dataclass
class DayHours:
    text: str                  # as written on the page, e.g. "8 a.m.-11 p.m."
    opens: int | None          # minutes since midnight, None when closed
    closes: int | None
    source: str                # which table it came from, for debugging


def _strip(markup: str) -> str:
    """Tags out, entities decoded. The page writes its dashes as &#8211;, so
    skipping the unescape silently breaks every date-range match."""
    text = re.sub(r"(?s)<[^>]+>", " ", markup)
    return re.sub(r"\s+", " ", html_module.unescape(text)).strip()


def parse_tables(markup: str):
    """[(header_cells, [row_cells, ...]), ...]"""
    tables = []
    for table in re.findall(r"(?is)<table.*?</table>", markup):
        rows = []
        for row in re.findall(r"(?is)<tr.*?</tr>", table):
            cells = [_strip(c) for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row)]
            cells = [c for c in cells if c]
            if cells:
                rows.append(cells)
        if rows:
            tables.append((rows[0], rows[1:]))
    return tables


def minutes_of(text: str):
    """"7 a.m.-8 p.m." -> (420, 1200). None when no pair of times is present."""
    found = TIME_RE.findall(text)
    if len(found) < 2:
        return None
    def to_minutes(hour, minute, meridiem):
        hour = int(hour) % 12
        if meridiem.lower().startswith("p"):
            hour += 12
        return hour * 60 + int(minute or 0)
    return to_minutes(*found[0]), to_minutes(*found[1])


def weekdays_in(spec: str) -> set[int]:
    """"Monday-Friday" -> {0,1,2,3,4}; "Saturday" -> {5}"""
    spec = spec.lower()
    span = re.search(rf"({'|'.join(WEEKDAYS)})\s*{DASH}\s*({'|'.join(WEEKDAYS)})", spec)
    if span:
        start, end = WEEKDAYS[span.group(1)], WEEKDAYS[span.group(2)]
        if start <= end:
            return set(range(start, end + 1))
        return set(range(start, 7)) | set(range(0, end + 1))
    return {WEEKDAYS[name] for name in WEEKDAYS if name in spec}


def _date_range(text: str, year: int):
    """"Summer 2026 (5/16 - 8/22)" -> (date(2026,5,16), date(2026,8,22))"""
    pair = re.search(rf"(\d{{1,2}})/(\d{{1,2}})\s*{DASH}\s*(\d{{1,2}})/(\d{{1,2}})", text)
    if not pair:
        return None
    m1, d1, m2, d2 = (int(g) for g in pair.groups())
    try:
        return date(year, m1, d1), date(year, m2, d2)
    except ValueError:
        return None


def _as_day_hours(cells, source):
    text = cells[-1]
    times = minutes_of(text)
    if times is None:
        # "CLOSED", "CLOSED for Caltopia", or anything else without two times
        return DayHours(text=text, opens=None, closes=None, source=source)
    return DayHours(text=text, opens=times[0], closes=times[1], source=source)


def hours_for(day: date, markup: str) -> DayHours | None:
    """Today's hours, or None when the page says nothing about this day"""
    tables = parse_tables(markup)

    # 1. an explicit date wins over everything
    for header, rows in tables:
        for cells in rows:
            for month, dom in MD_RE.findall(" ".join(cells[:-1])):
                if (int(month), int(dom)) == (day.month, day.day):
                    return _as_day_hours(cells, source=" ".join(header))

    # 2. a table scoped to a date range that contains today
    for header, rows in tables:
        window = _date_range(" ".join(header), day.year)
        if not window or not (window[0] <= day <= window[1]):
            continue
        for cells in rows:
            if day.weekday() in weekdays_in(" ".join(cells[:-1])):
                return _as_day_hours(cells, source=" ".join(header))

    # 3. the standing schedule: the first table with no date range at all
    for header, rows in tables:
        if _date_range(" ".join(header), day.year) or MD_RE.search(" ".join(header)):
            continue
        for cells in rows:
            if day.weekday() in weekdays_in(" ".join(cells[:-1])):
                return _as_day_hours(cells, source=" ".join(header))
    return None


def _fetch_markup() -> str:
    response = requests.get(
        HOURS_URL,
        timeout=TIMEOUT,
        headers={"User-Agent": "rsf-dashboard (personal project)"},
    )
    response.raise_for_status()
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", response.text)
    return "\n".join(re.findall(r"(?is)<table.*?</table>", body))


def cached_markup(now=None) -> str | None:
    """The hours tables, refetched at most twice a day.

    Falls back to a stale cache if the site is unreachable: yesterday's tables
    are far better than nothing, and the schedule rarely changes.
    """
    now = now or datetime.now(timezone.utc)
    cached = None
    if CACHE_PATH.exists():
        try:
            cached = json.loads(CACHE_PATH.read_text())
            fetched_at = datetime.fromisoformat(cached["fetched_at"])
            if now - fetched_at < MAX_AGE:
                return cached["markup"]
        except (ValueError, KeyError, OSError):
            cached = None

    try:
        markup = _fetch_markup()
    except Exception:
        return cached["markup"] if cached else None

    try:
        CACHE_PATH.write_text(json.dumps({"fetched_at": now.isoformat(), "markup": markup}))
    except OSError:
        pass
    return markup


def todays_hours(day: date) -> DayHours | None:
    markup = cached_markup()
    if not markup:
        return None
    try:
        return hours_for(day, markup)
    except Exception:
        return None
