"""Forecast today with the random forest, for the dashboard to draw and suggest from.

Runs hourly on a scheduled Fly machine (Dockerfile.forecast), because Fly's
schedules are fuzzy: the first run after local midnight forecasts the day, and
every later run that day finds the forecast stored and exits before importing
scikit-learn. A run that fails stores nothing, so the dashboard falls back to
the curve and the next hour tries again.

The forest is retrained on every run, on every hour before the day it
forecasts. The backtest retrained monthly; retraining daily only gives it more
to learn from, and nothing has to keep a 155 MB model between runs.

    DATABASE_URL=... python tools/forecast_today.py [--date YYYY-MM-DD] [--force] [--dry-run]
"""

import argparse
import resource
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import forest


def peak_megabytes() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1e6 if sys.platform == "darwin" else peak / 1024   # bytes on macOS, KB on Linux


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Forecast a day with the random forest.")
    parser.add_argument("--date", type=date.fromisoformat,
                        help="forecast this day instead of today, e.g. to check a past day")
    parser.add_argument("--force", action="store_true", help="replace a forecast already stored")
    parser.add_argument("--dry-run", action="store_true", help="print the forecast without storing it")
    args = parser.parse_args(argv)
    day = args.date or datetime.now(forest.LOCAL_TZ).date()

    started = time.monotonic()
    outcome, by_hour = forest.forecast_and_store(day, force=args.force, dry_run=args.dry_run)
    if outcome == "already forecast":
        print(f"{day}: already forecast")
        return
    if outcome == "no curve":
        print(f"{day}: fewer than {forest.MIN_INSTANCES} comparable days, so no curve to"
              " improve on; nothing stored")
        return

    shape = "  ".join(f"{hour}:00 {by_hour[hour]:.0%}" for hour in (8, 12, 17, 20))
    print(f"{day}: {outcome} in {time.monotonic() - started:.0f}s,"
          f" peak memory {peak_megabytes():.0f} MB -- {shape}")


if __name__ == "__main__":
    main()
