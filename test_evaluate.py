import unittest

import testing
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


class WeekdayBandsTest(testing.DatabaseTest):
    """The curve the dashboard draws and the backtest scores -- one query.

    Runs against Postgres because AT TIME ZONE and percentile_cont have no
    in-memory equivalent, and testing a different engine than production uses
    would defeat the point of moving it here.
    """

    ZONE = "America/Los_Angeles"

    def seed(self, weeks_back, hour, count, minute=0):
        day = date(2026, 9, 7) - timedelta(weeks=weeks_back)   # a Monday
        store.save(self.connection, at(
            datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ), count))

    def bands(self, window=8, target=date(2026, 9, 7)):
        return store.weekday_bands(self.connection, target, self.ZONE, BUCKET, window)

    def test_target_day_is_excluded_from_its_own_band(self):
        self.seed(1, 8, 100)
        self.seed(2, 8, 100)
        self.seed(0, 8, 0)                       # the day being viewed
        bands, weeks, _ = self.bands()

        self.assertEqual(weeks, 2, "the viewed day must not count as an instance")
        self.assertAlmostEqual(bands[16]["median"], 100 / 150)

    def test_median_resists_a_single_closure(self):
        for week in (1, 2, 3):
            self.seed(week, 8, 90)
        self.seed(4, 8, 0)
        bands, _, _ = self.bands()

        self.assertAlmostEqual(bands[16]["median"], 90 / 150, msg="a mean would sag")
        self.assertAlmostEqual(bands[16]["low"], 0.0, msg="but the band still shows it")

    def test_samples_group_into_half_hour_buckets(self):
        self.seed(1, 8, 60, minute=5)
        self.seed(1, 8, 90, minute=25)
        self.seed(1, 8, 150, minute=45)
        bands, _, _ = self.bands()

        self.assertAlmostEqual(bands[16]["median"], 75 / 150, msg="the half hour's mean of 60 and 90")
        self.assertAlmostEqual(bands[17]["median"], 1.0)

    def test_other_weekdays_are_ignored(self):
        store.save(self.connection, at(datetime(2026, 9, 1, 8, tzinfo=TZ), 140))  # Tuesday
        bands, weeks, _ = self.bands()

        self.assertEqual((bands, weeks), ({}, 0))

    def test_band_carries_the_range_across_instances(self):
        self.seed(1, 8, 30)
        self.seed(2, 8, 90)
        self.seed(3, 8, 120)
        bands, _, _ = self.bands()

        self.assertAlmostEqual(bands[16]["low"], 30 / 150)
        self.assertAlmostEqual(bands[16]["high"], 120 / 150)
        self.assertAlmostEqual(bands[16]["median"], 90 / 150)

    def test_local_time_bucketing_survives_a_dst_transition(self):
        """The reason this moved to Postgres: SQLite has no timezone database,
        so it could not place a UTC instant in the right local bucket."""
        # 2026-11-01 is the US autumn transition; these Sundays are 8am local
        for weeks_back in (1, 2, 3):
            day = date(2026, 11, 8) - timedelta(weeks=weeks_back)
            store.save(self.connection, at(
                datetime(day.year, day.month, day.day, 8, tzinfo=TZ), 75))
        bands, weeks, _ = self.bands(target=date(2026, 11, 8))

        self.assertEqual(weeks, 3)
        self.assertIn(16, bands, "8am local must land in the 8am bucket on both"
                                 " sides of the clock change")


class RollingWindowTest(testing.DatabaseTest):
    """Without a window the curve degrades as data accumulates."""

    ZONE = "America/Los_Angeles"

    def mondays_at_8am(self, counts):
        for weeks_back, count in enumerate(reversed(counts), start=1):
            day = date(2026, 9, 7) - timedelta(weeks=weeks_back)
            store.save(self.connection, at(
                datetime(day.year, day.month, day.day, 8, tzinfo=TZ), count))

    def bands(self, window=8, target=date(2026, 9, 7), before=None):
        return store.weekday_bands(self.connection, target, self.ZONE, BUCKET,
                                   window, before=before)

    def test_only_the_most_recent_instances_are_used(self):
        self.mondays_at_8am([0, 0, 0, 0] + [120] * 8)
        bands, weeks, _ = self.bands()

        self.assertEqual(weeks, 8)
        self.assertAlmostEqual(bands[16]["median"], 120 / 150)
        self.assertAlmostEqual(bands[16]["low"], 120 / 150,
                               msg="the stale empty Mondays must be gone entirely")

    def test_reports_what_it_used_not_what_exists(self):
        self.mondays_at_8am([100] * 20)
        _, weeks, _ = self.bands()

        self.assertEqual(weeks, 8, "basis_weeks must describe the numbers shown")

    def test_below_the_window_everything_is_used(self):
        self.mondays_at_8am([100] * 3)
        _, weeks, _ = self.bands()

        self.assertEqual(weeks, 3, "a window changes nothing until it fills")

    def test_the_window_is_relative_to_the_backtest_cutoff(self):
        self.mondays_at_8am([30] * 4 + [30] + [150] * 8)
        target = date(2026, 9, 7) - timedelta(weeks=9)
        midnight = datetime(target.year, target.month, target.day, tzinfo=TZ)
        bands, weeks, _ = self.bands(target=target, before=midnight)

        self.assertEqual(weeks, 4, "only the four Mondays that preceded it")
        self.assertAlmostEqual(bands[16]["median"], 30 / 150)


class WindowSpreadTest(unittest.TestCase):
    """Gating reads the window it is about to suggest, not the whole day."""

    def test_window_spread_ignores_buckets_outside_the_window(self):
        bands = {
            16: {"median": 0.5, "low": 0.48, "high": 0.52, "q1": 0.49, "q3": 0.51, "n": 4},   # steady morning
            17: {"median": 0.5, "low": 0.49, "high": 0.51, "q1": 0.495, "q3": 0.505, "n": 4},
            36: {"median": 0.5, "low": 0.10, "high": 0.90, "q1": 0.2, "q3": 0.8, "n": 4},   # chaotic evening
        }
        morning = evaluate.band_spread(bands, [16, 17])
        evening = evaluate.band_spread(bands, [36])

        self.assertLess(morning, 0.05)
        self.assertGreater(evening, 0.7)
        self.assertLess(morning, evaluate.spread_of({k: [v["low"], v["high"]]
                                                     for k, v in bands.items()}))

    def test_unknown_buckets_are_skipped(self):
        self.assertIsNone(evaluate.band_spread({}, [16, 17]))


class BacktestTest(testing.DatabaseTest):
    """The point of a backtest is that the model cannot see the day it scores."""

    ZONE = "America/Los_Angeles"

    def mondays(self, counts):
        last = date(2026, 9, 7)
        for weeks_back, count in enumerate(reversed(counts)):
            day = last - timedelta(weeks=weeks_back)
            for reading in day_of_readings(day, count):
                store.save(self.connection, reading)

    def run_backtest(self, target=date(2026, 9, 7)):
        return evaluate.backtest(self.connection, store.all_readings(self.connection),
                                 target, TZ, BUCKET)

    def test_refuses_below_three_prior_instances(self):
        self.mondays([75, 75, 75])
        self.assertIsNone(self.run_backtest())

    def test_scores_a_day_it_did_not_see(self):
        self.mondays([75, 75, 75, 75])
        result = self.run_backtest()

        self.assertEqual(result["basis_weeks"], 3)
        self.assertAlmostEqual(result["mean_absolute_error"], 0.0, places=6)

    def test_target_day_cannot_influence_its_own_forecast(self):
        self.mondays([15, 15, 15, 150])
        result = self.run_backtest()

        self.assertAlmostEqual(result["bias"], 0.10 - 1.0, places=6)
        self.assertGreater(result["mean_absolute_error"], 0.8)

    def test_bias_sign_distinguishes_over_from_under_forecasting(self):
        self.mondays([150, 150, 150, 15])
        self.assertGreater(self.run_backtest()["bias"], 0, "predicted busy, was quiet")


class PredictionLogTest(testing.DatabaseTest):
    """Absence of feedback must stay distinguishable from a "no" answer."""

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


class StorageNormalisationTest(testing.DatabaseTest):
    """timestamptz stores an instant, so an offset can no longer be lost.

    Under SQLite these were text comparisons and a locally-stamped reading
    sorted wrongly against UTC bounds. The column type now makes that
    impossible rather than caught-by-test.
    """

    def test_a_local_time_reading_round_trips_as_the_same_instant(self):
        local = datetime(2026, 9, 7, 14, tzinfo=TZ)
        store.save(self.connection, Reading(90, 150, local))
        [stored] = store.all_readings(self.connection)

        # the offset it comes back in is the session's; the instant is what
        # must survive, and comparing datetimes compares instants
        self.assertEqual(stored.observed_at, local)

    def test_a_local_time_reading_is_found_by_a_range_query(self):
        local = datetime(2026, 9, 7, 14, tzinfo=TZ)
        store.save(self.connection, Reading(90, 150, local))
        found = store.between(self.connection, local - timedelta(minutes=1),
                              local + timedelta(minutes=1))

        self.assertEqual(len(found), 1)


class PeriodMatchingTest(testing.DatabaseTest):
    """A Monday in term is compared with Mondays in term, not with the summer
    ones that happen to be most recent."""

    ZONE = "America/Los_Angeles"
    TARGET = date(2026, 9, 14)   # a Monday

    def label(self, day, kind):
        self.connection.execute(
            "INSERT INTO calendar_days (day, kind, label) VALUES (%s, %s, %s)", (day, kind, kind))

    def monday(self, weeks_back, count, kind):
        day = self.TARGET - timedelta(weeks=weeks_back)
        store.save(self.connection, at(datetime(day.year, day.month, day.day, 8, tzinfo=TZ), count))
        self.label(day, kind)

    def summer_then_term(self):
        self.monday(1, 20, "summer")                  # the most recent Monday
        for weeks_back in (2, 3, 4):
            self.monday(weeks_back, 120, "instruction")

    def bands(self, **options):
        return store.weekday_bands(self.connection, self.TARGET, self.ZONE, BUCKET, 8, **options)

    def test_instances_come_from_the_same_kind_of_period(self):
        self.label(self.TARGET, "instruction")
        self.summer_then_term()
        bands, weeks, _ = self.bands()

        self.assertEqual(weeks, 3)
        self.assertAlmostEqual(bands[16]["low"], 120 / 150, msg="the summer Monday is no instance")

    def test_matching_can_be_switched_off_to_measure_it(self):
        self.label(self.TARGET, "instruction")
        self.summer_then_term()
        bands, weeks, _ = self.bands(match_period=False)

        self.assertEqual(weeks, 4)
        self.assertAlmostEqual(bands[16]["low"], 20 / 150)

    def test_a_day_the_calendar_does_not_cover_falls_back_to_recency(self):
        self.summer_then_term()                       # the target itself is unlabelled
        _, weeks, _ = self.bands()

        self.assertEqual(weeks, 4)

    def test_imported_history_counts_as_instances(self):
        self.label(self.TARGET, "instruction")
        for weeks_back in (2, 3):
            day = self.TARGET - timedelta(weeks=weeks_back)
            self.connection.execute(
                "INSERT INTO history (observed_at, count) VALUES (%s, %s)",
                (datetime(day.year, day.month, day.day, 8, tzinfo=TZ), 90))
            self.label(day, "instruction")
        self.monday(1, 90, "instruction")
        bands, weeks, _ = self.bands()

        self.assertEqual(weeks, 3)
        self.assertAlmostEqual(bands[16]["median"], 90 / 150)


class SamplingRateTest(testing.DatabaseTest):
    """Live collection samples every four minutes and imported history every
    ten, so each day must get one vote however many readings it has."""

    ZONE = "America/Los_Angeles"

    def test_a_densely_sampled_day_does_not_outvote_sparse_ones(self):
        target = date(2026, 9, 14)
        dense = target - timedelta(weeks=1)
        for minute in range(0, 30, 4):                     # eight readings at 100
            store.save(self.connection, at(
                datetime(dense.year, dense.month, dense.day, 8, minute, tzinfo=TZ), 100))
        for weeks_back, count in ((2, 50), (3, 60)):       # three readings each
            day = target - timedelta(weeks=weeks_back)
            for minute in (0, 10, 20):
                store.save(self.connection, at(
                    datetime(day.year, day.month, day.day, 8, minute, tzinfo=TZ), count))
        bands, weeks, _ = store.weekday_bands(self.connection, target, self.ZONE, BUCKET, 8)

        # pooled, eight of the fourteen readings are 100, so the median would be 100
        self.assertEqual(weeks, 3)
        self.assertAlmostEqual(bands[16]["median"], 60 / 150)
