import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import policy

TZ = ZoneInfo("America/Los_Angeles")


def outcome(hour, booked=True, went=None, day=1):
    start = datetime(2026, 9, day, hour, tzinfo=TZ)
    return {
        "window_start": start.isoformat(),
        "window_end": (start + timedelta(hours=1)).isoformat(),
        "predicted_pct": 0.3, "booked": booked, "went": went,
    }


class TallyTest(unittest.TestCase):
    def test_counts_land_in_the_right_part_of_day(self):
        stats = policy.outcomes_by_section(
            [outcome(9, day=1), outcome(14, day=2), outcome(21, day=3)], TZ
        )

        self.assertEqual(sorted(stats), ["Afternoon", "Evening", "Morning"])
        self.assertEqual(stats["Morning"]["shown"], 1)

    def test_unanswered_counts_as_shown_but_not_as_an_outcome(self):
        stats = policy.outcomes_by_section([outcome(9, went=None)], TZ)["Morning"]

        self.assertEqual(stats["shown"], 1)
        self.assertEqual(stats["answered"], 0, "never asked is not an outcome")
        self.assertEqual((stats["attended"], stats["skipped"]), (0, 0))

    def test_attendance_and_skipping_are_distinguished(self):
        stats = policy.outcomes_by_section(
            [outcome(9, went=True, day=1), outcome(9, went=False, day=2)], TZ
        )["Morning"]

        self.assertEqual((stats["attended"], stats["skipped"], stats["answered"]), (1, 1, 2))


class NoteTest(unittest.TestCase):
    """Notes annotate; they never remove a suggestion."""

    def note_for(self, answered, skipped):
        return policy.section_note({
            "shown": answered, "booked": answered, "answered": answered,
            "skipped": skipped, "attended": answered - skipped,
        })

    def test_silent_below_the_evidence_threshold(self):
        self.assertIsNone(
            self.note_for(answered=3, skipped=3),
            "three skips is not yet a pattern",
        )

    def test_reports_a_consistent_skip(self):
        self.assertIn("skipped 4 of 4", self.note_for(answered=4, skipped=4))

    def test_reports_a_consistent_attendance(self):
        self.assertIn("went to 4 of 4", self.note_for(answered=4, skipped=0))

    def test_silent_when_the_record_is_mixed(self):
        self.assertIsNone(
            self.note_for(answered=6, skipped=3),
            "half and half says nothing worth reporting",
        )

    def test_a_section_with_no_history_is_silent(self):
        self.assertIsNone(policy.section_note(None))


class NonSuppressionTest(unittest.TestCase):
    """Removing an option would destroy the evidence that would revise it."""

    def test_a_badly_performing_section_is_still_suggested(self):
        curve = {slot: {"median": 0.3, "low": 0.28, "high": 0.32} for slot in range(14, 46)}
        midnight = datetime(2026, 9, 8, tzinfo=TZ)
        from types import SimpleNamespace
        result = policy.suggest_by_section(
            curve, midnight, SimpleNamespace(opens=7 * 60, closes=23 * 60), 30
        )
        sections = {w.section for w in result.windows}

        # the tally is advisory only: suggest_by_section does not consult it
        self.assertIn("Evening", sections)


if __name__ == "__main__":
    unittest.main()
