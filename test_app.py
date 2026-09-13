import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import app
import testing  # noqa: F401  -- points the pool at the test database
from density import Reading

TZ = ZoneInfo("America/Los_Angeles")


def reading(local_dt, count, capacity=150):
    return Reading(count=count, capacity=capacity, observed_at=local_dt.astimezone(ZoneInfo("UTC")))


def monday(week_offset, hour, minute=0):
    """A Monday at a local wall-clock time, `week_offset` weeks before 2026-09-07"""
    base = datetime(2026, 9, 7, hour, minute, tzinfo=TZ)  # a Monday
    return base - timedelta(weeks=week_offset)


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


def same_weekday_as_today(weeks_ago, hour):
    """A local datetime `weeks_ago` weeks back, so it shares today's weekday"""
    day = datetime.now(TZ).date() - timedelta(weeks=weeks_ago)
    return datetime(day.year, day.month, day.day, hour, tzinfo=TZ)


class ProxyAwarenessTest(unittest.TestCase):
    """Fly forwards plain HTTP; the OAuth redirect_uri must still be https,
    or Google rejects it as a mismatch."""

    def test_external_urls_honour_the_forwarded_protocol(self):
        # secret_key is read at import, so patching the environment is too
        # late; it lives in app.config, which patch.dict can restore cleanly
        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "cid"}), \
             patch.dict(app.app.config, {"SECRET_KEY": "test-key"}):
            client = app.app.test_client()
            response = client.get(
                "/auth/google",
                headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "rsf-dashboard.fly.dev"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            "redirect_uri=https%3A%2F%2Frsf-dashboard.fly.dev%2Fauth%2Fcallback",
            response.headers["Location"],
        )


class DayApiTest(unittest.TestCase):
    """/api/day is the contract the React client renders from."""

    def _fetch(self, weeks, today_samples=5, forecast=None):
        """Render /api/day with a stubbed curve.

        weekday_bands is a single query now, so stubbing it is both simpler
        and closer to what the route actually depends on than seeding rows.
        """
        now = datetime.now(TZ)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = [reading(midnight + timedelta(hours=n), 100) for n in range(today_samples)]
        bands = {
            slot: {"median": 0.4, "low": 0.3, "high": 0.5, "q1": 0.35, "q3": 0.45, "n": weeks}
            for slot in range(16, 44)
        } if weeks else {}
        with patch("store.between", return_value=today), \
             patch("store.weekday_bands", return_value=(bands, weeks, 0.2)), \
             patch("store.forecast_for", return_value=forecast), \
             patch("store.earliest", return_value=reading(midnight - timedelta(days=30), 0)), \
             patch("app.fetch_reading", return_value=reading(now, 100)):
            return app.app.test_client().get("/api/day").get_json()

    def test_typical_absent_below_three_instances(self):
        self.assertIsNone(self._fetch(weeks=2)["typical"])

    def test_typical_present_once_enough_history_exists(self):
        typical = self._fetch(weeks=3)["typical"]

        self.assertIsNotNone(typical)
        self.assertEqual(typical["weeks"], 3)
        # [minuteOfDay, median, low, high]
        self.assertTrue(all(len(point) == 4 for point in typical["points"]))

    def test_typical_names_the_period_it_was_drawn_from(self):
        """A curve built from last spring must not pass as simply "8 past Mondays"."""
        with patch("store.period_of", return_value="instruction"):
            typical = self._fetch(weeks=3)["typical"]

        self.assertEqual(typical["period"], "instruction")

    def test_todays_line_is_the_forest_forecast_when_one_is_stored(self):
        forecast = {"by_hour": {hour: hour / 100 for hour in range(24)}, "model": "forest",
                    "made_at": datetime.now(TZ)}
        typical = self._fetch(weeks=3, forecast=forecast)["typical"]
        line = {point[0]: point[1] for point in typical["points"]}

        self.assertEqual(typical["source"], "forest")
        self.assertEqual((line[8 * 60 + 15], line[8 * 60 + 45], line[9 * 60 + 15]), (0.08, 0.08, 0.09),
                         "each half hour takes its hour's forecast")
        self.assertEqual({(point[2], point[3]) for point in typical["points"]}, {(0.3, 0.5)},
                         "the band is still the curve's")

    def test_without_a_forecast_the_curve_is_drawn(self):
        self.assertEqual(self._fetch(weeks=3)["typical"]["source"], "curve")

    def test_suggestions_rank_on_the_forecast_and_are_logged_as_the_forests(self):
        from types import SimpleNamespace
        forecast = {"by_hour": {hour: 0.25 for hour in range(24)}, "model": "forest",
                    "made_at": datetime.now(TZ)}
        start = datetime.now(TZ) + timedelta(hours=1)
        suggested = SimpleNamespace(refusal=None, windows=[SimpleNamespace(
            start=start, end=start + timedelta(hours=1), predicted_pct=0.25, spread=0.05, section="Evening")])
        seen, logged = {}, []

        def policy_spy(bands, *args, **kwargs):
            seen["medians"] = {band["median"] for band in bands.values()}
            return suggested

        with patch("policy.suggest_by_section", side_effect=policy_spy), \
             patch("store.log_prediction", side_effect=lambda *a, **k: logged.append(k.get("model")) or 1):
            self._fetch(weeks=3, forecast=forecast)

        self.assertEqual(seen["medians"], {0.25}, "suggestions are ranked on the forecast")
        self.assertEqual(logged, ["forest"])

    def test_typical_does_not_depend_on_today_having_data(self):
        """Just after midnight the typical curve is the only thing worth drawing"""
        day = self._fetch(weeks=3, today_samples=0)

        self.assertIsNotNone(day["typical"])
        self.assertEqual(day["samples"], [])

    def test_today_carries_a_live_reading_and_no_summary(self):
        day = self._fetch(weeks=0)

        self.assertTrue(day["isToday"])
        self.assertIsNotNone(day["live"])
        self.assertIsNone(day["summary"], "summary is for days already over")

    def test_no_suggestions_below_the_three_instance_gate(self):
        """weekday_bands returns bands from any number of instances, so the
        gate has to be applied here or a single past weekday becomes advice."""
        day = self._fetch(weeks=1)

        self.assertEqual(day["suggestions"]["windows"], [])
        self.assertIn("1 so far", day["suggestions"]["refusal"])

    def test_suggestions_absent_for_a_past_day(self):
        day = self._fetch(weeks=0)
        self.assertTrue(day["isToday"])
        self.assertIsNotNone(day["suggestions"])

    def test_samples_are_minute_count_capacity_triples(self):
        samples = self._fetch(weeks=0, today_samples=3)["samples"]

        self.assertEqual(len(samples), 3)
        for minute, count, capacity in samples:
            self.assertIsInstance(minute, int)
            self.assertEqual((count, capacity), (100, 150))


if __name__ == "__main__":
    unittest.main()
