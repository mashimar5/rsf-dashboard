import base64
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean, median
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request, send_from_directory

import hours
import store
from density import fetch_reading, percentage

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
CHART_WIDTH = 720
CHART_HEIGHT = 180
BUCKET_MINUTES = 30
# occupancy thresholds -> colour, shared by the bar and the favicon
LEVELS = ((0.50, "#16a34a"), (0.85, "#ca8a04"), (1.01, "#dc2626"))
NEUTRAL = "#9ca3af"

FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="7" fill="{color}"/>'
    '<g fill="#fff">'
    '<rect x="5" y="11" width="4.5" height="10" rx="1.6"/>'
    '<rect x="22.5" y="11" width="4.5" height="10" rx="1.6"/>'
    '<rect x="9" y="14.4" width="14" height="3.2" rx="1.6"/>'
    "</g></svg>"
)
# Below this many past instances of a weekday, the median is too noisy to show
MIN_WEEKDAY_INSTANCES = 3

app = Flask(__name__)


def current_reading(connection):
    """Live reading; falls back to the newest stored one if the API is down.

    Deliberately does not save. Collection stays in collect.py so samples land
    at regular intervals -- a page refresh should not skew the history.
    """
    try:
        return fetch_reading(), True
    except Exception:
        return store.latest(connection), False


def todays_readings(connection):
    midnight = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return store.since(connection, midnight), midnight


def hour_label(hour: int) -> str:
    """0 -> 12a, 13 -> 1p"""
    hour = hour % 24
    return f"{hour % 12 or 12}{'a' if hour < 12 else 'p'}"


def chart_ticks(step_hours: int = 1):
    """Hourly x-axis ticks. Every fourth is "major" and survives on narrow
    screens, where 25 labels would overlap into an unreadable smear."""
    return [
        {
            "pct": hour / 24 * 100,
            "label": hour_label(hour),
            "major": hour % 4 == 0,
        }
        for hour in range(0, 25, step_hours)
    ]


def level_color(fraction) -> str:
    """Green below half full, amber to 85%, red above"""
    if fraction is None:
        return NEUTRAL
    for ceiling, colour in LEVELS:
        if fraction < ceiling:
            return colour
    return LEVELS[-1][1]


def favicon_uri(colour: str) -> str:
    """The tab icon, tinted to match how busy it is right now.

    Inlined as a data URI so there is no static file or route to serve, and
    no second request on page load.
    """
    svg = FAVICON_SVG.format(color=colour)
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def chart_y_ticks(step_percent: int = 25):
    """Horizontal gridline levels, as percent of capacity"""
    return [{"pct": p, "label": f"{p}%"} for p in range(0, 101, step_percent)]


def typical_curve(readings, weekday, today, tz):
    """Median occupancy by time-of-day across prior instances of one weekday.

    Returns (buckets, day_count). Today is excluded so the day is never
    compared against a curve it helped produce -- that would drag the two
    lines together, worst of all early in the morning when today's handful of
    samples is a large share of the total.

    Median rather than mean: one closure or holiday would visibly drag a mean
    when only a few weeks of history exist.
    """
    buckets = defaultdict(list)
    days = set()
    for reading in readings:
        if not reading.capacity:
            continue
        local = reading.observed_at.astimezone(tz)
        if local.weekday() != weekday or local.date() == today:
            continue
        days.add(local.date())
        slot = (local.hour * 60 + local.minute) // BUCKET_MINUTES
        buckets[slot].append(percentage(reading.count, reading.capacity))
    return {slot: median(values) for slot, values in buckets.items()}, len(days)


def curve_points(buckets):
    """Bucketed medians -> an SVG polyline, plotted at each bucket's midpoint"""
    points = []
    for slot in sorted(buckets):
        minutes = slot * BUCKET_MINUTES + BUCKET_MINUTES / 2
        x = minutes / 1440 * CHART_WIDTH
        y = CHART_HEIGHT - buckets[slot] * CHART_HEIGHT
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def chart_points(readings, midnight):
    """Readings -> an SVG polyline, x by time of day, y by percent full"""
    points = []
    for reading in readings:
        if not reading.capacity:
            continue
        elapsed = (reading.observed_at - midnight).total_seconds()
        x = elapsed / 86400 * CHART_WIDTH
        y = CHART_HEIGHT - percentage(reading.count, reading.capacity) * CHART_HEIGHT
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def start_collector(interval_seconds: int) -> None:
    """Collect in a background thread.

    Used in deployment, where there is no cron. Requires gunicorn to run a
    single worker -- more workers would mean duplicate collectors.
    """

    def loop():
        while True:
            try:
                connection = store.connect()
                store.save(connection, fetch_reading())
                connection.close()
            except Exception as error:
                app.logger.warning("collection failed: %s", error)
            time.sleep(interval_seconds)

    threading.Thread(target=loop, daemon=True, name="collector").start()


# Unset locally, so `python app.py` does not collect; cron/collect.py owns that
COLLECT_INTERVAL = int(os.environ.get("COLLECT_INTERVAL", "0"))
if COLLECT_INTERVAL:
    start_collector(COLLECT_INTERVAL)


def local_midnight(day):
    # datetime.combine is avoided because `time` here is the module, not the class
    return datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ)


def requested_date(today, earliest_day):
    """The day to display: ?date=YYYY-MM-DD, clamped to what actually exists"""
    raw = request.args.get("date")
    if not raw:
        return today
    try:
        wanted = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return today
    return min(max(wanted, earliest_day), today)


def open_window(day_hours):
    """Opening hours as minutes since local midnight, or None if unknown"""
    if not day_hours or day_hours.opens is None or day_hours.closes is None:
        return None
    opens, closes = day_hours.opens, day_hours.closes
    if closes <= opens:
        closes += 24 * 60          # a closing time past midnight
    return opens, closes


def quietest_window(readings, midnight, window, minutes=60, min_samples=4):
    """The sustained stretch with the lowest average occupancy.

    A single quiet reading can be a blip; an hour-long dip is something you
    can plan around. The window must fit entirely inside opening hours, so a
    half-hour clipped by closing time cannot win by default.
    """
    if not readings or not window:
        return None
    opens, closes = window

    entries = []
    for reading in readings:
        minute = (reading.observed_at.astimezone(LOCAL_TZ) - midnight).total_seconds() / 60
        entries.append((minute, percentage(reading.count, reading.capacity)))

    best = None
    for start, _ in entries:
        if start < opens or start + minutes > closes:
            continue
        inside = [pct for minute, pct in entries if start <= minute < start + minutes]
        if len(inside) < min_samples:
            continue
        average = mean(inside)
        if best is None or average < best["average_pct"]:
            best = {
                "average_pct": average,
                "start": midnight + timedelta(minutes=start),
                "end": midnight + timedelta(minutes=start + minutes),
            }
    return best


def day_summary(readings, midnight, day_hours):
    """Peak, quietest and average for a day already gone.

    Scoped to opening hours: the gym reads zero all night, and including
    those readings drags the average toward nothing. Falls back to the whole
    day only when the hours are unknown, so the number is never empty.
    """
    usable = [r for r in readings if r.capacity]
    if not usable:
        return None

    window = open_window(day_hours)
    during_open = []
    if window:
        opens, closes = window
        for reading in usable:
            minute = (reading.observed_at.astimezone(LOCAL_TZ) - midnight).total_seconds() / 60
            if opens <= minute <= closes:
                during_open.append(reading)

    scoped = during_open or usable
    peak = max(scoped, key=lambda r: r.count)
    return {
        "peak": peak,
        "peak_pct": percentage(peak.count, peak.capacity),
        "peak_at": peak.observed_at.astimezone(LOCAL_TZ),
        "quietest": quietest_window(scoped, midnight, window),
        "average_pct": mean(percentage(r.count, r.capacity) for r in scoped),
        "open_only": bool(during_open),
    }


@app.route("/")
def index():
    connection = store.connect()
    now_local = datetime.now(LOCAL_TZ)
    today = now_local.date()

    first = store.earliest(connection)
    earliest_day = first.observed_at.astimezone(LOCAL_TZ).date() if first else today

    viewed = requested_date(today, earliest_day)
    is_today = viewed == today

    midnight = local_midnight(viewed)
    readings = store.between(connection, midnight, midnight + timedelta(days=1))

    buckets, weeks = typical_curve(
        store.all_readings(connection), viewed.weekday(), viewed, LOCAL_TZ
    )
    show_typical = weeks >= MIN_WEEKDAY_INSTANCES

    # [minutes since local midnight, count, capacity] -- compact on purpose,
    # this is inlined into the page on every load
    samples = [
        [
            int((r.observed_at.astimezone(LOCAL_TZ) - midnight).total_seconds() // 60),
            r.count,
            r.capacity,
        ]
        for r in readings
    ]

    day_hours = hours.todays_hours(viewed)

    if is_today:
        reading, is_live = current_reading(connection)
        summary = None
    else:
        reading, is_live = None, False
        summary = day_summary(readings, midnight, day_hours)

    # the hero number drives the accent colour: live count today, peak otherwise
    if is_today:
        pct = percentage(reading.count, reading.capacity) if reading and reading.capacity else None
    else:
        pct = summary["peak_pct"] if summary else None

    return render_template(
        "index.html",
        samples=samples,
        hours_today=day_hours,
        reading=reading,
        is_live=is_live,
        is_today=is_today,
        summary=summary,
        viewed=viewed,
        day_label="Today" if is_today else viewed.strftime("%a, %b %-d"),
        full_day_label=viewed.strftime("%A, %B %-d"),
        prev_day=(viewed - timedelta(days=1)).isoformat() if viewed > earliest_day else None,
        next_day=(viewed + timedelta(days=1)).isoformat() if viewed < today else None,
        earliest_day=earliest_day.isoformat(),
        today_iso=today.isoformat(),
        pct=pct,
        level=level_color(pct),
        favicon=favicon_uri(level_color(pct)),
        local_time=reading.observed_at.astimezone(LOCAL_TZ) if reading else None,
        points=chart_points(readings, midnight),
        ticks=chart_ticks(),
        y_ticks=chart_y_ticks(),
        typical_points=curve_points(buckets) if show_typical else "",
        typical_weeks=weeks,
        weekday_name=viewed.strftime("%A"),
        sample_count=len(readings),
        total_rows=store.count_rows(connection),
        width=CHART_WIDTH,
        height=CHART_HEIGHT,
    )


@app.route("/apple-touch-icon.png")
@app.route("/apple-touch-icon-precomposed.png")
def apple_touch_icon():
    """iOS probes these root paths when it cannot find the link tag -- for
    instance from a cached copy of the page saved before the tag existed."""
    return send_from_directory(app.static_folder, "apple-touch-icon.png")


@app.route("/api/current")
def api_current():
    reading, is_live = current_reading(store.connect())
    if reading is None:
        return jsonify({"error": "no reading available"}), 503
    return jsonify(
        {
            "count": reading.count,
            "capacity": reading.capacity,
            "percentage": percentage(reading.count, reading.capacity),
            "observed_at": reading.observed_at.isoformat(),
            "live": is_live,
        }
    )


@app.route("/api/hours")
def api_hours():
    """What the scraper currently believes, and where it got it.

    Exists because hours failures hide themselves on the page: if RecWell
    rewrites the table headers, the line simply vanishes. This shows whether
    the tables still parse and which one each day resolved to.
    """
    markup = hours.cached_markup()
    if not markup:
        return jsonify({"error": "no hours available", "cache": hours.cache_info()}), 503

    def clock(minutes):
        return None if minutes is None else f"{minutes // 60:02d}:{minutes % 60:02d}"

    def resolve(day):
        found = hours.hours_for(day, markup)
        if not found:
            return {"date": day.isoformat(), "weekday": day.strftime("%A"), "hours": None}
        return {
            "date": day.isoformat(),
            "weekday": day.strftime("%A"),
            "text": found.text,
            "opens": clock(found.opens),
            "closes": clock(found.closes),
            "closed": found.opens is None,
            "source": found.source,
        }

    today = datetime.now(LOCAL_TZ).date()
    return jsonify(
        {
            "today": resolve(today),
            "week_ahead": [resolve(today + timedelta(days=n)) for n in range(1, 8)],
            "cache": hours.cache_info(),
            "source_url": hours.HOURS_URL,
            "tables": [
                {"header": header, "rows": rows}
                for header, rows in hours.parse_tables(markup)
            ],
        }
    )


@app.route("/api/history")
def api_history():
    from flask import request

    hours = request.args.get("hours", default=24, type=int)
    start = datetime.now(LOCAL_TZ) - timedelta(hours=hours)
    readings = store.since(store.connect(), start)
    return jsonify(
        [
            {
                "count": r.count,
                "capacity": r.capacity,
                "observed_at": r.observed_at.isoformat(),
            }
            for r in readings
        ]
    )


if __name__ == "__main__":
    # macOS ControlCenter (AirPlay Receiver) squats on 5000, so default to 5001
    app.run(debug=True, port=int(os.environ.get("PORT", 5001)))
