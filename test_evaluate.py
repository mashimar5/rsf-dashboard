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


class DispersionMetricTest(unittest.TestCase):
    def test_small_samples_use_the_range(self):
        self.assertAlmostEqual(evaluate.dispersion([0.2, 0.5, 0.7]), 0.5)

    def test_large_samples_use_the_interquartile_range(self):
        values = [0.40, 0.42, 0.44, 0.46, 0.48, 0.50, 0.52, 0.54]
        self.assertLess(
            evaluate.dispersion(values), max(values) - min(values),
            "IQR must be tighter than the range it replaces",
        )

    def test_a_single_outlier_moves_the_range_but_not_the_iqr(self):
        steady = [0.50] * 11
        with_closure = steady + [0.0]          # one holiday closure

        # the range doubles the story; the IQR ignores it, as the median does
        self.assertAlmostEqual(evaluate.dispersion(with_closure), 0.0, places=6)
        self.assertAlmostEqual(max(with_closure) - min(with_closure), 0.5)

    def test_range_inflates_with_sample_count_which_is_why_it_is_replaced(self):
        """Same distribution, more samples: the range grows, so it is not
        comparable across cohorts of different size."""
        import random
        random.seed(11)
        draw = lambda n: [random.gauss(0.5, 0.08) for _ in range(n)]
        small = max(s := draw(4)) - min(s)
        large = max(l := draw(40)) - min(l)

        self.assertGreater(large, small * 1.5)

    def test_too_few_values_to_disagree(self):
        self.assertIsNone(evaluate.dispersion([0.5]))
        self.assertIsNone(evaluate.dispersion([]))


class WeekdayBandsTest(unittest.TestCase):
    """The curve the dashboard draws and the backtest scores -- one function."""

    def monday(self, weeks_back, hour, minute=0, count=100):
        day = date(2026, 9, 7) - timedelta(weeks=weeks_back)   # 2026-09-07 is a Monday
        return at(datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ), count)

    def bands(self, readings, target=date(2026, 9, 7)):
        return evaluate.weekday_bands(readings, target, TZ, BUCKET)

    def test_target_day_is_excluded_from_its_own_band(self):
        readings = [self.monday(1, 8, count=100), self.monday(2, 8, count=100),
                    self.monday(0, 8, count=0)]          # the day being viewed
        bands, weeks, _ = self.bands(readings)

        self.assertEqual(weeks, 2, "the viewed day must not count as an instance")
        self.assertAlmostEqual(bands[16]["median"], 100 / 150)

    def test_median_resists_a_single_closure(self):
        readings = [self.monday(w, 8, count=90) for w in (1, 2, 3)]
        readings.append(self.monday(4, 8, count=0))
        bands, _, _ = self.bands(readings)

        self.assertAlmostEqual(bands[16]["median"], 90 / 150, msg="a mean would sag")
        self.assertAlmostEqual(bands[16]["low"], 0.0, msg="but the band still shows it")

    def test_samples_group_into_half_hour_buckets(self):
        readings = [self.monday(1, 8, 5, count=60), self.monday(1, 8, 25, count=90),
                    self.monday(1, 8, 45, count=150)]
        bands, _, _ = self.bands(readings)

        self.assertAlmostEqual(bands[16]["median"], 75 / 150, msg="median of 60 and 90")
        self.assertAlmostEqual(bands[17]["median"], 1.0)

    def test_other_weekdays_are_ignored(self):
        tuesday = at(datetime(2026, 9, 1, 8, tzinfo=TZ), 140)
        bands, weeks, _ = self.bands([tuesday])

        self.assertEqual((bands, weeks), ({}, 0))

    def test_band_carries_the_range_across_instances(self):
        readings = [self.monday(1, 8, count=30), self.monday(2, 8, count=90),
                    self.monday(3, 8, count=120)]
        bands, _, _ = self.bands(readings)

        self.assertAlmostEqual(bands[16]["low"], 30 / 150)
        self.assertAlmostEqual(bands[16]["high"], 120 / 150)
        self.assertAlmostEqual(bands[16]["median"], 90 / 150)


class RollingWindowTest(unittest.TestCase):
    """Without a window the curve degrades as data accumulates: a "typical
    Monday" would eventually blend semester weeks with winter break."""

    def mondays_at_8am(self, counts):
        """One reading at 8am on each of len(counts) consecutive Mondays,
        oldest first, ending the week before 2026-09-07."""
        readings = []
        for weeks_back, count in enumerate(reversed(counts), start=1):
            day = date(2026, 9, 7) - timedelta(weeks=weeks_back)
            readings.append(at(datetime(day.year, day.month, day.day, 8, tzinfo=TZ), count))
        return readings

    def bands(self, readings, **kwargs):
        return evaluate.weekday_bands(readings, date(2026, 9, 7), TZ, BUCKET, **kwargs)

    def test_only_the_most_recent_instances_are_used(self):
        # eight recent busy Mondays, preceded by four ancient empty ones
        readings = self.mondays_at_8am([0, 0, 0, 0] + [120] * 8)
        bands, weeks, _ = self.bands(readings)

        self.assertEqual(weeks, evaluate.WINDOW_INSTANCES)
        self.assertAlmostEqual(bands[16]["median"], 120 / 150)
        self.assertAlmostEqual(bands[16]["low"], 120 / 150,
                               msg="the stale empty Mondays must be gone entirely")

    def test_reports_what_it_used_not_what_exists(self):
        _, weeks, _ = self.bands(self.mondays_at_8am([100] * 20))

        self.assertEqual(weeks, evaluate.WINDOW_INSTANCES,
                         "basis_weeks must describe the numbers actually shown")

    def test_below_the_window_everything_is_used(self):
        _, weeks, _ = self.bands(self.mondays_at_8am([100] * 3))

        self.assertEqual(weeks, 3, "a window changes nothing until it fills")

    def test_the_window_is_relative_to_the_backtest_cutoff(self):
        """Backtesting an old day uses the instances before it, not the most
        recent ones overall."""
        # four quiet Mondays, then the target, then eight busy ones after it
        readings = self.mondays_at_8am([30] * 4 + [30] + [150] * 8)
        target = date(2026, 9, 7) - timedelta(weeks=9)
        midnight = datetime(target.year, target.month, target.day, tzinfo=TZ)
        bands, weeks, _ = evaluate.weekday_bands(
            readings, target, TZ, BUCKET, before=midnight
        )

        self.assertEqual(weeks, 4, "only the four Mondays that preceded it")
        self.assertAlmostEqual(bands[16]["median"], 30 / 150)

    def test_a_day_with_patchy_collection_still_counts_once(self):
        """The window counts days, so a densely sampled day cannot crowd out
        a sparse one."""
        dense_day = date(2026, 9, 7) - timedelta(weeks=1)
        sparse_days = [date(2026, 9, 7) - timedelta(weeks=n) for n in range(2, 5)]
        readings = [
            at(datetime(dense_day.year, dense_day.month, dense_day.day, 8, m, tzinfo=TZ), 150)
            for m in range(0, 30, 5)
        ]
        readings += [
            at(datetime(d.year, d.month, d.day, 8, tzinfo=TZ), 30) for d in sparse_days
        ]
        _, weeks, _ = self.bands(readings)

        self.assertEqual(weeks, 4)


class WindowSpreadTest(unittest.TestCase):
    """Gating reads the window it is about to suggest, not the whole day."""

    def test_window_spread_ignores_buckets_outside_the_window(self):
        bands = {
            16: {"median": 0.5, "low": 0.48, "high": 0.52},   # steady morning
            17: {"median": 0.5, "low": 0.49, "high": 0.51},
            36: {"median": 0.5, "low": 0.10, "high": 0.90},   # chaotic evening
        }
        morning = evaluate.band_spread(bands, [16, 17])
        evening = evaluate.band_spread(bands, [36])

        self.assertLess(morning, 0.05)
        self.assertGreater(evening, 0.7)
        self.assertLess(morning, evaluate.spread_of({k: [v["low"], v["high"]]
                                                     for k, v in bands.items()}))

    def test_unknown_buckets_are_skipped(self):
        self.assertIsNone(evaluate.band_spread({}, [16, 17]))


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
        return store.log_prediction(
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


class StorageNormalisationTest(unittest.TestCase):
    """Timestamps are compared as text, so what goes in must be UTC."""

    def setUp(self):
        self.path = Path(f"/tmp/rsf-utc-{id(self)}.db")
        self.connection = store.connect(self.path)
        self.addCleanup(self.connection.close)
        self.addCleanup(lambda: [
            self.path.with_name(self.path.name + s).unlink(missing_ok=True)
            for s in ("", "-wal", "-shm")
        ])

    def test_a_local_time_reading_is_stored_as_utc(self):
        local = datetime(2026, 9, 7, 14, tzinfo=TZ)
        store.save(self.connection, Reading(90, 150, local))
        stored = self.connection.execute("SELECT observed_at FROM readings").fetchone()[0]

        self.assertTrue(stored.endswith("+00:00"), f"stored as {stored}")

    def test_a_local_time_reading_is_still_found_by_a_range_query(self):
        local = datetime(2026, 9, 7, 14, tzinfo=TZ)
        store.save(self.connection, Reading(90, 150, local))
        found = store.between(self.connection, local - timedelta(minutes=1),
                              local + timedelta(minutes=1))

        self.assertEqual(len(found), 1, "text comparison fails without normalising")
