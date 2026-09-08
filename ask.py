"""Conversation about the occupancy history: patterns, comparisons, forecasts.

The shape of the data is small enough to hand over whole. A weekday-by-hour
grid is 7 x 24 = 168 cells however long collection runs -- about 550 tokens --
so the model can hold the entire picture at once and reason over it, rather
than guessing which slice to query. That is what makes open-ended questions
("what patterns do you see", "how busy will Thursday evening be") answerable
at all; a fixed set of lookups can answer "what was X" and nothing more.

Precise tools remain for exact figures over a chosen slice. The model gets
tools and an overview, never a database: it cannot write SQL, reach another
table, or write anything at all.
"""

import json
import os
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic
from anthropic import beta_tool

import intent
import store

MODEL = "claude-opus-5"
MAX_TOKENS = 4096
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
BUCKET_MINUTES = 30
WINDOW_INSTANCES = 8

# Each turn costs money and runs several queries, unlike the one-shot
# preference parse. A single person cannot legitimately need more than this.
RATE_LIMIT_PER_HOUR = 20
_recent: list[float] = []

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _local_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ)


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


@beta_tool
def data_overview() -> str:
    """The whole picture: average occupancy for every weekday and hour.

    Call this first for any question about patterns, comparisons or what to
    expect. It returns the full grid, so you can reason over the shape of the
    week directly instead of querying slice by slice.
    """
    with store.connection() as conn:
        grid = conn.execute(
            """WITH local AS (
                   SELECT observed_at AT TIME ZONE %(zone)s AS t,
                          count::float / capacity AS pct
                   FROM readings WHERE capacity > 0
               )
               SELECT EXTRACT(ISODOW FROM t)::int AS dow,
                      EXTRACT(HOUR FROM t)::int AS hour,
                      ROUND(AVG(pct)::numeric, 3) AS mean,
                      ROUND(MAX(pct)::numeric, 3) AS peak,
                      COUNT(DISTINCT t::date) AS days
               FROM local GROUP BY 1, 2 ORDER BY 1, 2""",
            {"zone": str(LOCAL_TZ)},
        ).fetchall()
        days = conn.execute(
            """WITH local AS (
                   SELECT observed_at AT TIME ZONE %(zone)s AS t,
                          count::float / capacity AS pct
                   FROM readings WHERE capacity > 0
               )
               SELECT t::date AS day,
                      ROUND(AVG(pct)::numeric, 3) AS mean,
                      ROUND(MAX(pct)::numeric, 3) AS peak,
                      COUNT(*) AS samples
               FROM local GROUP BY 1 ORDER BY 1 DESC LIMIT 90""",
            {"zone": str(LOCAL_TZ)},
        ).fetchall()

    if not grid:
        return json.dumps({"error": "no readings recorded yet"})
    return json.dumps({
        "capacity": 150,
        "today": datetime.now(LOCAL_TZ).date().isoformat(),
        "note": (
            "Occupancy is a fraction of capacity. The sensor counts entries minus"
            " exits, so values above 1.0 are real. 'days' is how many distinct"
            " days contributed to a cell -- treat a cell backed by one or two days"
            " as weak evidence."
        ),
        "weekday_hour_grid": {
            "columns": ["weekday", "hour", "mean", "peak", "days"],
            "rows": [
                [WEEKDAYS[r["dow"] - 1], r["hour"], float(r["mean"]),
                 float(r["peak"]), r["days"]]
                for r in grid
            ],
        },
        "by_day": {
            "columns": ["date", "weekday", "mean", "peak", "samples"],
            "rows": [
                [r["day"].isoformat(), WEEKDAYS[r["day"].weekday()],
                 float(r["mean"]), float(r["peak"]), r["samples"]]
                for r in days
            ],
        },
    })


@beta_tool
def data_range() -> str:
    """What the occupancy history covers, and today's date.

    Call this first when a question involves dates, so you do not ask about
    days that were never recorded.
    """
    with store.connection() as conn:
        row = conn.execute(
            """SELECT MIN(observed_at) AS first, MAX(observed_at) AS last,
                      COUNT(*) AS readings FROM readings"""
        ).fetchone()
    if not row or not row["first"]:
        return json.dumps({"error": "no readings recorded yet"})
    return json.dumps({
        "first_day": row["first"].astimezone(LOCAL_TZ).date().isoformat(),
        "last_day": row["last"].astimezone(LOCAL_TZ).date().isoformat(),
        "readings": row["readings"],
        "today": datetime.now(LOCAL_TZ).date().isoformat(),
        "capacity": 150,
        "note": "Counts can exceed capacity; the sensor counts entries minus exits.",
    })


@beta_tool
def occupancy_stats(
    start_date: str,
    end_date: str,
    weekday: str | None = None,
    start_hour: int | None = None,
    end_hour: int | None = None,
) -> str:
    """Occupancy over a date range, as a percentage of capacity.

    Args:
        start_date: First local day to include, YYYY-MM-DD.
        end_date: Last local day to include, inclusive, YYYY-MM-DD.
        weekday: Optional weekday name to narrow to, e.g. "Monday".
        start_hour: Optional earliest local hour to include, 0-23.
        end_hour: Optional latest local hour to include, 0-23.
    """
    try:
        start, end = _parse_date(start_date), _parse_date(end_date)
    except ValueError:
        return json.dumps({"error": "dates must be YYYY-MM-DD"})

    clauses, params = [], {
        "start": _local_midnight(start),
        "end": _local_midnight(end + timedelta(days=1)),
        "zone": str(LOCAL_TZ),
    }
    if weekday:
        if weekday.capitalize() not in WEEKDAYS:
            return json.dumps({"error": f"unknown weekday {weekday!r}"})
        clauses.append("EXTRACT(ISODOW FROM local_at) = %(isodow)s")
        params["isodow"] = WEEKDAYS.index(weekday.capitalize()) + 1
    if start_hour is not None:
        clauses.append("EXTRACT(HOUR FROM local_at) >= %(start_hour)s")
        params["start_hour"] = start_hour
    if end_hour is not None:
        clauses.append("EXTRACT(HOUR FROM local_at) <= %(end_hour)s")
        params["end_hour"] = end_hour
    narrowing = (" AND " + " AND ".join(clauses)) if clauses else ""

    with store.connection() as conn:
        row = conn.execute(
            f"""WITH local AS (
                    SELECT observed_at AT TIME ZONE %(zone)s AS local_at,
                           count::float / capacity AS pct, count
                    FROM readings
                    WHERE observed_at >= %(start)s AND observed_at < %(end)s
                      AND capacity > 0
                )
                SELECT COUNT(*) AS samples,
                       COUNT(DISTINCT local_at::date) AS days,
                       AVG(pct) AS mean_pct,
                       MAX(pct) AS peak_pct,
                       MIN(pct) AS lowest_pct
                FROM local WHERE TRUE{narrowing}""",
            params,
        ).fetchone()

    if not row or not row["samples"]:
        return json.dumps({"samples": 0, "note": "no readings matched"})
    return json.dumps({
        "samples": row["samples"],
        "days_covered": row["days"],
        "mean_pct": round(float(row["mean_pct"]), 3),
        "peak_pct": round(float(row["peak_pct"]), 3),
        "lowest_pct": round(float(row["lowest_pct"]), 3),
    })


@beta_tool
def typical_weekday_curve(weekday: str) -> str:
    """The typical shape of one weekday: median occupancy per half hour.

    Args:
        weekday: Weekday name, e.g. "Monday".
    """
    if weekday.capitalize() not in WEEKDAYS:
        return json.dumps({"error": f"unknown weekday {weekday!r}"})

    today = datetime.now(LOCAL_TZ).date()
    ahead = (WEEKDAYS.index(weekday.capitalize()) - today.weekday()) % 7
    target = today + timedelta(days=ahead or 7)

    with store.connection() as conn:
        bands, instances, spread = store.weekday_bands(
            conn, target, str(LOCAL_TZ), BUCKET_MINUTES, WINDOW_INSTANCES
        )
    if instances < 3:
        return json.dumps({
            "instances": instances,
            "note": "fewer than three past instances, so there is no reliable curve yet",
        })
    return json.dumps({
        "weekday": weekday.capitalize(),
        "instances": instances,
        "mean_spread": round(spread, 3) if spread else None,
        "curve": [
            {
                "time": f"{(slot * BUCKET_MINUTES) // 60:02d}:{(slot * BUCKET_MINUTES) % 60:02d}",
                "median_pct": round(band["median"], 3),
                "low_pct": round(band["low"], 3),
                "high_pct": round(band["high"], 3),
            }
            for slot, band in sorted(bands.items())
        ],
    })


@beta_tool
def my_sessions() -> str:
    """Which suggested windows were booked, and whether they were attended.

    Only covers windows the dashboard suggested and the user confirmed.
    """
    with store.connection() as conn:
        rows = conn.execute(
            """SELECT b.for_date, b.starts_at, b.ends_at, b.predicted_pct, f.went
               FROM bookings b LEFT JOIN feedback f ON f.prediction_id = b.prediction_id
               ORDER BY b.for_date DESC LIMIT 60"""
        ).fetchall()
    if not rows:
        return json.dumps({"sessions": [], "note": "nothing has been booked yet"})
    return json.dumps({"sessions": [
        {
            "date": row["for_date"].isoformat(),
            "start": row["starts_at"].astimezone(LOCAL_TZ).strftime("%H:%M"),
            "end": row["ends_at"].astimezone(LOCAL_TZ).strftime("%H:%M"),
            "predicted_pct": round(row["predicted_pct"], 3) if row["predicted_pct"] else None,
            "attended": row["went"],
        }
        for row in rows
    ]})


@beta_tool
def current_preferences() -> str:
    """The scheduling preferences currently in effect, if any."""
    with store.connection() as conn:
        raw = store.get_state(conn, "preferences")
    return raw or json.dumps({"preferences": None, "note": "none set"})


@beta_tool
def set_preferences(
    session_minutes: int | None = None,
    earliest_hour: int | None = None,
    latest_hour: int | None = None,
    max_crowding_pct: float | None = None,
    travel_buffer_minutes: int | None = None,
    sessions_per_week: int | None = None,
) -> str:
    """Set the user's scheduling preferences, replacing any already set.

    Call this when the user says what they want from suggestions -- session
    length, hours they will go, how busy is too busy, clearance around
    meetings. Pass only the fields they actually expressed; omit the rest.
    Values are validated and may be adjusted, so read back what is returned
    rather than what you sent.

    Args:
        session_minutes: How long a workout should be.
        earliest_hour: Earliest local hour they would go, 0-23.
        latest_hour: Latest local hour a session may start, 0-23.
        max_crowding_pct: Busiest they will tolerate, as a fraction (0.5 = half full).
        travel_buffer_minutes: Clearance needed either side of a calendar commitment.
        sessions_per_week: How many workouts a week they are aiming for.
    """
    stated = {
        "session_minutes": session_minutes,
        "earliest_hour": earliest_hour,
        "latest_hour": latest_hour,
        "max_crowding_pct": max_crowding_pct,
        "travel_buffer_minutes": travel_buffer_minutes,
        "sessions_per_week": sessions_per_week,
    }
    if all(value is None for value in stated.values()):
        return json.dumps({"error": "nothing to set; no preference was expressed"})

    # The schema fixes the shape; these validators fix the sense
    checked = intent.Preferences(summary="", **stated)
    saved = {k: v for k, v in checked.model_dump().items()
             if k not in ("summary", "clauses")}
    with store.connection() as conn:
        store.set_state(conn, "preferences", json.dumps(saved))
    return json.dumps({"saved": {k: v for k, v in saved.items() if v is not None}})


@beta_tool
def clear_preferences() -> str:
    """Remove all scheduling preferences, returning suggestions to defaults."""
    with store.connection() as conn:
        store.set_state(conn, "preferences", "")
    return json.dumps({"cleared": True})


TOOLS = [data_overview, data_range, occupancy_stats, typical_weekday_curve,
         my_sessions, current_preferences, set_preferences, clear_preferences]

SYSTEM = """You are an analyst for a dataset of gym occupancy readings from the \
UC Berkeley RSF weight rooms. You discuss patterns, make comparisons, and
forecast what to expect, in conversation.

The data
- Occupancy is a fraction of a 150-person capacity. A doorway sensor counts
  entries minus exits every few minutes, so values above 1.0 are real.
- Call data_overview first for almost anything. It gives you the entire
  weekday-by-hour grid plus per-day summaries, so you can look for patterns
  yourself rather than querying blindly. Use the narrower tools afterwards
  when you need an exact figure for a specific slice.

How to answer
- Two or three sentences. This is a panel on a dashboard, not a report.
- Plain prose. No headings, no tables, no bullet lists, unless the user asks
  for a breakdown.
- Lead with the answer. No preamble and no restating the question.
- Quantify: "evenings peak near 85% between 4 and 7pm", not "evenings are
  busy". One or two figures carry an answer; five bury it.
- When asked what to expect, put what it rests on in the same sentence:
  "around 70%, from three Thursdays".
- Sample size governs confidence. A cell backed by one or two days is weak
  evidence; say so in a clause rather than a paragraph. "Not enough data yet"
  is a complete answer when it is true.
- If you noticed something else worth knowing, name it in a clause and stop --
  "Fridays are the exception" -- and let them ask. Do not explain unprompted.
- Be willing to say a pattern is absent.

Settings
- You can set the user's scheduling preferences, which filter the windows the
  dashboard suggests: session length, the hours they will go, how busy is too
  busy, clearance around meetings. When they express one, call
  set_preferences and confirm in a sentence what now applies.
- Only set what they actually said. Inventing a preference is worse than
  setting none. "Mornings if possible" is not an hour unless they name or
  clearly imply a boundary.

Limits
- You cannot see the user's calendar or book anything. Say so if asked.
- Do not invent causes. You can note that occupancy drops after 8pm; you
  cannot know why unless the data shows it."""


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def within_rate_limit() -> bool:
    """A crude per-process limit. One person cannot need more than this, and
    an unbounded loop of paid calls is the failure mode worth preventing."""
    cutoff = time.time() - 3600
    _recent[:] = [t for t in _recent if t > cutoff]
    if len(_recent) >= RATE_LIMIT_PER_HOUR:
        return False
    _recent.append(time.time())
    return True


MAX_TURNS = 12


def answer(messages, client=None) -> dict | None:
    """Continue a conversation. None when it could not be answered at all.

    `messages` is the exchange so far, oldest first. It is trimmed to the most
    recent turns: an unbounded history would grow the cost of every follow-up
    without improving the answer.
    """
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    messages = [
        {"role": m["role"], "content": str(m["content"]).strip()}
        for m in messages
        if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()
    ][-MAX_TURNS:]
    if not messages or messages[-1]["role"] != "user":
        return None
    try:
        client = client or anthropic.Anthropic()
        runner = client.beta.messages.tool_runner(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        used, final = [], None
        for message in runner:
            for block in message.content:
                if block.type == "tool_use":
                    used.append(block.name)
            final = message
    except Exception:
        return None

    if final is None:
        return None
    text = " ".join(b.text for b in final.content if b.type == "text").strip()
    return {"answer": text, "toolsUsed": used} if text else None
