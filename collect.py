"""Record RSF occupancy to readings.db.

One-shot by default, so it can be driven by cron or launchd:
    */5 * * * * cd /path/to/rsf-dashboard && .venv/bin/python collect.py

Or run it standalone with its own timer:
    .venv/bin/python collect.py --interval 300
"""

import argparse
import sys
import time
from datetime import datetime, timezone

import store
from density import fetch_reading, percentage


def collect_once(connection) -> None:
    reading = fetch_reading()
    store.save(connection, reading)
    print(
        f"{reading.observed_at:%Y-%m-%d %H:%M:%S} UTC  "
        f"{reading.count:>3}/{reading.capacity}  "
        f"{percentage(reading.count, reading.capacity):.0%}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interval",
        type=int,
        metavar="SECONDS",
        help="keep running, collecting every SECONDS (default: collect once and exit)",
    )
    args = parser.parse_args()

    connection = store.connect()

    if args.interval is None:
        collect_once(connection)
        return 0

    print(f"collecting every {args.interval}s, ctrl-c to stop")
    while True:
        # a long-running collector must survive a blip; one failed poll is not
        # a reason to lose every later one
        try:
            collect_once(connection)
        except Exception as error:
            print(f"skipped: {error}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
