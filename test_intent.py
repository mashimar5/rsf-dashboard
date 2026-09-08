import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import intent
import policy

TZ = ZoneInfo("America/Los_Angeles")
MIDNIGHT = datetime(2026, 9, 8, tzinfo=TZ)
BUCKET = 30


class ValidationTest(unittest.TestCase):
    """A schema guarantees shape, not sense. These are the sense checks."""

    def make(self, **fields):
        return intent.Preferences(summary="…", **fields)

    def test_an_absurd_session_length_is_clamped(self):
        self.assertEqual(self.make(session_minutes=600).session_minutes, 180)
        self.assertEqual(self.make(session_minutes=1).session_minutes, 20)

    def test_an_impossible_hour_is_dropped_rather_than_clamped(self):
        """Clamping 99 to 23 would invent a preference nobody stated."""
        self.assertIsNone(self.make(earliest_hour=99).earliest_hour)
        self.assertIsNone(self.make(latest_hour=-3).latest_hour)
        self.assertEqual(self.make(earliest_hour=7).earliest_hour, 7)

    def test_a_percentage_written_as_a_whole_number_is_read_correctly(self):
        self.assertAlmostEqual(self.make(max_crowding_pct=60).max_crowding_pct, 0.6)
        self.assertAlmostEqual(self.make(max_crowding_pct=0.6).max_crowding_pct, 0.6)
        self.assertIsNone(self.make(max_crowding_pct=0).max_crowding_pct)

    def test_a_summary_alone_counts_as_nothing_extracted(self):
        self.assertTrue(self.make().is_empty())
        self.assertFalse(self.make(session_minutes=60).is_empty())


class ParseTest(unittest.TestCase):
    """The model is mocked: tests must not spend money or need a network."""

    def client_returning(self, parsed):
        client = Mock()
        client.messages.parse.return_value = SimpleNamespace(parsed_output=parsed)
        return client

    def test_blank_input_never_reaches_the_model(self):
        client = self.client_returning(None)
        self.assertIsNone(intent.parse("   ", client=client))
        client.messages.parse.assert_not_called()

    def test_a_model_failure_returns_none_rather_than_raising(self):
        """A misread sentence must leave existing settings alone, not 500."""
        client = Mock()
        client.messages.parse.side_effect = RuntimeError("api down")

        self.assertIsNone(intent.parse("mornings please", client=client))

    def test_an_empty_extraction_is_treated_as_a_failure(self):
        parsed = intent.Preferences(summary="I understood nothing at all")
        self.assertIsNone(intent.parse("hello", client=self.client_returning(parsed)))

    def test_a_usable_extraction_comes_back(self):
        parsed = intent.Preferences(session_minutes=90, earliest_hour=7, summary="90 min, from 7am")
        result = intent.parse("90 minute sessions, not before 7", client=self.client_returning(parsed))

        self.assertEqual(result.session_minutes, 90)
        self.assertEqual(result.earliest_hour, 7)


class PolicyHonoursPreferencesTest(unittest.TestCase):
    """What the model extracts has to actually change the suggestions."""

    def bands(self, value=0.3):
        return {slot: {"median": value, "low": value - 0.02, "high": value + 0.02,
                       "q1": value - 0.01, "q3": value + 0.01, "n": 4}
                for slot in range(14, 46)}

    def hours(self):
        return SimpleNamespace(opens=7 * 60, closes=23 * 60)

    def suggest(self, preferences=None, busy=()):
        return policy.suggest_by_section(self.bands(), MIDNIGHT, self.hours(), BUCKET,
                                         busy=busy, preferences=preferences)

    def test_an_earliest_hour_removes_earlier_windows(self):
        result = self.suggest({"earliest_hour": 10})

        self.assertTrue(result.windows)
        for window in result.windows:
            self.assertGreaterEqual(window.start.hour, 10)

    def test_a_latest_hour_removes_later_starts(self):
        result = self.suggest({"latest_hour": 12})

        for window in result.windows:
            self.assertLessEqual(window.start.hour, 12)

    def test_a_session_length_changes_the_window(self):
        result = self.suggest({"session_minutes": 90})

        for window in result.windows:
            self.assertEqual((window.end - window.start), timedelta(minutes=90))

    def test_a_travel_buffer_widens_calendar_conflicts(self):
        """Tested on every candidate, not the three that get chosen: only one
        window per section is suggested, so a flat curve makes which one
        arbitrary."""
        busy = [(MIDNIGHT.replace(hour=14), MIDNIGHT.replace(hour=15))]
        eligible = lambda buffer: {
            w.start.strftime("%H:%M")
            for w in policy.candidate_windows(
                self.bands(), MIDNIGHT, self.hours(), BUCKET,
                busy=busy, travel_buffer=buffer,
            )
        }
        without, with_buffer = eligible(0), eligible(60)

        self.assertIn("13:00", without, "1pm-2pm butts against the meeting but is legal")
        self.assertNotIn("13:00", with_buffer, "an hour's clearance rules it out")
        self.assertLess(len(with_buffer), len(without))

    def test_a_stated_ceiling_overrides_the_default(self):
        busy_day = {slot: {"median": 0.7, "low": 0.68, "high": 0.72,
                           "q1": 0.69, "q3": 0.71, "n": 4} for slot in range(14, 46)}
        result = policy.suggest_by_section(busy_day, MIDNIGHT, self.hours(), BUCKET,
                                           preferences={"max_crowding_pct": 0.5})

        self.assertEqual(result.windows, [], "70% exceeds a stated 50% ceiling")
        self.assertIn("quiet enough", result.refusal)

    def test_preferences_cannot_widen_beyond_opening_hours(self):
        """A stated 5am start does not open the gym at 5am."""
        result = self.suggest({"earliest_hour": 5})

        for window in result.windows:
            self.assertGreaterEqual(window.start.hour, 7)


if __name__ == "__main__":
    unittest.main()
