import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import policy

TZ = ZoneInfo("America/Los_Angeles")
BUCKET = 30
MIDNIGHT = datetime(2026, 9, 7, tzinfo=TZ)


def hours(open_hour, close_hour):
    return SimpleNamespace(opens=open_hour * 60, closes=close_hour * 60)


def bands(**by_hour):
    """bands(h8=0.2, h9=0.9) -> both 30-minute buckets of 8am at 0.2, etc."""
    out = {}
    for key, value in by_hour.items():
        hour = int(key[1:])
        for slot in (hour * 2, hour * 2 + 1):
            out[slot] = {"median": value, "low": value - 0.02, "high": value + 0.02}
    return out


def flat(open_hour, close_hour, value=0.5):
    return bands(**{f"h{h}": value for h in range(open_hour, close_hour)})


def at(hour, minute=0):
    return MIDNIGHT.replace(hour=hour, minute=minute)


class ClosingTimeTest(unittest.TestCase):
    """The bug this policy exists to fix: a session must be able to finish."""

    def test_a_window_cannot_run_past_closing(self):
        result = policy.suggest(flat(8, 23), MIDNIGHT, hours(8, 23), BUCKET)
        latest = max(w.end for w in result.windows)

        self.assertLessEqual(
            latest, at(23) - timedelta(minutes=policy.WIND_DOWN_MINUTES),
            "must finish with time to leave, not at the moment the doors lock",
        )

    def test_the_quiet_closing_stretch_is_not_suggested(self):
        # exactly the real shape: busy all day, empties right before an 11pm close
        curve = flat(8, 21, 0.7)
        curve.update(bands(h21=0.1, h22=0.05))
        result = policy.suggest(curve, MIDNIGHT, hours(8, 23), BUCKET)

        self.assertTrue(result.windows)
        chosen = result.windows[0]
        self.assertLessEqual(chosen.end, at(22, 45))

    def test_opening_is_not_trimmed_because_a_session_fits(self):
        curve = flat(7, 22, 0.8)
        curve.update(bands(h7=0.05))          # empty right at opening
        result = policy.suggest(curve, MIDNIGHT, hours(7, 22), BUCKET)

        self.assertEqual(result.windows[0].start, at(7),
                         "an empty gym at opening is a real recommendation")


class RankingTest(unittest.TestCase):
    def test_suggestions_do_not_overlap(self):
        result = policy.suggest(flat(8, 22, 0.3), MIDNIGHT, hours(8, 22), BUCKET)

        self.assertEqual(len(result.windows), policy.MAX_SUGGESTIONS)
        for earlier, later in zip(result.windows, result.windows[1:]):
            self.assertLessEqual(earlier.end, later.start,
                                 "three names for one slot is not three options")

    def test_quietest_windows_win(self):
        """Selection is by quietness; presentation is chronological, so the
        best window is not necessarily the first one listed."""
        curve = flat(8, 22, 0.8)
        curve.update(bands(h13=0.1, h14=0.1))
        result = policy.suggest(curve, MIDNIGHT, hours(8, 22), BUCKET)

        best = min(result.windows, key=lambda w: w.predicted_pct)
        self.assertEqual(best.start, at(13))
        self.assertAlmostEqual(best.predicted_pct, 0.1)
        self.assertTrue(all(w.predicted_pct <= 0.8 for w in result.windows))

    def test_windows_are_returned_in_time_order(self):
        result = policy.suggest(flat(8, 22, 0.3), MIDNIGHT, hours(8, 22), BUCKET)
        starts = [w.start for w in result.windows]

        self.assertEqual(starts, sorted(starts))


class RefusalTest(unittest.TestCase):
    def test_refuses_when_everything_is_above_the_ceiling(self):
        result = policy.suggest(flat(8, 22, 0.95), MIDNIGHT, hours(8, 22), BUCKET)

        self.assertEqual(result.windows, [])
        self.assertIn("busier", result.refusal)

    def test_refuses_on_a_closed_day(self):
        result = policy.suggest(flat(8, 22), MIDNIGHT,
                                SimpleNamespace(opens=None, closes=None), BUCKET)

        self.assertEqual(result.refusal, "closed")

    def test_refuses_without_a_forecast(self):
        result = policy.suggest({}, MIDNIGHT, hours(8, 22), BUCKET)

        self.assertIn("no forecast", result.refusal)

    def test_refuses_when_the_calendar_leaves_no_room(self):
        busy = [(at(0), at(23, 59))]
        result = policy.suggest(flat(8, 22), MIDNIGHT, hours(8, 22), BUCKET, busy=busy)

        self.assertEqual(result.windows, [])
        self.assertIn("no free window", result.refusal)


class CalendarTest(unittest.TestCase):
    """Nothing supplies busy intervals yet, so the calendar work stays a
    data-supply problem rather than a redesign."""

    def test_busy_intervals_remove_windows(self):
        curve = flat(8, 22, 0.8)
        curve.update(bands(h13=0.1, h14=0.1))     # the quietest stretch
        busy = [(at(13), at(15))]                 # ...which is booked
        result = policy.suggest(curve, MIDNIGHT, hours(8, 22), BUCKET, busy=busy)

        for window in result.windows:
            self.assertFalse(policy.overlaps(window.start, window.end, at(13), at(15)))

    def test_a_window_touching_a_meeting_edge_is_still_allowed(self):
        busy = [(at(14), at(15))]
        result = policy.suggest(flat(8, 22, 0.3), MIDNIGHT, hours(8, 22), BUCKET, busy=busy)
        ends_at_meeting = [w for w in result.windows if w.end == at(14)]

        self.assertFalse(
            any(policy.overlaps(w.start, w.end, at(14), at(15)) for w in ends_at_meeting)
        )


class ConfidenceTest(unittest.TestCase):
    def test_each_window_carries_the_spread_of_its_own_buckets(self):
        curve = flat(8, 22, 0.5)
        curve[28] = {"median": 0.5, "low": 0.1, "high": 0.9}     # 2pm is volatile
        curve[29] = {"median": 0.5, "low": 0.1, "high": 0.9}
        result = policy.suggest(curve, MIDNIGHT, hours(8, 22), BUCKET, limit=99)
        volatile = next(w for w in result.windows if w.start == at(14))
        steady = next(w for w in result.windows if w.start == at(9))

        self.assertGreater(volatile.spread, steady.spread)


if __name__ == "__main__":
    unittest.main()
