import json
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import hours

FIXTURE = (Path(__file__).parent / "tests" / "rsf_hours_fixture.html").read_text()


class ParsingTest(unittest.TestCase):
    def test_reads_times_into_minutes(self):
        self.assertEqual(hours.minutes_of("7 a.m.–8 p.m."), (420, 1200))
        self.assertEqual(hours.minutes_of("8:30 a.m.–11 p.m."), (510, 1380))
        self.assertEqual(hours.minutes_of("12 p.m.–12 a.m."), (720, 0))

    def test_no_times_means_closed(self):
        self.assertIsNone(hours.minutes_of("CLOSED for Caltopia"))
        self.assertIsNone(hours.minutes_of("CLOSED"))

    def test_expands_weekday_ranges(self):
        self.assertEqual(hours.weekdays_in("Monday–Friday"), {0, 1, 2, 3, 4})
        self.assertEqual(hours.weekdays_in("Saturday"), {5})
        self.assertEqual(hours.weekdays_in("Sunday"), {6})


class TableSelectionTest(unittest.TestCase):
    def hours_on(self, day):
        return hours.hours_for(day, FIXTURE)

    def test_specific_date_beats_everything(self):
        # 8/23 is a Sunday that the standing table would call 8am-11pm
        result = self.hours_on(date(2026, 8, 23))
        self.assertEqual(result.text, "CLOSED for Caltopia")
        self.assertIsNone(result.opens)

    def test_dated_event_with_real_times(self):
        result = self.hours_on(date(2026, 8, 26))
        self.assertEqual((result.opens, result.closes), (420, 1380))

    def test_seasonal_range_applies_inside_its_window(self):
        # 7/4/2026 is a Saturday inside Summer 2026 (5/16-8/22)
        result = self.hours_on(date(2026, 7, 4))
        self.assertEqual((result.opens, result.closes), (480, 1080))
        self.assertIn("Summer", result.source)

    def test_falls_through_to_standing_schedule_outside_any_window(self):
        # 8/30 is past both the summer window and the event dates
        result = self.hours_on(date(2026, 8, 30))
        self.assertEqual((result.opens, result.closes), (480, 1380))
        self.assertNotIn("Summer", result.source)


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(f"/tmp/rsf-hours-test-{id(self)}.json")
        patcher = patch.object(hours, "CACHE_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: self.path.unlink(missing_ok=True))

    def test_fresh_cache_is_reused_without_fetching(self):
        self.path.write_text(json.dumps({
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "markup": "<table><tr><td>cached</td></tr></table>",
        }))
        with patch.object(hours, "_fetch_markup") as fetch:
            self.assertIn("cached", hours.cached_markup())
            fetch.assert_not_called()

    def test_stale_cache_is_kept_when_the_site_is_unreachable(self):
        old = datetime.now(timezone.utc) - timedelta(days=3)
        self.path.write_text(json.dumps({
            "fetched_at": old.isoformat(),
            "markup": "<table><tr><td>stale</td></tr></table>",
        }))
        with patch.object(hours, "_fetch_markup", side_effect=OSError("down")):
            # yesterday's schedule beats no schedule
            self.assertIn("stale", hours.cached_markup())

    def test_returns_none_when_there_is_no_cache_and_no_network(self):
        with patch.object(hours, "_fetch_markup", side_effect=OSError("down")):
            self.assertIsNone(hours.cached_markup())


if __name__ == "__main__":
    unittest.main()
