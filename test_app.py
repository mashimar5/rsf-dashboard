import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import app
from density import Reading

TZ = ZoneInfo("America/Los_Angeles")


def reading(local_dt, count, capacity=150):
    return Reading(count=count, capacity=capacity, observed_at=local_dt.astimezone(ZoneInfo("UTC")))


def monday(week_offset, hour, minute=0):
    """A Monday at a local wall-clock time, `week_offset` weeks before 2026-09-07"""
    base = datetime(2026, 9, 7, hour, minute, tzinfo=TZ)  # a Monday
    return base - timedelta(weeks=week_offset)


class TypicalCurveTest(unittest.TestCase):
    def test_excludes_today_from_its_own_average(self):
        readings = [
            reading(monday(1, 8), 100),
            reading(monday(2, 8), 100),
            # today's wildly different value must not pull the median
            reading(monday(0, 8), 0),
        ]
        buckets, weeks = app.typical_curve(readings, 0, monday(0, 8).date(), TZ)

        self.assertEqual(weeks, 2, "today must not count as an instance")
        slot = 8 * 60 // app.BUCKET_MINUTES
        self.assertAlmostEqual(buckets[slot], 100 / 150)

    def test_uses_median_so_one_outlier_does_not_skew(self):
        readings = [
            reading(monday(1, 8), 90),
            reading(monday(2, 8), 90),
            reading(monday(3, 8), 90),
            reading(monday(4, 8), 0),  # a closure
        ]
        buckets, _ = app.typical_curve(readings, 0, monday(0, 8).date(), TZ)

        slot = 8 * 60 // app.BUCKET_MINUTES
        self.assertAlmostEqual(buckets[slot], 90 / 150, msg="a mean would be dragged to ~0.45")

    def test_groups_samples_into_half_hour_buckets(self):
        readings = [
            reading(monday(1, 8, 5), 60),
            reading(monday(1, 8, 25), 90),   # same 8:00-8:30 bucket
            reading(monday(1, 8, 45), 150),  # next bucket
        ]
        buckets, _ = app.typical_curve(readings, 0, monday(0, 8).date(), TZ)

        self.assertAlmostEqual(buckets[16], 75 / 150, msg="median of 60 and 90")
        self.assertAlmostEqual(buckets[17], 1.0)

    def test_ignores_other_weekdays(self):
        tuesday = monday(1, 8) + timedelta(days=1)
        buckets, weeks = app.typical_curve(
            [reading(tuesday, 140)], 0, monday(0, 8).date(), TZ
        )
        self.assertEqual(weeks, 0)
        self.assertEqual(buckets, {})


def same_weekday_as_today(weeks_ago, hour):
    """A local datetime `weeks_ago` weeks back, so it shares today's weekday"""
    day = datetime.now(TZ).date() - timedelta(weeks=weeks_ago)
    return datetime(day.year, day.month, day.day, hour, tzinfo=TZ)


class DaySummaryTest(unittest.TestCase):
    """A day is mostly closed hours reading zero, so scoping matters a lot."""

    def setUp(self):
        self.midnight = datetime(2026, 8, 30, tzinfo=TZ)

    def at(self, hour, count, minute=0):
        return reading(self.midnight.replace(hour=hour, minute=minute), count)

    def hours(self, opens, closes):
        return SimpleNamespace(opens=opens, closes=closes)

    def test_average_ignores_readings_taken_while_closed(self):
        readings = [
            self.at(3, 0), self.at(5, 0),        # closed overnight
            self.at(9, 75), self.at(15, 75),     # open
            self.at(23, 0),                      # closed again
        ]
        summary = app.day_summary(readings, self.midnight, self.hours(8 * 60, 22 * 60))

        # only the two open-hours readings count: both 75/150
        self.assertAlmostEqual(summary["average_pct"], 0.5)
        self.assertTrue(summary["open_only"])

    def _full_day(self, quiet_hour=8, fluke_at=None):
        """Readings every 15 minutes from 8am to 10pm, busy except one quiet hour"""
        readings = []
        for minute in range(8 * 60, 22 * 60 + 1, 15):
            hour, rest = divmod(minute, 60)
            count = 15 if hour == quiet_hour else 120
            if fluke_at is not None and minute == fluke_at:
                count = 0            # a single dip, not a sustained one
            readings.append(self.at(hour, count, rest))
        return readings

    def test_quietest_hour_is_sustained_not_a_single_dip(self):
        summary = app.day_summary(
            self._full_day(quiet_hour=8, fluke_at=15 * 60),
            self.midnight,
            self.hours(8 * 60, 22 * 60),
        )
        quietest = summary["quietest"]

        self.assertEqual(quietest["start"].hour, 8, "the sustained quiet hour should win")
        self.assertLess(quietest["average_pct"], 0.2)
        self.assertEqual(summary["peak"].count, 120)

    def test_quietest_window_must_fit_inside_opening_hours(self):
        summary = app.day_summary(
            self._full_day(quiet_hour=21),      # quiet in the final open hour
            self.midnight,
            self.hours(8 * 60, 21 * 60 + 30),   # closes 9:30pm, mid-quiet-hour
        )
        quietest = summary["quietest"]

        # a 9pm-10pm window runs past closing, so it cannot be chosen
        self.assertLessEqual(
            (quietest["end"] - self.midnight).total_seconds() / 60, 21 * 60 + 30
        )

    def test_quietest_ignores_readings_taken_while_closed(self):
        readings = [self.at(3, 0), self.at(4, 0)] + self._full_day(quiet_hour=8)
        summary = app.day_summary(readings, self.midnight, self.hours(8 * 60, 22 * 60))

        self.assertGreaterEqual(summary["quietest"]["start"].hour, 8)

    def test_falls_back_to_the_whole_day_when_hours_are_unknown(self):
        readings = [self.at(4, 0), self.at(12, 150)]
        summary = app.day_summary(readings, self.midnight, None)

        self.assertAlmostEqual(summary["average_pct"], 0.5)
        self.assertFalse(summary["open_only"], "should say so when not scoped")

    def test_closing_after_midnight_does_not_empty_the_window(self):
        # "12 p.m.-12 a.m." parses as opens=720, closes=0
        readings = [self.at(14, 60), self.at(20, 90)]
        summary = app.day_summary(readings, self.midnight, self.hours(720, 0))

        self.assertTrue(summary["open_only"])
        self.assertEqual(summary["peak"].count, 90)

    def test_a_fully_closed_day_still_reports_rather_than_crashing(self):
        readings = [self.at(4, 0), self.at(12, 0)]
        summary = app.day_summary(readings, self.midnight, self.hours(None, None))

        self.assertEqual(summary["average_pct"], 0.0)
        self.assertFalse(summary["open_only"])


class TypicalLineRenderingTest(unittest.TestCase):
    def _render_with(self, readings, today_samples=5):
        """Render the page against synthetic history.

        todays_readings is patched too: leaving it to hit the real database
        made this test pass or fail depending on the time of day, since just
        after midnight there is nothing recorded for today yet.
        """
        now = datetime.now(TZ)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = [reading(midnight + timedelta(hours=n), 100) for n in range(today_samples)]
        with patch("store.all_readings", return_value=readings), \
             patch("app.todays_readings", return_value=(today, midnight)), \
             patch("app.fetch_reading", return_value=reading(now, 100)):
            return app.app.test_client().get("/").data.decode()

    def test_typical_line_shows_even_before_today_has_data(self):
        """Just after midnight the typical curve is the only thing worth drawing"""
        readings = [reading(same_weekday_as_today(week, hour), 100)
                    for week in (1, 2, 3) for hour in (8, 12, 18)]
        html = self._render_with(readings, today_samples=0)
        self.assertIn("Typical", html)
        self.assertNotIn("Waiting for today", html)

    def test_hidden_below_three_instances(self):
        readings = [reading(same_weekday_as_today(week, 8), 100) for week in (1, 2)]
        self.assertNotIn("Typical", self._render_with(readings))

    def test_shown_once_enough_history_exists(self):
        readings = [reading(same_weekday_as_today(week, hour), 100)
                    for week in (1, 2, 3) for hour in (8, 12, 18)]
        html = self._render_with(readings)
        # only assert on the legend, which appears whenever the line is drawn
        self.assertIn("Typical", html)
        self.assertIn("3 weeks", html)


if __name__ == "__main__":
    unittest.main()
