import os
import sqlite3
from datetime import datetime, timezone
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
"""


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
    return connection


def save(connection: sqlite3.Connection, reading: Reading) -> None:
    """Append one reading"""
    connection.execute(
        "INSERT INTO readings (observed_at, count, capacity) VALUES (?, ?, ?)",
        (reading.observed_at.isoformat(), reading.count, reading.capacity),
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


def count_rows(connection: sqlite3.Connection) -> int:
    return connection.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
