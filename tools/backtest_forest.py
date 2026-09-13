"""Can a random forest beat the typical-weekday curve?

Builds day-ahead features for every hour since the imported history begins,
retrains a forest at the start of each month on everything before it, and
scores the forest and the dashboard's curve on the same hours. forest.py
explains why the comparison is fair.

    DATABASE_URL=... python tools/backtest_forest.py          # the test years
    DATABASE_URL=... python tools/backtest_forest.py --dev    # the second half of 2022

Settings are checked with --dev; the test years are only for the verdict.
Needs requirements-ml.txt.
"""

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import forest
import store

DEV = (date(2022, 7, 1), date(2022, 12, 31))
TEST = (date(2023, 1, 1), date(2026, 8, 28))   # the days tools/backtest_curve.py scores


def importances(rows, start: date, end: date) -> list[tuple[str, float]]:
    """How much the error rises when each feature is shuffled, for a forest
    trained on everything before `start` and scored on [start, end]."""
    import numpy as np
    from sklearn.inspection import permutation_importance

    train = [r for r in rows if r.day < start and r.actual is not None and r.curve is not None]
    test = [r for r in rows if start <= r.day <= end and forest.scoreable(r)]
    model = forest.default_model().fit(np.array([r.features for r in train]),
                                       np.array([r.actual for r in train]))
    result = permutation_importance(model, np.array([r.features for r in test]),
                                    np.array([r.actual for r in test]),
                                    scoring="neg_mean_absolute_error", n_repeats=5,
                                    random_state=0, n_jobs=-1)
    return sorted(zip(forest.FEATURES, result.importances_mean), key=lambda pair: -pair[1])


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Backtest a random forest against the typical-weekday curve.")
    parser.add_argument("--dev", action="store_true",
                        help="score the second half of 2022, for choosing settings")
    args = parser.parse_args(argv)
    start, end = DEV if args.dev else TEST

    clock = time.monotonic()
    with store.connection() as conn:
        rows = forest.build_rows(conn, forest.FIRST_DAY, end)
    built = time.monotonic() - clock
    clock = time.monotonic()
    forecasts = forest.walk_forward(rows, start, end)
    trained = time.monotonic() - clock
    days = forest.per_day(forecasts)
    comparisons = forest.compare(days)

    print(f"{'dev' if args.dev else 'test'} period {start} to {end}: {len(days):,} days,"
          f" {len(forecasts):,} scored hours")
    print(f"features for {len(rows):,} hours built in {built:.0f}s;"
          f" monthly retraining took {trained:.0f}s\n")
    print(f"{'period':<12} {'days':>5}  {'curve':>7} {'forest':>7} {'change':>7}"
          f"  {'forest better on':>16}  {'bias, curve':>11} {'bias, forest':>12}")
    for c in comparisons:
        print(f"{c.kind:<12} {c.days:>5}  {100 * c.curve_error:>5.1f}pt {100 * c.forest_error:>5.1f}pt"
              f" {100 * (c.forest_error - c.curve_error):>+5.1f}pt  {c.forest_better:>8.0%} of days"
              f"  {100 * c.curve_bias:>+9.1f}pt {100 * c.forest_bias:>+10.1f}pt")

    overall = next(c for c in comparisons if c.kind == "all")
    low, high = forest.weekly_bootstrap(days)
    print(f"\nforest minus curve, mean daily error: {100 * (overall.forest_error - overall.curve_error):+.2f}pt"
          f" (95% interval {100 * low:+.2f} to {100 * high:+.2f}pt, resampling whole weeks)")

    if not args.dev:
        recent = (end.replace(day=1) - timedelta(days=150)).replace(day=1)
        print(f"\nwhat the forest leans on, {recent} to {end} (rise in error when shuffled):")
        for name, rise in importances(rows, recent, end)[:8]:
            print(f"  {name:<24} {100 * rise:>+6.2f}pt")


if __name__ == "__main__":
    main()
