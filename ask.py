"""Ad hoc questions about the occupancy history.

Only worth having for questions a fixed widget cannot answer in advance --
"was last Tuesday busier than usual", "Monday versus Friday evenings". The
dashboard already answers "how busy is it" and "when should I go" better than
a sentence could, and those deliberately stay where they are.

The model gets tools, not a database. Each tool is a vetted query with typed
arguments, so the worst a confused model can do is ask a sensible question of
the wrong slice -- it cannot write SQL, cannot reach another table, and cannot
write anything at all.
"""

import json
import os
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic
from anthropic import beta_tool

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


TOOLS = [data_range, occupancy_stats, typical_weekday_curve, my_sessions]

SYSTEM = """You answer questions about occupancy data for the UC Berkeley RSF \
weight rooms, using the tools provided.

- Occupancy is a percentage of a 150-person capacity. Readings are taken every
  few minutes by a doorway sensor that counts entries minus exits, so counts
  above capacity are real, not errors.
- Call data_range first whenever a question involves dates, so you do not
  reason about days that were never recorded.
- Answer in one or two short sentences with the actual numbers. No preamble.
- If the data cannot answer the question -- too few days, nothing recorded --
  say so plainly rather than estimating. Saying "there isn't enough data yet"
  is a good answer.
- You cannot see the user's calendar or change any setting. If asked, say so."""


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


def answer(question: str, client=None) -> dict | None:
    """Answer one question. None when it could not be answered at all."""
    if not question or not question.strip():
        return None
    try:
        client = client or anthropic.Anthropic()
        runner = client.beta.messages.tool_runner(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            tools=TOOLS,
            messages=[{"role": "user", "content": question.strip()}],
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
