import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from density import Reading

# In production the volume is mounted elsewhere, so the path is configurable
DB_PATH = Path(os.environ.get("RSF_DB_PATH", Path(__file__).parent / "readings.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    count INTEGER NOT NULL,
    capacity INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS readings_observed_at ON readings (observed_at);

-- What the agent predicted, as it was shown. Kept separate from feedback so
-- that "no answer" and "answered no" stay distinguishable: an unanswered
-- prediction simply has no feedback row.
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    made_at TEXT NOT NULL,
    for_date TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    predicted_pct REAL NOT NULL,
    -- the model's own confidence at the time, which cannot be recovered later
    basis_weeks INTEGER NOT NULL,
    basis_spread REAL,
    section TEXT
);
CREATE INDEX IF NOT EXISTS predictions_for_date ON predictions (for_date);
-- One row per suggested window per day, so re-rendering the page all day
-- does not produce dozens of duplicate predictions.
CREATE UNIQUE INDEX IF NOT EXISTS predictions_window
    ON predictions (for_date, window_start);

-- Small key/value store for things like the app calendar's id
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One booking per day, keyed by date so a second confirmation replaces the
-- first rather than double-booking.
CREATE TABLE IF NOT EXISTS bookings (
    for_date TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    predicted_pct REAL,
    prediction_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    prediction_id INTEGER PRIMARY KEY REFERENCES predictions (id),
    answered_at TEXT NOT NULL,
    went INTEGER NOT NULL
);
"""


def _migrate(connection: sqlite3.Connection) -> None:
    """Add columns that post-date a table's creation.

    CREATE TABLE IF NOT EXISTS silently does nothing for an existing table, so
    a new column never appears without this.
    """
    for table, column, kind in (
        ("bookings", "prediction_id", "INTEGER"),
        ("predictions", "section", "TEXT"),
    ):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns and column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            connection.commit()


def connect(db_path=DB_PATH) -> sqlite3.Connection:
    """Open the database, creating the table on first use"""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    # WAL: collect.py can run as its own process (cron) against the same file
    # the web app is serving from. SQLite locks per connection, not per
    # process, so this matters in deployment too -- the in-process collector
    # and each request open separate connections. Without WAL a writer takes
    # an EXCLUSIVE lock and readers get SQLITE_BUSY.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    _migrate(connection)
    return connection


def save(connection: sqlite3.Connection, reading: Reading) -> None:
    """Append one reading, normalised to UTC.

    Every query bound is converted to UTC before comparison, so a row stored
    with a local offset would sort and filter against them incorrectly --
    timestamps are compared as text. Readings from the collector are already
    UTC, which is why this was invisible until a local-time Reading was saved
    directly.
    """
    connection.execute(
        "INSERT INTO readings (observed_at, count, capacity) VALUES (?, ?, ?)",
        (
            reading.observed_at.astimezone(timezone.utc).isoformat(),
            reading.count,
            reading.capacity,
        ),
    )
    connection.commit()


def _to_reading(row: sqlite3.Row) -> Reading:
    return Reading(
        count=row["count"],
        capacity=row["capacity"],
        observed_at=datetime.fromisoformat(row["observed_at"]),
    )


def latest(connection: sqlite3.Connection) -> Reading | None:
    """Most recent stored reading, or None if nothing has been collected yet"""
    row = connection.execute(
        "SELECT * FROM readings ORDER BY observed_at DESC LIMIT 1"
    ).fetchone()
    return _to_reading(row) if row else None


def since(connection: sqlite3.Connection, start: datetime) -> list[Reading]:
    """Every reading at or after start, oldest first.

    Timestamps are compared as text, so the bound must be normalised to UTC
    first. A local-time bound like ...T00:00:00-07:00 would compare its "00"
    hour against stored "+00:00" hours and silently pull in the prior evening.
    """
    rows = connection.execute(
        "SELECT * FROM readings WHERE observed_at >= ? ORDER BY observed_at",
        (start.astimezone(timezone.utc).isoformat(),),
    ).fetchall()
    return [_to_reading(row) for row in rows]


def between(connection: sqlite3.Connection, start: datetime, end: datetime) -> list[Reading]:
    """Readings in [start, end), oldest first. Bounds are normalised to UTC
    for the same reason as since()."""
    rows = connection.execute(
        "SELECT * FROM readings WHERE observed_at >= ? AND observed_at < ? ORDER BY observed_at",
        (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()),
    ).fetchall()
    return [_to_reading(row) for row in rows]


def earliest(connection: sqlite3.Connection) -> Reading | None:
    """The very first reading ever recorded, for bounding date navigation"""
    row = connection.execute(
        "SELECT * FROM readings ORDER BY observed_at LIMIT 1"
    ).fetchone()
    return _to_reading(row) if row else None


def all_readings(connection: sqlite3.Connection) -> list[Reading]:
    """Every reading, oldest first.

    Weekday filtering happens in Python because the local weekday of a UTC
    timestamp depends on DST, which SQLite cannot work out. Fine at a few
    hundred rows per day; revisit if this ever gets slow.
    """
    rows = connection.execute("SELECT * FROM readings ORDER BY observed_at").fetchall()
    return [_to_reading(row) for row in rows]


def log_prediction(
    connection: sqlite3.Connection,
    for_date: date,
    window_start: datetime,
    window_end: datetime,
    predicted_pct: float,
    basis_weeks: int,
    basis_spread: float | None = None,
    section: str | None = None,
) -> int:
    """Record a window as shown, once. Returns the row id either way.

    Idempotent on (for_date, window_start): the dashboard re-renders on every
    load and every 60s poll, and each of those is the same suggestion, not a
    new one.

    predicted_pct is what the user actually saw, not something to recompute
    later -- the model will change, and recomputing would score today's model
    against decisions it never made. The section is stored rather than derived
    on read, because deriving it needs DST-aware local time that SQLite cannot
    do; storing it lets the tally be a plain GROUP BY.
    """
    connection.execute(
        """INSERT OR IGNORE INTO predictions
           (made_at, for_date, window_start, window_end, predicted_pct,
            basis_weeks, basis_spread, section)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            for_date.isoformat(),
            window_start.astimezone(timezone.utc).isoformat(),
            window_end.astimezone(timezone.utc).isoformat(),
            predicted_pct, basis_weeks, basis_spread, section,
        ),
    )
    connection.commit()
    row = connection.execute(
        "SELECT id FROM predictions WHERE for_date = ? AND window_start = ?",
        (for_date.isoformat(), window_start.astimezone(timezone.utc).isoformat()),
    ).fetchone()
    return row[0]


def feedback_for(connection: sqlite3.Connection, prediction_id: int):
    row = connection.execute(
        "SELECT went FROM feedback WHERE prediction_id = ?", (prediction_id,)
    ).fetchone()
    return None if row is None else bool(row[0])


def record_feedback(
    connection: sqlite3.Connection,
    prediction_id: int,
    went: bool,
    answered_at: datetime | None = None,
) -> None:
    """Answer "did you go?" for one prediction. Re-answering replaces."""
    answered_at = answered_at or datetime.now(timezone.utc)
    connection.execute(
        """INSERT INTO feedback (prediction_id, answered_at, went)
           VALUES (?, ?, ?)
           ON CONFLICT (prediction_id) DO UPDATE SET
               answered_at = excluded.answered_at, went = excluded.went""",
        (prediction_id, answered_at.astimezone(timezone.utc).isoformat(), int(went)),
    )
    connection.commit()


def predictions_on(connection: sqlite3.Connection, for_date: date) -> list[dict]:
    """Predictions made for one local date, each with its answer or None."""
    rows = connection.execute(
        """SELECT p.*, f.went, f.answered_at
           FROM predictions p LEFT JOIN feedback f ON f.prediction_id = p.id
           WHERE p.for_date = ? ORDER BY p.window_start""",
        (for_date.isoformat(),),
    ).fetchall()
    return [
        {
            **{k: row[k] for k in row.keys() if k not in ("went", "answered_at")},
            # None means unanswered, which is not the same as answered "no"
            "went": None if row["went"] is None else bool(row["went"]),
            "answered_at": row["answered_at"],
        }
        for row in rows
    ]


def get_state(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_state(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """INSERT INTO app_state (key, value) VALUES (?, ?)
           ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
        (key, value),
    )
    connection.commit()


def save_booking(connection: sqlite3.Connection, for_date: date, event_id: str,
                 starts_at: datetime, ends_at: datetime, predicted_pct=None,
                 prediction_id=None) -> None:
    connection.execute(
        """INSERT INTO bookings
           (for_date, event_id, starts_at, ends_at, predicted_pct, prediction_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (for_date) DO UPDATE SET
               event_id = excluded.event_id, starts_at = excluded.starts_at,
               ends_at = excluded.ends_at, predicted_pct = excluded.predicted_pct,
               prediction_id = excluded.prediction_id, created_at = excluded.created_at""",
        (
            for_date.isoformat(), event_id,
            starts_at.astimezone(timezone.utc).isoformat(),
            ends_at.astimezone(timezone.utc).isoformat(),
            predicted_pct, prediction_id, datetime.now(timezone.utc).isoformat(),
        ),
    )
    connection.commit()


def booking_on(connection: sqlite3.Connection, for_date: date) -> dict | None:
    row = connection.execute(
        "SELECT * FROM bookings WHERE for_date = ?", (for_date.isoformat(),)
    ).fetchone()
    return dict(row) if row else None


def delete_booking(connection: sqlite3.Connection, for_date: date) -> None:
    connection.execute("DELETE FROM bookings WHERE for_date = ?", (for_date.isoformat(),))
    connection.commit()


def section_outcomes(connection: sqlite3.Connection) -> dict[str, dict]:
    """How suggestions in each part of the day have fared, aggregated in SQL.

    Three tables' worth of state -- what was suggested, what was confirmed,
    what was answered -- collapse into one grouped join. Counting rows across
    joined tables is what a database is for; doing it in Python meant loading
    every row to increment counters.

    Grouping by the stored section rather than deriving it here is deliberate:
    the section depends on DST-aware local time, which SQLite cannot compute,
    so it is worked out in Python at write time and simply grouped here.
    """
    rows = connection.execute(
        """SELECT p.section,
                  COUNT(*)                          AS shown,
                  COUNT(b.event_id)                 AS booked,
                  COUNT(f.went)                     AS answered,
                  COALESCE(SUM(f.went), 0)          AS attended,
                  COALESCE(SUM(1 - f.went), 0)      AS skipped
           FROM predictions p
           LEFT JOIN bookings b ON b.prediction_id = p.id
           LEFT JOIN feedback f ON f.prediction_id = p.id
           WHERE p.section IS NOT NULL
           GROUP BY p.section"""
    ).fetchall()
    return {row["section"]: {k: row[k] for k in row.keys() if k != "section"} for row in rows}


def day_statistics(connection: sqlite3.Connection, start: datetime, end: datetime):
    """Peak, average and sample count over a time range, computed in SQL.

    The bounds arrive already converted to UTC, so nothing here depends on a
    timezone -- which is exactly why this aggregation can live in the database
    while the weekday curve cannot.
    """
    row = connection.execute(
        """WITH scoped AS (
               SELECT observed_at, "count", capacity,
                      CAST("count" AS REAL) / capacity AS pct
               FROM readings
               WHERE observed_at >= ? AND observed_at < ? AND capacity > 0
           ),
           ranked AS (
               SELECT *, ROW_NUMBER() OVER (ORDER BY "count" DESC, observed_at) AS rank
               FROM scoped
           )
           SELECT (SELECT "count"      FROM ranked WHERE rank = 1) AS peak_count,
                  (SELECT capacity     FROM ranked WHERE rank = 1) AS peak_capacity,
                  (SELECT observed_at  FROM ranked WHERE rank = 1) AS peak_at,
                  AVG(pct)                                         AS average_pct,
                  COUNT(*)                                         AS samples
           FROM scoped""",
        (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()),
    ).fetchone()
    if not row or not row["samples"]:
        return None
    return {
        "peak_count": row["peak_count"],
        "peak_capacity": row["peak_capacity"],
        "peak_at": datetime.fromisoformat(row["peak_at"]),
        "average_pct": row["average_pct"],
        "samples": row["samples"],
    }


def prediction_outcomes(connection: sqlite3.Connection) -> list[dict]:
    """Every logged suggestion with what became of it.

    booked is whether it was confirmed; went is None when never answered,
    which is a different signal from answering no.
    """
    rows = connection.execute(
        """SELECT p.window_start, p.window_end, p.predicted_pct,
                  b.event_id IS NOT NULL AS booked, f.went
           FROM predictions p
           LEFT JOIN bookings b ON b.prediction_id = p.id
           LEFT JOIN feedback f ON f.prediction_id = p.id
           ORDER BY p.window_start"""
    ).fetchall()
    return [
        {
            "window_start": row["window_start"],
            "window_end": row["window_end"],
            "predicted_pct": row["predicted_pct"],
            "booked": bool(row["booked"]),
            "went": None if row["went"] is None else bool(row["went"]),
        }
        for row in rows
    ]


def count_rows(connection: sqlite3.Connection) -> int:
    return connection.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
