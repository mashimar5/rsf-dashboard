"""Postgres storage.

Timestamps are `timestamptz`, so the driver hands back aware datetimes and
comparisons happen on instants rather than on text. That removes an entire
class of bug this project hit four separate times under SQLite, where an ISO
string in one offset was compared against an ISO string in another.

Connections come from a pool: unlike a SQLite file handle, a Postgres
connection is a network socket and a server-side process, so opening one per
call would be wasteful and would eventually exhaust the server's limit.
"""

import atexit
import csv
import os
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from density import Reading

def database_url() -> str:
    """Read at connect time, not import time, so tests can redirect it."""
    return os.environ.get("DATABASE_URL", "postgresql:///rsf_dev")

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observed_at TIMESTAMPTZ NOT NULL,
    count       INTEGER NOT NULL,
    capacity    INTEGER NOT NULL
);
-- One sample per instant. Makes duplicate collection impossible rather than
-- merely unlikely, and lets the SQLite import be re-run safely.
CREATE UNIQUE INDEX IF NOT EXISTS readings_observed_at ON readings (observed_at);

CREATE TABLE IF NOT EXISTS predictions (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    made_at       TIMESTAMPTZ NOT NULL,
    for_date      DATE NOT NULL,
    window_start  TIMESTAMPTZ NOT NULL,
    window_end    TIMESTAMPTZ NOT NULL,
    predicted_pct DOUBLE PRECISION NOT NULL,
    basis_weeks   INTEGER NOT NULL,
    basis_spread  DOUBLE PRECISION,
    section       TEXT
);
CREATE INDEX IF NOT EXISTS predictions_for_date ON predictions (for_date);
-- One row per suggested window per day: the page re-renders on every load and
-- every 60s poll, and each of those is the same suggestion, not a new one.
CREATE UNIQUE INDEX IF NOT EXISTS predictions_window
    ON predictions (for_date, window_start);

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    for_date      DATE PRIMARY KEY,
    event_id      TEXT NOT NULL,
    starts_at     TIMESTAMPTZ NOT NULL,
    ends_at       TIMESTAMPTZ NOT NULL,
    predicted_pct DOUBLE PRECISION,
    prediction_id BIGINT REFERENCES predictions (id),
    created_at    TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    prediction_id BIGINT PRIMARY KEY REFERENCES predictions (id),
    answered_at   TIMESTAMPTZ NOT NULL,
    went          BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS google_tokens (
    email         TEXT PRIMARY KEY,
    refresh_token TEXT NOT NULL,
    linked_at     TIMESTAMPTZ NOT NULL
);

-- The sensor vendor's own ten-minute history, imported once and cleaned on
-- the way in (tools/backfill_history.py). Kept apart from `readings` so the
-- collector, the day view and the health checks only ever see live data;
-- analytics opt in through the `occupancy` view.
CREATE TABLE IF NOT EXISTS history (
    observed_at TIMESTAMPTZ PRIMARY KEY,
    count       INTEGER NOT NULL
);

-- One row per local date, expanded from data/academic_calendar.csv whenever
-- the pool opens.
CREATE TABLE IF NOT EXISTS calendar_days (
    day   DATE PRIMARY KEY,
    kind  TEXT NOT NULL,
    label TEXT NOT NULL
);

-- What the curve, the backtest and the chat learn from. History only fills
-- the time before live collection began, so the two never overlap, and it
-- takes today's capacity: the room did not change size, and the cap posted
-- in earlier years (140) would make percentages incomparable across them.
CREATE OR REPLACE VIEW occupancy AS
    SELECT observed_at, count, capacity FROM readings
    UNION ALL
    SELECT observed_at, count, 150 FROM history
    WHERE observed_at < (SELECT COALESCE(MIN(observed_at), 'infinity') FROM readings);
"""

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    """The process-wide connection pool, opened on first use."""
    global _pool
    if _pool is None:
        opened = ConnectionPool(
            database_url(), min_size=1, max_size=4, kwargs={"row_factory": dict_row}
        )
        try:
            with opened.connection() as connection:
                connection.execute(SCHEMA)
                sync_calendar(connection)
        except Exception:
            # leave no half-initialised pool behind for the next call to reuse
            opened.close()
            raise
        _pool = opened
    return _pool


@contextmanager
def connection():
    """A pooled connection, returned to the pool on exit."""
    with pool().connection() as conn:
        yield conn


atexit.register(lambda: reset_pool())


def reset_pool() -> None:
    """Drop the pool so a later call reconnects. Used by tests."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _to_reading(row) -> Reading:
    return Reading(count=row["count"], capacity=row["capacity"],
                   observed_at=row["observed_at"])


def save(conn, reading: Reading) -> None:
    """Append one reading."""
    conn.execute(
        "INSERT INTO readings (observed_at, count, capacity) VALUES (%s, %s, %s)",
        (reading.observed_at, reading.count, reading.capacity),
    )
    conn.commit()


def latest(conn) -> Reading | None:
    row = conn.execute(
        "SELECT * FROM readings ORDER BY observed_at DESC LIMIT 1"
    ).fetchone()
    return _to_reading(row) if row else None


def earliest(conn) -> Reading | None:
    """The very first reading ever recorded, for bounding date navigation."""
    row = conn.execute("SELECT * FROM readings ORDER BY observed_at LIMIT 1").fetchone()
    return _to_reading(row) if row else None


def since(conn, start: datetime) -> list[Reading]:
    rows = conn.execute(
        "SELECT * FROM readings WHERE observed_at >= %s ORDER BY observed_at", (start,)
    ).fetchall()
    return [_to_reading(row) for row in rows]


def between(conn, start: datetime, end: datetime) -> list[Reading]:
    """Readings in [start, end), oldest first."""
    rows = conn.execute(
        "SELECT * FROM readings WHERE observed_at >= %s AND observed_at < %s"
        " ORDER BY observed_at",
        (start, end),
    ).fetchall()
    return [_to_reading(row) for row in rows]


def all_readings(conn) -> list[Reading]:
    rows = conn.execute("SELECT * FROM readings ORDER BY observed_at").fetchall()
    return [_to_reading(row) for row in rows]


def count_rows(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM readings").fetchone()["n"]


def occupancy_between(conn, start: datetime, end: datetime) -> list[Reading]:
    """Live readings and imported history in [start, end), oldest first."""
    rows = conn.execute(
        "SELECT observed_at, count, capacity FROM occupancy"
        " WHERE observed_at >= %s AND observed_at < %s ORDER BY observed_at",
        (start, end),
    ).fetchall()
    return [_to_reading(row) for row in rows]


CALENDAR_PATH = Path(__file__).resolve().parent / "data" / "academic_calendar.csv"

# When periods overlap, the more specific one names the day: a holiday in term
# is a holiday, and review week or finals are not instruction. An overlap
# between two different kinds of equal rank can only be a transcription error.
PERIOD_RANK = {"holiday": 3, "break": 2, "rrr": 2, "finals": 2,
               "instruction": 1, "summer": 1}


def calendar_days_from(path: Path = CALENDAR_PATH) -> dict[date, tuple[str, str]]:
    """Expand the academic calendar's periods into one (kind, label) per day."""
    days: dict[date, tuple[str, str]] = {}
    with open(path, newline="") as handle:
        for line, row in enumerate(csv.DictReader(handle), start=2):
            kind, label = row["kind"], row["label"]
            if kind not in PERIOD_RANK:
                raise ValueError(f"{path.name} line {line}: unknown kind {kind!r}")
            start, end = date.fromisoformat(row["start"]), date.fromisoformat(row["end"])
            if end < start:
                raise ValueError(f"{path.name} line {line}: ends before it starts")
            day = start
            while day <= end:
                held = days.get(day)
                if held is None or PERIOD_RANK[kind] > PERIOD_RANK[held[0]]:
                    days[day] = (kind, label)
                elif PERIOD_RANK[kind] == PERIOD_RANK[held[0]] and kind != held[0]:
                    raise ValueError(
                        f"{path.name} line {line}: {day} is both {held[0]} and {kind}")
                day += timedelta(days=1)
    return days


def sync_calendar(conn, path: Path = CALENDAR_PATH) -> int:
    """Replace calendar_days with the checked-in calendar.

    Runs when the pool opens, so a calendar edit takes effect with the deploy
    that ships it rather than needing a separate step someone could forget.
    """
    days = calendar_days_from(path)
    conn.execute("DELETE FROM calendar_days")
    with conn.cursor() as cursor:
        with cursor.copy("COPY calendar_days (day, kind, label) FROM STDIN") as copy:
            for day, (kind, label) in sorted(days.items()):
                copy.write_row((day, kind, label))
    return len(days)


def period_of(conn, day: date) -> str | None:
    """The kind of academic period a local date falls in, if the calendar knows."""
    row = conn.execute("SELECT kind FROM calendar_days WHERE day = %s", (day,)).fetchone()
    return row["kind"] if row else None


def weekday_bands(conn, target: date, zone: str, bucket_minutes: int,
                  window_instances: int, before: datetime | None = None,
                  match_period: bool = True):
    """The typical-weekday curve, computed in the database.

    This is the computation SQLite could not do. `AT TIME ZONE` is DST-aware,
    so the local weekday and the local time-of-day bucket are correct across
    the spring and autumn transitions; `percentile_cont` gives a real median.

    Only the most recent `window_instances` days are used -- without a limit
    every past Monday would be weighted equally forever, so the curve would
    degrade as data accumulated, blending quiet August with busy October.

    Those days come from the same kind of academic period as the target when
    the calendar knows it, so a Monday in term is compared with Mondays in
    term rather than with the summer ones that happen to be most recent.
    `match_period=False` restores plain recency, for backtesting the
    difference; a target outside the calendar gets plain recency anyway.

    Each day is averaged per bucket before the median is taken, so every
    instance gets one vote however often it was sampled: live collection runs
    every four minutes and imported history every ten.

    Returns (bands, instance_count, mean_spread) where bands maps a bucket
    index to {median, low, high}.
    """
    rows = conn.execute(
        """
        WITH target AS (
            SELECT kind FROM calendar_days
            WHERE day = %(target)s AND %(match_period)s
        ),
        local AS (
            SELECT observed_at AT TIME ZONE %(zone)s AS local_at,
                   count::float / capacity           AS pct
            FROM occupancy
            WHERE capacity > 0
              AND (%(before)s::timestamptz IS NULL OR observed_at < %(before)s)
        ),
        -- one value per instance and bucket, whatever the sampling rate
        matching AS (
            SELECT local_at::date                            AS day,
                   (EXTRACT(HOUR FROM local_at) * 60
                    + EXTRACT(MINUTE FROM local_at))::int
                       / %(bucket)s                          AS bucket,
                   AVG(pct)                                  AS pct
            FROM local
            WHERE EXTRACT(ISODOW FROM local_at) = %(isodow)s
              AND local_at::date <> %(target)s
            GROUP BY 1, 2
        ),
        -- the window applies to days, not rows, so a day with patchy
        -- collection still counts as one instance
        recent AS (
            SELECT m.day FROM matching m
            LEFT JOIN calendar_days c ON c.day = m.day
            WHERE NOT EXISTS (SELECT 1 FROM target)
               OR c.kind = (SELECT kind FROM target)
            GROUP BY m.day ORDER BY m.day DESC LIMIT %(window)s
        )
        SELECT m.bucket,
               percentile_cont(0.5)  WITHIN GROUP (ORDER BY m.pct) AS median,
               percentile_cont(0.25) WITHIN GROUP (ORDER BY m.pct) AS q1,
               percentile_cont(0.75) WITHIN GROUP (ORDER BY m.pct) AS q3,
               MIN(m.pct) AS low,
               MAX(m.pct) AS high,
               COUNT(DISTINCT m.day) AS days,
               (SELECT COUNT(*) FROM recent) AS instances
        FROM matching m
        JOIN recent r ON r.day = m.day
        GROUP BY m.bucket
        ORDER BY m.bucket
        """,
        {
            "zone": zone, "bucket": bucket_minutes, "target": target,
            "isodow": target.isoweekday(), "window": window_instances,
            "before": before, "match_period": match_period,
        },
    ).fetchall()

    if not rows:
        return {}, 0, None
    # q1/q3 come back alongside the range so the caller can pick a dispersion
    # measure by sample size: a range's expected value grows with n, so it is
    # not comparable across cohorts, while an IQR is.
    bands = {
        row["bucket"]: {
            "median": row["median"], "low": row["low"], "high": row["high"],
            "q1": row["q1"], "q3": row["q3"], "n": row["days"],
        }
        for row in rows
    }
    import evaluate   # imported lazily to avoid a cycle at import time

    return bands, rows[0]["instances"], evaluate.spread_of_bands(bands)


def day_statistics(conn, start: datetime, end: datetime):
    """Peak, average and sample count over a time range."""
    row = conn.execute(
        """
        WITH scoped AS (
            SELECT observed_at, count, capacity, count::float / capacity AS pct
            FROM readings
            WHERE observed_at >= %s AND observed_at < %s AND capacity > 0
        ),
        ranked AS (
            SELECT *, ROW_NUMBER() OVER (ORDER BY count DESC, observed_at) AS rank
            FROM scoped
        )
        SELECT (SELECT count      FROM ranked WHERE rank = 1) AS peak_count,
               (SELECT capacity   FROM ranked WHERE rank = 1) AS peak_capacity,
               (SELECT observed_at FROM ranked WHERE rank = 1) AS peak_at,
               AVG(pct) AS average_pct,
               COUNT(*) AS samples
        FROM scoped
        """,
        (start, end),
    ).fetchone()
    if not row or not row["samples"]:
        return None
    return dict(row)


def log_prediction(conn, for_date: date, window_start: datetime, window_end: datetime,
                   predicted_pct: float, basis_weeks: int,
                   basis_spread: float | None = None, section: str | None = None) -> int:
    """Record a window as shown, once. Returns the row id either way.

    predicted_pct is what the user actually saw, not something to recompute
    later -- the model will change, and recomputing would score today's model
    against decisions it never made.
    """
    row = conn.execute(
        """INSERT INTO predictions
           (made_at, for_date, window_start, window_end, predicted_pct,
            basis_weeks, basis_spread, section)
           VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (for_date, window_start) DO UPDATE
               SET for_date = EXCLUDED.for_date
           RETURNING id""",
        (for_date, window_start, window_end, predicted_pct,
         basis_weeks, basis_spread, section),
    ).fetchone()
    conn.commit()
    return row["id"]


def feedback_for(conn, prediction_id: int):
    row = conn.execute(
        "SELECT went FROM feedback WHERE prediction_id = %s", (prediction_id,)
    ).fetchone()
    return None if row is None else row["went"]


def record_feedback(conn, prediction_id: int, went: bool) -> None:
    """Answer "did you go?" for one prediction. Re-answering replaces."""
    conn.execute(
        """INSERT INTO feedback (prediction_id, answered_at, went)
           VALUES (%s, NOW(), %s)
           ON CONFLICT (prediction_id) DO UPDATE
               SET answered_at = EXCLUDED.answered_at, went = EXCLUDED.went""",
        (prediction_id, went),
    )
    conn.commit()


def section_outcomes(conn) -> dict[str, dict]:
    """How suggestions in each part of the day have fared.

    The section is stored at write time rather than derived here, so this is a
    plain GROUP BY across the three tables that hold what was suggested, what
    was confirmed, and what was answered.
    """
    rows = conn.execute(
        """SELECT p.section,
                  COUNT(*)                                        AS shown,
                  COUNT(b.event_id)                               AS booked,
                  COUNT(f.went)                                   AS answered,
                  COUNT(*) FILTER (WHERE f.went)                   AS attended,
                  COUNT(*) FILTER (WHERE f.went IS FALSE)          AS skipped
           FROM predictions p
           LEFT JOIN bookings b ON b.prediction_id = p.id
           LEFT JOIN feedback f ON f.prediction_id = p.id
           WHERE p.section IS NOT NULL
           GROUP BY p.section"""
    ).fetchall()
    return {
        row["section"]: {k: v for k, v in row.items() if k != "section"} for row in rows
    }


def prediction_outcomes(conn) -> list[dict]:
    """Every logged suggestion with what became of it."""
    rows = conn.execute(
        """SELECT p.window_start, p.window_end, p.predicted_pct, p.section,
                  b.event_id IS NOT NULL AS booked, f.went
           FROM predictions p
           LEFT JOIN bookings b ON b.prediction_id = p.id
           LEFT JOIN feedback f ON f.prediction_id = p.id
           ORDER BY p.window_start"""
    ).fetchall()
    return [dict(row) for row in rows]


def predictions_on(conn, for_date: date) -> list[dict]:
    """Predictions made for one local date, each with its answer or None."""
    rows = conn.execute(
        """SELECT p.*, f.went, f.answered_at
           FROM predictions p LEFT JOIN feedback f ON f.prediction_id = p.id
           WHERE p.for_date = %s ORDER BY p.window_start""",
        (for_date,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_state(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO app_state (key, value) VALUES (%s, %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
        (key, value),
    )
    conn.commit()


def save_booking(conn, for_date: date, event_id: str, starts_at: datetime,
                 ends_at: datetime, predicted_pct=None, prediction_id=None) -> None:
    conn.execute(
        """INSERT INTO bookings
           (for_date, event_id, starts_at, ends_at, predicted_pct, prediction_id, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, NOW())
           ON CONFLICT (for_date) DO UPDATE SET
               event_id = EXCLUDED.event_id, starts_at = EXCLUDED.starts_at,
               ends_at = EXCLUDED.ends_at, predicted_pct = EXCLUDED.predicted_pct,
               prediction_id = EXCLUDED.prediction_id, created_at = EXCLUDED.created_at""",
        (for_date, event_id, starts_at, ends_at, predicted_pct, prediction_id),
    )
    conn.commit()


def booking_on(conn, for_date: date) -> dict | None:
    row = conn.execute(
        "SELECT * FROM bookings WHERE for_date = %s", (for_date,)
    ).fetchone()
    return dict(row) if row else None


def delete_booking(conn, for_date: date) -> None:
    conn.execute("DELETE FROM bookings WHERE for_date = %s", (for_date,))
    conn.commit()
