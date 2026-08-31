import unittest
from datetime import datetime, timedelta
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
