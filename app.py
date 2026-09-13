import base64
import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from pathlib import Path

from flask import Flask, g, jsonify, redirect, request, send_from_directory, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import evaluate
import google_auth
import ask
import hours
import intent
import policy
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
# Signs the session cookie that remembers who is signed in. Without it Flask
# refuses to use sessions at all, so sign-in simply would not work.
app.secret_key = os.environ.get("SECRET_KEY", "")
# Fly terminates TLS at its edge and forwards plain HTTP, so without this
# url_for(_external=True) builds http:// URLs. The OAuth redirect_uri would
# then not match the https one registered with Google.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)


def db():
    """The request's connection, checked out of the pool once and reused."""
    if "db" not in g:
        g.db_cm = store.connection()
        g.db = g.db_cm.__enter__()
    return g.db


@app.teardown_appcontext
def _return_connection(exception):
    """Hand the connection back. A pooled connection that is never returned is
    leaked, unlike a SQLite handle which simply gets garbage collected."""
    cm = g.pop("db_cm", None)
    g.pop("db", None)
    if cm is None:
        return
    if exception is None:
        cm.__exit__(None, None, None)
    else:
        # a failed request rolls back rather than committing a partial write
        cm.__exit__(type(exception), exception, exception.__traceback__)


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


def band_points(bands):
    """[minuteOfDay, median, low, high] at each bucket's midpoint.

    The client draws the geometry; this only says where in the day each
    bucket sits and how much the past instances disagreed there.
    """
    return [
        [
            slot * BUCKET_MINUTES + BUCKET_MINUTES // 2,
            bands[slot]["median"],
            bands[slot]["low"],
            bands[slot]["high"],
        ]
        for slot in sorted(bands)
    ]


def with_forecast(bands, by_hour):
    """The curve's bands with each half hour's median replaced by the forest's
    forecast for that hour. Only the line moves: the spread across past
    instances stays the curve's, because the forest has no band of its own."""
    return {
        slot: {**band, "median": by_hour[(slot * BUCKET_MINUTES) // 60]}
        for slot, band in bands.items()
    }


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


_collector: threading.Thread | None = None


def start_collector(interval_seconds: int) -> None:
    """Collect in a background thread.

    Used in deployment, where there is no cron. Requires gunicorn to run a
    single worker -- more workers would mean duplicate collectors.
    """

    def loop():
        while True:
            try:
                with store.connection() as conn:
                    store.save(conn, fetch_reading())
            except Exception as error:
                app.logger.warning("collection failed: %s", error)
            time.sleep(interval_seconds)

    global _collector
    _collector = threading.Thread(target=loop, daemon=True, name="collector")
    _collector.start()


# No reading for this long means something is wrong. Three missed cycles at
# the deployed interval, which tolerates a transient API failure without
# crying wolf.
STALE_AFTER_SECONDS = 15 * 60

# A count of at least this many, unchanged for this long, is a stalled sensor
# rather than a crowd; the history import drops such stretches by the same
# rule. The floor is there because an empty gym legitimately reads 0 all night.
FROZEN_MIN_COUNT = 10
FROZEN_AFTER_SECONDS = 60 * 60

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


def day_summary(readings, midnight, day_hours, connection=None):
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

    # The open-hours bounds are worked out here because they depend on
    # DST-aware local time; once they are UTC instants the aggregation itself
    # is timezone-free, so the database does it.
    stats = None
    if connection is not None and window:
        opens, closes = window
        stats = store.day_statistics(
            connection,
            midnight + timedelta(minutes=opens),
            midnight + timedelta(minutes=closes),
        )
    if stats is None:
        peak = max(scoped, key=lambda r: r.count)
        stats = {
            "peak_count": peak.count,
            "peak_capacity": peak.capacity,
            "peak_at": peak.observed_at,
            "average_pct": mean(percentage(r.count, r.capacity) for r in scoped),
        }

    return {
        "peak": SimpleNamespace(count=stats["peak_count"], capacity=stats["peak_capacity"]),
        "peak_pct": percentage(stats["peak_count"], stats["peak_capacity"]),
        "peak_at": stats["peak_at"].astimezone(LOCAL_TZ),
        "quietest": quietest_window(scoped, midnight, window),
        "average_pct": stats["average_pct"],
        "open_only": bool(during_open),
    }


@app.route("/")
def index():
    """Serve the built React app.

    Every view lives at / with a ?date= query the client reads, so there is
    no server-side routing to keep in step with the frontend.
    """
    build = Path(app.static_folder) / "app" / "index.html"
    if not build.exists():
        return (
            "Frontend not built. Run: cd frontend && npm install && npm run build",
            503,
        )
    return send_from_directory(build.parent, "index.html")


def signed_in_email():
    return session.get("email")


def busy_today(midnight, connection):
    """The signed-in user's busy blocks for the day, or () when not linked.

    Failures here return () rather than propagating: a calendar outage should
    degrade suggestions to "as if you were free", not break the dashboard.
    """
    email = signed_in_email()
    if not email:
        return ()
    try:
        refresh_token = google_auth.load_refresh_token(connection, email)
        if not refresh_token:
            return ()
        access_token = google_auth.access_token_from(refresh_token)
        return google_auth.busy_intervals(access_token, midnight, midnight + timedelta(days=1))
    except google_auth.NeedsReauth:
        session.pop("email", None)          # prompt a fresh sign-in
        return ()
    except Exception as error:
        app.logger.warning("free/busy lookup failed: %s", error)
        return ()


@app.route("/auth/google")
def auth_start():
    state = google_auth.new_state()
    session["oauth_state"] = state
    return redirect(google_auth.authorize_url(url_for("auth_callback", _external=True), state))


@app.route("/auth/callback")
def auth_callback():
    # state ties the callback to the browser that started the flow
    if not request.args.get("state") or request.args["state"] != session.pop("oauth_state", None):
        return "Sign-in expired or was tampered with. Please try again.", 400
    if "code" not in request.args:
        return f"Google declined: {request.args.get('error', 'unknown')}", 400

    tokens = google_auth.exchange_code(
        request.args["code"], url_for("auth_callback", _external=True)
    )
    email = google_auth.email_from_id_token(tokens.get("id_token", ""))
    if not email or email not in google_auth.allowed_emails():
        return "That account is not allowed to sign in here.", 403
    if "refresh_token" not in tokens:
        return "Google did not return a refresh token. Revoke access and try again.", 400

    google_auth.save_refresh_token(db(), email, tokens["refresh_token"])
    session["email"] = email
    return redirect("/")


def access_token_for(email, connection):
    refresh_token = google_auth.load_refresh_token(connection, email)
    if not refresh_token:
        return None
    return google_auth.access_token_from(refresh_token)


def app_calendar_id(access_token, connection) -> str:
    """The app's own calendar, created on first use and remembered."""
    existing = store.get_state(connection, "app_calendar_id")
    if existing:
        return existing
    created = google_auth.create_calendar(access_token)
    store.set_state(connection, "app_calendar_id", created)
    return created


@app.route("/api/book", methods=["POST"])
def api_book():
    """Write a confirmed window to the app's calendar.

    The user proposes nothing here -- the policy did. This only acts on a
    window the policy is currently suggesting, so a crafted request cannot
    write arbitrary events, and a stale tab cannot book a window that is no
    longer sensible.
    """
    email = signed_in_email()
    if not email:
        return jsonify({"error": "not signed in"}), 401

    wanted = (request.get_json(silent=True) or {}).get("start")
    if not wanted:
        return jsonify({"error": "no start time given"}), 400

    connection = db()
    today = datetime.now(LOCAL_TZ).date()
    view = day_view(connection, today, today, today)
    windows = (view.get("suggestions") or {}).get("windows") or []
    chosen = next((w for w in windows if w["start"] == wanted), None)
    if not chosen:
        return jsonify({"error": "that window is no longer suggested"}), 409

    try:
        access_token = access_token_for(email, connection)
        if not access_token:
            return jsonify({"error": "calendar not linked"}), 401
        calendar_id = app_calendar_id(access_token, connection)

        # replace rather than duplicate: one booking per day
        previous = store.booking_on(connection, today)
        if previous:
            google_auth.delete_event(access_token, calendar_id, previous["event_id"])

        start = datetime.fromisoformat(chosen["start"])
        end = datetime.fromisoformat(chosen["end"])
        prediction_id = store.log_prediction(
            connection, today, start, end, chosen["predictedPct"],
            (view.get("typical") or {}).get("weeks", 0), chosen.get("spread"),
            chosen.get("section"),
        )
        event_id = google_auth.create_event(
            access_token, calendar_id, start, end,
            summary="Gym — RSF",
            description=(
                f"Suggested by RSF Dashboard. Typically about "
                f"{round(chosen['predictedPct'] * 100)}% full at this time.\n"
                f"https://rsf-dashboard.fly.dev"
            ),
        )
        store.save_booking(connection, today, event_id, start, end,
                           chosen["predictedPct"], prediction_id)
    except google_auth.NeedsReauth:
        session.pop("email", None)
        return jsonify({"error": "sign in again"}), 401
    except Exception as error:
        app.logger.warning("booking failed: %s", error)
        return jsonify({"error": "could not write to the calendar"}), 502

    return jsonify({"booked": {"start": chosen["start"], "end": chosen["end"]}})


def stored_preferences(connection):
    raw = store.get_state(connection, "preferences")
    return json.loads(raw) if raw else None


MAX_CHAT_TURNS = 8


@app.route("/api/ask", methods=["POST"])
def api_ask():
    """Conversation about the occupancy data, and the only way preferences
    are set.

    Deliberately the single text input on the page. The dashboard already
    answers "how busy is it" and "when should I go" better than a sentence
    could, and those stay as they are; this covers the open-ended questions
    and the settings that shape the suggestions.

    The model reaches the data only through vetted tools with typed
    arguments, so it cannot write SQL or reach another table. The one tool
    that writes touches a single settings row, which is visible on the page
    and reversible in a sentence.
    """
    if not signed_in_email():
        return jsonify({"error": "not signed in"}), 401
    if not ask.available():
        return jsonify({"error": "not configured"}), 503
    if not ask.within_rate_limit():
        return jsonify({"error": "too many questions in the last hour"}), 429

    payload = request.get_json(silent=True) or {}
    # The client keeps the transcript; the server trims it. History from a
    # client is not trusted beyond its shape -- roles are filtered and the
    # length is capped in ask.answer.
    messages = payload.get("messages")
    if not messages:
        question = (payload.get("question") or "").strip()
        messages = [{"role": "user", "content": question}] if question else None
    if not messages:
        return jsonify({"error": "no question given"}), 400

    result = ask.answer(messages)
    if result is None:
        return jsonify({"error": "could not answer that"}), 502
    return jsonify(result)


@app.route("/api/preferences", methods=["DELETE"])
def api_clear_preferences():
    if not signed_in_email():
        return jsonify({"error": "not signed in"}), 401
    store.set_state(db(), "preferences", "")
    return jsonify({"preferences": None})


@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    if not signed_in_email():
        return jsonify({"error": "not signed in"}), 401
    payload = request.get_json(silent=True) or {}
    if "predictionId" not in payload or "went" not in payload:
        return jsonify({"error": "predictionId and went are required"}), 400

    connection = db()
    store.record_feedback(connection, int(payload["predictionId"]), bool(payload["went"]))
    return jsonify({"answered": bool(payload["went"])})


@app.route("/api/book", methods=["DELETE"])
def api_unbook():
    email = signed_in_email()
    if not email:
        return jsonify({"error": "not signed in"}), 401

    connection = db()
    today = datetime.now(LOCAL_TZ).date()
    booking = store.booking_on(connection, today)
    if not booking:
        return jsonify({"booked": None})

    try:
        access_token = access_token_for(email, connection)
        calendar_id = store.get_state(connection, "app_calendar_id")
        if access_token and calendar_id:
            google_auth.delete_event(access_token, calendar_id, booking["event_id"])
    except Exception as error:
        # the row goes regardless; a stale event the user already deleted
        # should not leave the dashboard permanently stuck
        app.logger.warning("could not delete calendar event: %s", error)
    store.delete_booking(connection, today)
    return jsonify({"booked": None})


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    email = session.pop("email", None)
    if email and request.args.get("forget"):
        google_auth.forget(db(), email)
    return jsonify({"signedIn": False})


@app.route("/health")
def health():
    """Liveness and data freshness, deliberately separated.

    The HTTP status means "a restart might help": the database is unreachable,
    or the collector thread has died while gunicorn carried on serving. Both
    are conditions a restart plausibly fixes, so Fly's health check can act on
    them.

    Stale data is reported in the body but does not fail the check. If the
    upstream sensor API is down, restarting this machine repeatedly changes
    nothing and would turn one outage into a crash loop -- that is a human's
    problem, not a supervisor's.
    """
    report = {
        "collector": None,
        "database": "unreachable",
        "lastReadingAt": None,
        "ageSeconds": None,
        "stale": None,
        "gapMinutes": None,
    }
    if COLLECT_INTERVAL:
        report["collector"] = "alive" if (_collector and _collector.is_alive()) else "dead"

    try:
        connection = db()
        row = connection.execute(
            """SELECT MAX(observed_at) AS newest,
                      COUNT(*) FILTER (WHERE observed_at > NOW() - INTERVAL '6 hours') AS recent
               FROM readings"""
        ).fetchone()
        report["database"] = "ok"
        if row["newest"]:
            age = (datetime.now(timezone.utc) - row["newest"]).total_seconds()
            report["lastReadingAt"] = row["newest"].astimezone(LOCAL_TZ).isoformat()
            report["ageSeconds"] = round(age)
            report["stale"] = age > STALE_AFTER_SECONDS
        gap = connection.execute(
            """WITH deltas AS (
                   SELECT observed_at - LAG(observed_at) OVER (ORDER BY observed_at) AS d
                   FROM readings WHERE observed_at > NOW() - INTERVAL '24 hours'
               )
               SELECT EXTRACT(EPOCH FROM MAX(d)) / 60 AS worst FROM deltas"""
        ).fetchone()
        # EXTRACT returns Decimal, which json renders as a string
        report["gapMinutes"] = round(float(gap["worst"]), 1) if gap and gap["worst"] else None
    except Exception as error:
        app.logger.warning("health check could not reach the database: %s", error)

    restartable = report["database"] != "ok" or report["collector"] == "dead"
    report["ok"] = not restartable
    return jsonify(report), (503 if restartable else 200)


def _now() -> datetime:
    """The current instant, behind a function so tests can fix the clock."""
    return datetime.now(timezone.utc)


@app.route("/health/freshness")
def freshness():
    """Fails when readings have stopped arriving, or the count has frozen.

    Separate from /health on purpose. Fly's health check watches /health and
    restarts on failure, so staleness must not fail that -- a dead sensor API
    is not fixed by restarting. This endpoint is for an external monitor,
    which should page a human instead. Any uptime service can watch it; no
    JSON keyword matching required.

    A frozen count is staleness the timestamps cannot show. The API reports no
    measurement time, so readings are stamped when fetched, and a stalled
    sensor keeps answering on time with the same number. The hour is measured
    between readings, not up to now: readings that stop are already stale, and
    silence is no evidence that the count held.
    """
    try:
        row = db().execute(
            """SELECT latest.observed_at AS newest, latest.count,
                      -- the first reading since the count last changed
                      (SELECT MIN(observed_at) FROM readings
                       WHERE observed_at > COALESCE(
                           (SELECT MAX(observed_at) FROM readings
                            WHERE count <> latest.count),
                           '-infinity')) AS unchanged_since
               FROM (SELECT observed_at, count FROM readings
                     ORDER BY observed_at DESC LIMIT 1) AS latest"""
        ).fetchone()
    except Exception as error:
        return jsonify({"fresh": False, "reason": f"database unreachable: {error}"}), 503

    if not row:
        return jsonify({"fresh": False, "reason": "no readings recorded"}), 503

    age = (_now() - row["newest"]).total_seconds()
    held = (row["newest"] - row["unchanged_since"]).total_seconds()
    stale = age > STALE_AFTER_SECONDS
    frozen = row["count"] >= FROZEN_MIN_COUNT and held >= FROZEN_AFTER_SECONDS
    fresh = not stale and not frozen
    body = {
        "fresh": fresh,
        "stale": stale,
        "frozen": frozen,
        "ageSeconds": round(age),
        "thresholdSeconds": STALE_AFTER_SECONDS,
        "lastReadingAt": row["newest"].astimezone(LOCAL_TZ).isoformat(),
        "count": row["count"],
        "unchangedSince": row["unchanged_since"].astimezone(LOCAL_TZ).isoformat(),
    }
    reasons = []
    if stale:
        reasons.append(f"no reading for {round(age / 60)} minutes")
    if frozen:
        reasons.append(f"count frozen at {row['count']} for {round(held / 60)} minutes")
    if reasons:
        body["reason"] = "; ".join(reasons)
    return jsonify(body), (200 if fresh else 503)


@app.route("/privacy")
def privacy():
    """A real policy, not a formality: the app reads a user's calendar
    availability, and Google requires a published policy to leave Testing."""
    return send_from_directory(app.static_folder, "privacy.html")


@app.route("/apple-touch-icon.png")
@app.route("/apple-touch-icon-precomposed.png")
def apple_touch_icon():
    """iOS probes these root paths when it cannot find the link tag -- for
    instance from a cached copy of the page saved before the tag existed."""
    return send_from_directory(app.static_folder, "apple-touch-icon.png")


@app.route("/api/current")
def api_current():
    reading, is_live = current_reading(db())
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


def feedback_prompt(connection, viewed, is_today):
    """Ask "did you go?" about a booked window, once the day is over.

    Only about bookings: seeing three suggestions and booking none is already
    a signal, so there is nothing to ask. Attendance cannot be derived from
    the sensor, which counts bodies at a doorway rather than identities, so
    this is the only place the answer can come from.
    """
    if is_today:
        return None
    booking = store.booking_on(connection, viewed)
    if not booking or not booking.get("prediction_id"):
        return None
    return {
        "predictionId": booking["prediction_id"],
        "start": booking["starts_at"].astimezone(LOCAL_TZ).isoformat(),
        "end": booking["ends_at"].astimezone(LOCAL_TZ).isoformat(),
        "answered": store.feedback_for(connection, booking["prediction_id"]),
    }


def day_view(connection, viewed, today, earliest_day):
    """Everything the dashboard needs for one day, as plain JSON-ready data.

    The React client renders from this; nothing about layout or SVG geometry
    is decided here beyond the minute-of-day the samples fall on.
    """
    midnight = local_midnight(viewed)
    readings = store.between(connection, midnight, midnight + timedelta(days=1))
    is_today = viewed == today
    day_hours = hours.todays_hours(viewed)

    def minute_of(reading):
        return int((reading.observed_at.astimezone(LOCAL_TZ) - midnight).total_seconds() // 60)

    live = None
    if is_today:
        reading, is_live = current_reading(connection)
        if reading:
            live = {
                "count": reading.count,
                "capacity": reading.capacity,
                "percentage": percentage(reading.count, reading.capacity) if reading.capacity else None,
                "observedAt": reading.observed_at.astimezone(LOCAL_TZ).isoformat(),
                "isLive": is_live,
            }

    summary = None
    if not is_today:
        found = day_summary(readings, midnight, day_hours)
        if found:
            quietest = found["quietest"]
            summary = {
                "peak": {
                    "count": found["peak"].count,
                    "capacity": found["peak"].capacity,
                    "percentage": found["peak_pct"],
                    "at": found["peak_at"].isoformat(),
                },
                "quietest": quietest and {
                    "percentage": quietest["average_pct"],
                    "start": quietest["start"].isoformat(),
                    "end": quietest["end"].isoformat(),
                },
                "averagePct": found["average_pct"],
                "openOnly": found["open_only"],
            }

    # The whole curve is one query now: AT TIME ZONE makes the local weekday
    # and bucket DST-correct, percentile_cont gives a real median, and the
    # rolling window is a LIMIT. Nothing is loaded into Python to be bucketed.
    bands, weeks, spread = store.weekday_bands(
        connection, viewed, str(LOCAL_TZ), BUCKET_MINUTES, evaluate.WINDOW_INSTANCES
    )
    # Today's line is the random forest's when the forecast job has stored one,
    # since it beat the curve by two points in backtesting. The band stays the
    # curve's spread, and with no forecast -- the job failed or has not run
    # yet -- the line and the suggestions fall back to the curve.
    forecast = (store.forecast_for(connection, viewed)
                if is_today and weeks >= MIN_WEEKDAY_INSTANCES else None)
    if forecast:
        bands = with_forecast(bands, forecast["by_hour"])
    typical = None
    if weeks >= MIN_WEEKDAY_INSTANCES:
        typical = {
            "weeks": weeks,
            "weekday": viewed.strftime("%A"),
            "spread": spread,
            "points": band_points(bands),
            # what the instances were drawn from, so a curve built from last
            # spring does not pass itself off as simply "8 past Mondays"
            "period": store.period_of(connection, viewed),
            "source": "forest" if forecast else "curve",
        }

    # Suggestions are for today only: "when should I go" is not a question
    # about a day that is over, and the stat tiles already say what happened.
    preferences = stored_preferences(connection)

    suggestions = None
    if is_today and weeks < MIN_WEEKDAY_INSTANCES:
        # weekday_bands returns bands from any number of instances; the
        # three-instance gate is applied to the drawn curve, so without this
        # the policy would happily recommend from a single past weekday.
        suggestions = {
            "windows": [],
            "refusal": (
                f"needs {MIN_WEEKDAY_INSTANCES} past {viewed.strftime('%A')}s to predict from"
                f" — {weeks} so far"
            ),
        }
    elif is_today:
        found = policy.suggest_by_section(
            bands, midnight, day_hours, BUCKET_MINUTES,
            busy=busy_today(midnight, connection),
            not_before=datetime.now(LOCAL_TZ),
            preferences=preferences,
        )
        history = store.section_outcomes(connection)
        windows = []
        for window in found.windows:
            # recorded as shown: the model will change, and recomputing later
            # would score today's model against a decision it never made
            store.log_prediction(
                connection, viewed, window.start, window.end,
                window.predicted_pct, weeks, window.spread, window.section,
                model="forest" if forecast else "curve",
            )
            windows.append({
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
                "predictedPct": window.predicted_pct,
                "spread": window.spread,
                "section": window.section,
                # what actually happened last time this part of the day was
                # suggested; annotation only, never suppression
                "note": policy.section_note(history.get(window.section)),
            })
        suggestions = {"windows": windows, "refusal": found.refusal}

    return {
        "date": viewed.isoformat(),
        "isToday": is_today,
        "label": viewed.strftime("%A, %B %-d"),
        "shortLabel": "Today" if is_today else viewed.strftime("%a, %b %-d"),
        "nav": {
            "prev": (viewed - timedelta(days=1)).isoformat() if viewed > earliest_day else None,
            "next": (viewed + timedelta(days=1)).isoformat() if viewed < today else None,
            "earliest": earliest_day.isoformat(),
            "today": today.isoformat(),
        },
        "live": live,
        "summary": summary,
        "samples": [[minute_of(r), r.count, r.capacity] for r in readings],
        "typical": typical,
        "suggestions": suggestions,
        "booking": (
            {
                # local time, so it is directly comparable with suggestion
                # windows rather than being the same instant spelled in UTC
                "start": booked["starts_at"].astimezone(LOCAL_TZ).isoformat(),
                "end": booked["ends_at"].astimezone(LOCAL_TZ).isoformat(),
                "predictedPct": booked["predicted_pct"],
            }
            if is_today and (booked := store.booking_on(connection, viewed))
            else None
        ),
        "feedback": feedback_prompt(connection, viewed, is_today),
        "stale": (
            (datetime.now(LOCAL_TZ) - reading.observed_at).total_seconds() > STALE_AFTER_SECONDS
            if is_today and reading else False
        ),
        "preferences": preferences,
        "askAvailable": ask.available(),
        "auth": {
            "signedIn": bool(signed_in_email()),
            "email": signed_in_email(),
            # Sign-in only gates calendar-derived output; occupancy stays public
            "calendarAware": bool(signed_in_email()),
        },
        "hours": day_hours and {
            "text": day_hours.text,
            "opens": day_hours.opens,
            "closes": day_hours.closes,
            "closed": day_hours.opens is None,
        },
    }


@app.route("/api/day")
def api_day():
    connection = db()
    today = datetime.now(LOCAL_TZ).date()
    first = store.earliest(connection)
    earliest_day = first.observed_at.astimezone(LOCAL_TZ).date() if first else today
    return jsonify(day_view(connection, requested_date(today, earliest_day), today, earliest_day))


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
    readings = store.since(db(), start)
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
