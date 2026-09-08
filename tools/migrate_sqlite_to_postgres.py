"""Copy a SQLite readings.db into Postgres.

Run once per database. Idempotent on readings: it refuses to import if the
target already has rows, so a repeated run cannot silently double the history.

    DATABASE_URL=... python tools/migrate_sqlite_to_postgres.py path/to/readings.db
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store


def rows(connection, table):
    try:
        return connection.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.OperationalError:
        return []


def when(value):
    """SQLite kept ISO text; Postgres wants an aware datetime."""
    return datetime.fromisoformat(value) if value else None


def main(path):
    source = sqlite3.connect(path)
    source.row_factory = sqlite3.Row

    with store.connection() as target:
        existing = store.count_rows(target)
        if existing:
            print(f"refusing: target already has {existing} readings")
            return 1

        readings = rows(source, "readings")
        with target.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO readings (observed_at, count, capacity) VALUES (%s, %s, %s)",
                [(when(r["observed_at"]), r["count"], r["capacity"]) for r in readings],
            )
        print(f"readings: {len(readings)}")

        for row in rows(source, "google_tokens"):
            target.execute(
                """INSERT INTO google_tokens (email, refresh_token, linked_at)
                   VALUES (%s, %s, %s) ON CONFLICT (email) DO NOTHING""",
                (row["email"], row["refresh_token"], when(row["linked_at"])),
            )
        for row in rows(source, "app_state"):
            target.execute(
                """INSERT INTO app_state (key, value) VALUES (%s, %s)
                   ON CONFLICT (key) DO NOTHING""",
                (row["key"], row["value"]),
            )
        for row in rows(source, "predictions"):
            target.execute(
                """INSERT INTO predictions (made_at, for_date, window_start, window_end,
                       predicted_pct, basis_weeks, basis_spread, section)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (for_date, window_start) DO NOTHING""",
                (when(row["made_at"]), row["for_date"], when(row["window_start"]),
                 when(row["window_end"]), row["predicted_pct"], row["basis_weeks"],
                 row["basis_spread"], row["section"] if "section" in row.keys() else None),
            )
        target.commit()

        total = store.count_rows(target)
        span = target.execute(
            "SELECT MIN(observed_at) AS lo, MAX(observed_at) AS hi FROM readings"
        ).fetchone()
        print(f"tokens: {len(rows(source, 'google_tokens'))}"
              f" | app_state: {len(rows(source, 'app_state'))}")
        print(f"verified: {total} readings, {span['lo']} .. {span['hi']}")
        return 0 if total == len(readings) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
