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
    # WAL lets the web request read while the collector thread writes
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
    """Every reading at or after start, oldest first"""
    rows = connection.execute(
        "SELECT * FROM readings WHERE observed_at >= ? ORDER BY observed_at",
        (start.isoformat(),),
    ).fetchall()
    return [_to_reading(row) for row in rows]


def count_rows(connection: sqlite3.Connection) -> int:
    return connection.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
