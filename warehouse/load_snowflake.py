"""Load the dashboard's tables into Snowflake, the warehouse-native way.

Reads the local Postgres development database and copies `readings`, `history`
and `occupancy` into Snowflake through an internal stage and `COPY INTO`,
rather than inserting row by row -- which is the loading path a warehouse is
built around, and the one worth being able to describe.

`occupancy` is loaded as a table rather than recreated as a view, so the rule
about where imported history stops keeps its single definition in store.py.

Credentials come from the environment (warehouse/.env, gitignored). Nothing is
printed, and nothing is written into the repository.

    set -a; . warehouse/.env; set +a
    warehouse/.venv/bin/python warehouse/load_snowflake.py
"""

import csv
import gzip
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import snowflake.connector

import store

# One query per table, and the Snowflake column types to land them in.
TABLES = {
    "readings": (
        "SELECT observed_at, count, capacity FROM readings ORDER BY observed_at",
        "observed_at TIMESTAMP_TZ NOT NULL, count INTEGER NOT NULL, capacity INTEGER NOT NULL",
    ),
    "history": (
        "SELECT observed_at, count FROM history ORDER BY observed_at",
        "observed_at TIMESTAMP_TZ NOT NULL, count INTEGER NOT NULL",
    ),
    "occupancy": (
        "SELECT observed_at, count, capacity FROM occupancy ORDER BY observed_at",
        "observed_at TIMESTAMP_TZ NOT NULL, count INTEGER NOT NULL, capacity INTEGER NOT NULL",
    ),
}

FILE_FORMAT = (
    "TYPE = CSV FIELD_OPTIONALLY_ENCLOSED_BY = '\"' SKIP_HEADER = 1 "
    "TIMESTAMP_FORMAT = 'AUTO' COMPRESSION = GZIP"
)


def export(connection, table: str, query: str, directory: Path) -> tuple[Path, int]:
    """Write one table to a gzipped CSV. Timestamps go out as ISO 8601 with
    their offset, which Snowflake's AUTO format reads without being told."""
    path = directory / f"{table}.csv.gz"
    rows = connection.execute(query).fetchall()
    with gzip.open(path, "wt", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(rows[0].keys() if rows else [])
        for row in rows:
            writer.writerow([
                value.isoformat() if hasattr(value, "isoformat") else value
                for value in row.values()
            ])
    return path, len(rows)


def main() -> int:
    missing = [name for name in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD")
               if not os.environ.get(name)]
    if missing:
        print(f"missing from the environment: {', '.join(missing)}", file=sys.stderr)
        print("source warehouse/.env first", file=sys.stderr)
        return 1

    database = os.environ.get("SNOWFLAKE_DATABASE", "RSF")
    warehouse = os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")

    snowflake_connection = snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        role=os.environ.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
        warehouse=warehouse,
    )
    cursor = snowflake_connection.cursor()

    # Compute is sized and suspended separately from storage, which is the
    # part that has no Postgres equivalent. The smallest warehouse is ample
    # for 300k rows, and a minute of idle is enough to notice.
    cursor.execute(
        f"ALTER WAREHOUSE {warehouse} SET WAREHOUSE_SIZE = XSMALL"
        " AUTO_SUSPEND = 60 AUTO_RESUME = TRUE"
    )
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS {database}")
    cursor.execute(f"USE DATABASE {database}")
    cursor.execute("USE SCHEMA PUBLIC")

    with tempfile.TemporaryDirectory() as workspace, store.connection() as postgres:
        for table, (query, columns) in TABLES.items():
            path, rows = export(postgres, table, query, Path(workspace))
            cursor.execute(f"CREATE OR REPLACE TABLE {table} ({columns})")
            # The table's own stage: no bucket, no credentials, no cleanup.
            cursor.execute(f"PUT file://{path} @%{table} OVERWRITE = TRUE AUTO_COMPRESS = FALSE")
            cursor.execute(f"COPY INTO {table} FROM @%{table} FILE_FORMAT = ({FILE_FORMAT})")
            loaded = cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            status = "ok" if loaded == rows else "MISMATCH"
            print(f"{table:<10} {rows:>7,} exported  {loaded:>7,} in Snowflake  {status}")

    cursor.close()
    snowflake_connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
