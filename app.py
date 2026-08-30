import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template

import store
from density import fetch_reading, percentage

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
CHART_WIDTH = 720
CHART_HEIGHT = 180

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


def chart_ticks(step_hours: int = 4):
    """Evenly spaced x-axis ticks across a midnight-to-midnight day"""
    return [
        {"pct": hour / 24 * 100, "label": hour_label(hour)}
        for hour in range(0, 25, step_hours)
    ]


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


@app.route("/")
def index():
    connection = store.connect()
    reading, is_live = current_reading(connection)
    readings, midnight = todays_readings(connection)
    return render_template(
        "index.html",
        reading=reading,
        is_live=is_live,
        pct=percentage(reading.count, reading.capacity) if reading and reading.capacity else None,
        local_time=reading.observed_at.astimezone(LOCAL_TZ) if reading else None,
        points=chart_points(readings, midnight),
        ticks=chart_ticks(),
        sample_count=len(readings),
        width=CHART_WIDTH,
        height=CHART_HEIGHT,
    )


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
