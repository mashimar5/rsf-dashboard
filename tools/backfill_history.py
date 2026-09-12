"""Import the RSF weight room's occupancy history from before live collection.

Density, the company behind the doorway sensors, keeps every count. Anthony
Ozerov has pulled them at ten-minute intervals since November 2020 and
publishes the file on his site under a CC0 public-domain mark:

    https://aozerov.com/berkeley/weightroom/data.txt
    columns: interval start (UTC), count, interval min, interval max

It is the same sensor this app reads live: over the days both cover, the
counts agree to a median of two people.

Only rows that can be trusted are stored, and nothing is corrected -- a
stretch that cannot be trusted is left out rather than repaired:

- Before 2021-09-06 the gym ran under pandemic restrictions. The weekly
  median daily peak was 75 people the week before and 153 the week of.
- A count of 10 or more that stays exactly the same for an hour is a stalled
  feed, not a crowd. Those rows go; the rest of the day stays.
- A day with any count above 180 (120% of capacity), or with more than 20
  people still counted at 00:30 after closing, drifted. The error builds over
  the day, so the whole day goes.
- Nothing at or after the first live reading is imported; live data wins.

Only `count` is used. The min/max columns contradict it in a fifth to a third
of rows, and live readings from the same sensor side with `count`.

Safe to re-run: rows are keyed by instant.

    DATABASE_URL=... python tools/backfill_history.py [--dry-run] [--replace] [source]

`source` is a URL or a local path, and defaults to the published file.
"""

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store

SOURCE_URL = "https://aozerov.com/berkeley/weightroom/data.txt"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
STEP = timedelta(minutes=10)

FIRST_DAY = date(2021, 9, 6)
CEILING = 180                # 120% of the 150-person capacity
LEFTOVER_AT = time(0, 30)    # after every closing time, before the nightly reset
LEFTOVER_MAX = 20
FROZEN_MIN_COUNT = 10        # an empty gym legitimately reads 0 for hours
FROZEN_MIN_ROWS = 6          # six ten-minute intervals: one value held for an hour


@dataclass(frozen=True)
class Row:
    observed_at: datetime
    count: int


@dataclass
class Report:
    kept: list[Row] = field(default_factory=list)
    before_cutoff: int = 0
    after_live: int = 0
    excluded_days: dict[date, str] = field(default_factory=dict)
    frozen: list[tuple[datetime, datetime, int]] = field(default_factory=list)
    frozen_rows: int = 0


def parse(lines) -> list[Row]:
    """Read the published CSV, refusing anything that does not look like it."""
    rows = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        fields = line.strip().split(",")
        if len(fields) != 4:
            raise ValueError(f"row {number}: expected 4 columns, found {len(fields)}")
        try:
            observed_at = datetime.fromisoformat(fields[0])
            count = int(fields[1])
        except ValueError as error:
            raise ValueError(f"row {number}: {error}") from None
        if observed_at.tzinfo is None:
            raise ValueError(f"row {number}: {fields[0]} has no UTC offset")
        observed_at = observed_at.astimezone(timezone.utc)
        if observed_at.minute % 10 or observed_at.second or observed_at.microsecond:
            raise ValueError(f"row {number}: {fields[0]} is not on the ten-minute grid")
        if count < 0:
            raise ValueError(f"row {number}: negative count {count}")
        rows.append(Row(observed_at, count))
    return rows


def local_day(row: Row) -> date:
    return row.observed_at.astimezone(LOCAL_TZ).date()


def clean(rows, first_live: datetime | None = None) -> Report:
    """Decide what to keep. Pure, so every rule is testable without a database."""
    rows = sorted(rows, key=lambda row: row.observed_at)
    report = Report()

    scoped = []
    for row in rows:
        if local_day(row) < FIRST_DAY:
            report.before_cutoff += 1
        elif first_live is not None and row.observed_at >= first_live:
            report.after_live += 1
        else:
            scoped.append(row)

    # Whole days that drifted
    count_at = {row.observed_at: row.count for row in rows}
    peaks: dict[date, int] = defaultdict(int)
    for row in scoped:
        peaks[local_day(row)] = max(peaks[local_day(row)], row.count)
    for day, peak in sorted(peaks.items()):
        after_closing = datetime.combine(day + timedelta(days=1), LEFTOVER_AT, LOCAL_TZ)
        leftover = count_at.get(after_closing.astimezone(timezone.utc))
        if peak > CEILING:
            report.excluded_days[day] = f"a count of {peak}, above {CEILING}"
        elif leftover is not None and leftover > LEFTOVER_MAX:
            report.excluded_days[day] = f"{leftover} people still counted at 00:30"

    # Stretches where the feed stalled
    stalled: set[datetime] = set()
    run: list[Row] = []
    for row in [*scoped, None]:
        if (row is not None and run and row.count == run[-1].count
                and row.observed_at - run[-1].observed_at == STEP):
            run.append(row)
            continue
        if len(run) >= FROZEN_MIN_ROWS and run[0].count >= FROZEN_MIN_COUNT:
            report.frozen.append((run[0].observed_at, run[-1].observed_at, run[0].count))
            stalled.update(r.observed_at for r in run)
        run = [row] if row is not None else []

    for row in scoped:
        if local_day(row) in report.excluded_days:
            continue
        if row.observed_at in stalled:
            report.frozen_rows += 1
        else:
            report.kept.append(row)
    return report


def load(conn, rows, replace: bool = False) -> int:
    """Insert rows, skipping instants already present. Returns rows inserted."""
    with conn.cursor() as cursor:
        if replace:
            cursor.execute("TRUNCATE history")
        cursor.execute(
            "CREATE TEMP TABLE history_import (observed_at TIMESTAMPTZ, count INTEGER)"
            " ON COMMIT DROP"
        )
        with cursor.copy("COPY history_import (observed_at, count) FROM STDIN") as copy:
            for row in rows:
                copy.write_row((row.observed_at, row.count))
        cursor.execute(
            "INSERT INTO history (observed_at, count)"
            " SELECT observed_at, count FROM history_import"
            " ON CONFLICT (observed_at) DO NOTHING"
        )
        inserted = cursor.rowcount
    conn.commit()
    return inserted


def read_source(source: str) -> list[str]:
    if source.startswith(("http://", "https://")):
        response = requests.get(source, timeout=120)
        response.raise_for_status()
        return response.text.splitlines()
    return Path(source).read_text().splitlines()


def describe(report: Report, read: int, verbose: bool = False) -> str:
    over = sum("above" in why for why in report.excluded_days.values())
    frozen_days = {first.astimezone(LOCAL_TZ).date() for first, _, _ in report.frozen}
    lines = [
        f"rows read                {read:>9,}",
        f"before {FIRST_DAY}        {report.before_cutoff:>9,}",
        f"at or after live start   {report.after_live:>9,}",
        f"days excluded            {len(report.excluded_days):>9,}"
        f"   ({over} above {CEILING}, {len(report.excluded_days) - over} still counting at 00:30)",
        f"frozen rows dropped      {report.frozen_rows:>9,}"
        f"   ({len(report.frozen)} stretches, on {len(frozen_days - set(report.excluded_days))} kept days)",
        f"rows kept                {len(report.kept):>9,}",
    ]
    if report.kept:
        lines.append(f"kept from {report.kept[0].observed_at:%Y-%m-%d %H:%M}Z"
                     f" to {report.kept[-1].observed_at:%Y-%m-%d %H:%M}Z")
    if verbose:
        lines += [f"  excluded {day} {day:%a}: {why}"
                  for day, why in sorted(report.excluded_days.items())]
    return "\n".join(lines)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Import cleaned occupancy history.")
    parser.add_argument("source", nargs="?", default=SOURCE_URL)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be kept and write nothing")
    parser.add_argument("--replace", action="store_true",
                        help="empty the history table before loading")
    parser.add_argument("--verbose", action="store_true", help="list every excluded day")
    args = parser.parse_args(argv)

    rows = parse(read_source(args.source))
    with store.connection() as conn:
        first = store.earliest(conn)
        report = clean(rows, first.observed_at if first else None)
        print(describe(report, len(rows), args.verbose))
        if args.dry_run:
            print("dry run: nothing written")
            return
        before = conn.execute("SELECT COUNT(*) AS n FROM history").fetchone()["n"]
        inserted = load(conn, report.kept, replace=args.replace)
        after = conn.execute("SELECT COUNT(*) AS n FROM history").fetchone()["n"]
        print(f"history table: {before:,} -> {after:,} ({inserted:,} inserted)")


if __name__ == "__main__":
    main()
