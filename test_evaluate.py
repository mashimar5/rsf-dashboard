import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import evaluate
import store
from density import Reading

TZ = ZoneInfo("America/Los_Angeles")
BUCKET = 30


def at(local_dt, count, capacity=150):
    return Reading(count=count, capacity=capacity,
                   observed_at=local_dt.astimezone(timezone.utc))


def day_of_readings(day: date, count, start_hour=8, end_hour=20, step=5):
    """A day's readings at `step`-minute cadence, all at the same count"""
    readings = []
    minute = start_hour * 60
    while minute <= end_hour * 60:
        hour, rest = divmod(minute, 60)
        readings.append(at(datetime(day.year, day.month, day.day, hour, rest, tzinfo=TZ), count))
        minute += step
    return readings


class ScoreTest(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 7, 14, tzinfo=TZ)
        self.end = self.start + timedelta(hours=1)

    def test_error_is_signed_so_bias_is_visible(self):
        readings = [at(self.start + timedelta(minutes=n * 5), 75) for n in range(12)]
        result = evaluate.score(readings, predicted_pct=0.60, start=self.start, end=self.end)

        self.assertAlmostEqual(result.actual_pct, 0.5)
        self.assertAlmostEqual(result.error, 0.10, msg="positive = forecast too high")
        self.assertAlmostEqual(result.absolute_error, 0.10)

    def test_a_sparsely_observed_window_is_unscoreable(self):
        readings = [at(self.start + timedelta(minutes=n * 5), 75) for n in range(2)]

        self.assertIsNone(
            evaluate.score(readings, 0.6, self.start, self.end),
            "two readings should not be scored like twelve",
        )

    def test_readings_outside_the_window_are_ignored(self):
        inside = [at(self.start + timedelta(minutes=n * 5), 75) for n in range(12)]
        outside = [at(self.start - timedelta(hours=3), 150), at(self.end + timedelta(hours=1), 0)]
        result = evaluate.score(inside + outside, 0.5, self.start, self.end)

        self.assertEqual(result.samples, 12)
        self.assertAlmostEqual(result.actual_pct, 0.5)


class SpreadTest(unittest.TestCase):
    def test_agreement_and_disagreement_are_distinguished(self):
        agree = evaluate.spread_of({0: [0.50, 0.51, 0.49]})
        disagree = evaluate.spread_of({0: [0.20, 0.45, 0.70]})

        self.assertLess(agree, 0.05)
        self.assertGreater(disagree, 0.4, "a count of three hides this; spread does not")

    def test_single_sample_buckets_have_no_spread(self):
        self.assertIsNone(evaluate.spread_of({0: [0.5]}))


class BacktestTest(unittest.TestCase):
    """The point of a backtest is that the model cannot see the day it scores."""

    def mondays(self, counts):
        """Consecutive Mondays ending 2026-09-07, one entry per count given"""
        last = date(2026, 9, 7)
        readings = []
        for weeks_back, count in enumerate(reversed(counts)):
            readings += day_of_readings(last - timedelta(weeks=weeks_back), count)
        return readings

    def test_refuses_below_three_prior_instances(self):
        # three Mondays total, so only two precede the target
        readings = self.mondays([75, 75, 75])
        self.assertIsNone(evaluate.backtest(readings, date(2026, 9, 7), TZ, BUCKET))

    def test_scores_a_day_it_did_not_see(self):
        readings = self.mondays([75, 75, 75, 75])
        result = evaluate.backtest(readings, date(2026, 9, 7), TZ, BUCKET)

        self.assertEqual(result["basis_weeks"], 3)
        self.assertAlmostEqual(result["mean_absolute_error"], 0.0, places=6)

    def test_target_day_cannot_influence_its_own_forecast(self):
        # three quiet Mondays, then a wildly busy one to predict
        readings = self.mondays([15, 15, 15, 150])
        result = evaluate.backtest(readings, date(2026, 9, 7), TZ, BUCKET)

        # forecast 10%, actual 100% -> the error must show, not be averaged away
        self.assertAlmostEqual(result["bias"], 0.10 - 1.0, places=6)
        self.assertGreater(result["mean_absolute_error"], 0.8)

    def test_later_days_cannot_leak_backwards_into_an_earlier_forecast(self):
        """The cutoff, not just the exclude-the-target-day rule.

        Backtesting a day in the middle of history must not use weekdays that
        came after it. Testing only the most recent day hides this entirely,
        because there is nothing later to leak.
        """
        # three quiet Mondays, the target, then three busy ones after it
        readings = self.mondays([15, 15, 15, 75, 150, 150, 150])
        target = date(2026, 9, 7) - timedelta(weeks=3)
        result = evaluate.backtest(readings, target, TZ, BUCKET)

        self.assertEqual(result["basis_weeks"], 3, "only the three prior Mondays")
        # forecast 10% from the quiet Mondays, actual 50%
        self.assertAlmostEqual(result["bias"], 0.10 - 0.50, places=6)

    def test_bias_sign_distinguishes_over_from_under_forecasting(self):
        over = evaluate.backtest(self.mondays([150, 150, 150, 15]), date(2026, 9, 7), TZ, BUCKET)
        under = evaluate.backtest(self.mondays([15, 15, 15, 150]), date(2026, 9, 7), TZ, BUCKET)

        self.assertGreater(over["bias"], 0, "predicted busy, was quiet")
        self.assertLess(under["bias"], 0, "predicted quiet, was busy")


class PredictionLogTest(unittest.TestCase):
    """Absence of feedback must stay distinguishable from a "no" answer."""

    def setUp(self):
        self.path = Path(f"/tmp/rsf-eval-test-{id(self)}.db")
        self.connection = store.connect(self.path)
        self.addCleanup(self.connection.close)
        self.addCleanup(lambda: [
            self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)
            for suffix in ("", "-wal", "-shm")
        ])

    def save(self, pct=0.35, weeks=3, spread=0.1):
        start = datetime(2026, 9, 7, 14, tzinfo=TZ)
        return store.save_prediction(
            self.connection, date(2026, 9, 7), start, start + timedelta(hours=1),
            predicted_pct=pct, basis_weeks=weeks, basis_spread=spread,
        )

    def test_unanswered_is_none_not_false(self):
        self.save()
        [row] = store.predictions_on(self.connection, date(2026, 9, 7))

        self.assertIsNone(row["went"], "no answer is not the same as answering no")

    def test_answering_no_is_recorded_as_false(self):
        prediction_id = self.save()
        store.record_feedback(self.connection, prediction_id, went=False)
        [row] = store.predictions_on(self.connection, date(2026, 9, 7))

        self.assertIs(row["went"], False)
        self.assertIsNotNone(row["answered_at"])

    def test_re_answering_replaces_rather_than_duplicating(self):
        prediction_id = self.save()
        store.record_feedback(self.connection, prediction_id, went=False)
        store.record_feedback(self.connection, prediction_id, went=True)
        rows = store.predictions_on(self.connection, date(2026, 9, 7))

        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["went"], True)

    def test_confidence_at_prediction_time_is_preserved(self):
        self.save(pct=0.42, weeks=5, spread=0.33)
        [row] = store.predictions_on(self.connection, date(2026, 9, 7))

        self.assertAlmostEqual(row["predicted_pct"], 0.42)
        self.assertEqual(row["basis_weeks"], 5)
        self.assertAlmostEqual(row["basis_spread"], 0.33)


if __name__ == "__main__":
    unittest.main()
