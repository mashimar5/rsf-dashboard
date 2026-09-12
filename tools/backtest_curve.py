"""Does comparing like periods with like make the typical curve more accurate?

Scores the curve against days of imported history it never saw, twice for
each day: once drawing instances from the same kind of academic period as
the day being scored, once taking the most recent same weekdays regardless.
Prints mean absolute error and bias, in percentage points of capacity, by
kind of period.

Buckets are an hour rather than the dashboard's half hour: history arrives
every ten minutes, and scoring a bucket needs at least four readings
(evaluate.MIN_WINDOW_SAMPLES), which a half hour of history never has.

    DATABASE_URL=... python tools/backtest_curve.py [--start 2023-01-01] [--end 2026-08-28] [--every 1]
"""

import argparse
import sys
import time as clock
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import evaluate
import store

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
BUCKET_MINUTES = 60


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Backtest period matching.")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2023, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 8, 28))
    parser.add_argument("--every", type=int, default=1, help="score every Nth day")
    args = parser.parse_args(argv)

    results = {True: defaultdict(list), False: defaultdict(list)}
    started = clock.monotonic()
    with store.connection() as conn:
        day = args.start
        while day <= args.end:
            midnight = datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ)
            readings = store.occupancy_between(conn, midnight, midnight + timedelta(days=1))
            kind = store.period_of(conn, day) or "unlabelled"
            scored = {
                matched: evaluate.backtest(conn, readings, day, LOCAL_TZ, BUCKET_MINUTES,
                                           match_period=matched)
                for matched in (True, False)
            }
            # only days both rules could score, so the comparison is like for like
            if all(scored.values()):
                for matched, result in scored.items():
                    results[matched][kind].append(result)
                    results[matched]["all"].append(result)
            day += timedelta(days=args.every)

    print(f"scored {len(results[True]['all'])} days, {args.start} to {args.end}"
          f" every {args.every}, in {clock.monotonic() - started:.0f}s\n")
    print(f"{'period':<12} {'days':>5}   {'error, matched':>14} {'error, recent':>13}"
          f"   {'bias, matched':>13} {'bias, recent':>12}")
    kinds = sorted(results[True], key=lambda kind: (kind == "all", -len(results[True][kind])))
    for kind in kinds:
        matched, recent = results[True][kind], results[False][kind]
        print(f"{kind:<12} {len(matched):>5}"
              f"   {100 * mean(r['mean_absolute_error'] for r in matched):>12.1f}pt"
              f" {100 * mean(r['mean_absolute_error'] for r in recent):>11.1f}pt"
              f"   {100 * mean(r['bias'] for r in matched):>+11.1f}pt"
              f" {100 * mean(r['bias'] for r in recent):>+10.1f}pt")


if __name__ == "__main__":
    main()
