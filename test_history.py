"""The import decides what history the curve learns from.

Its rules are tested against the shapes the real file showed: a feed that
froze on a small number after closing, a day that drifted far past capacity,
and a day that genuinely went over capacity and has to survive.
"""

import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import testing
import store
from density import Reading
from tools import backfill_history as backfill

TZ = ZoneInfo("America/Los_Angeles")
TERM_DAY = date(2025, 10, 14)   # a Tuesday in term


def local(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), TZ).astimezone(timezone.utc)


def ordinary_day(day, peak=120):
    """06:00 to 02:50 the next morning in ten-minute rows: empty at opening,
    rising to `peak` and back, empty from 23:00 through the nightly reset."""
    rising = [round(peak * (step + 1) / 48) for step in range(48)]
    counts = [0] * 6 + rising + rising[::-1] + [0] * 24
    start = local(day, 6)
    return [backfill.Row(start + timedelta(minutes=10 * i), count)
            for i, count in enumerate(counts)]


def with_counts(rows, changes):
    """The same rows with the counts at some instants replaced."""
    return [backfill.Row(r.observed_at, changes.get(r.observed_at, r.count)) for r in rows]


class ParseTest(unittest.TestCase):
    def test_reads_the_published_format(self):
        [row] = backfill.parse(["2025-10-14T22:40:00+00:00,87,80,95"])
        self.assertEqual(row, backfill.Row(datetime(2025, 10, 14, 22, 40, tzinfo=timezone.utc), 87))

    def test_a_row_off_the_ten_minute_grid_is_refused_by_number(self):
        lines = ["2025-10-14T22:40:00+00:00,87,80,95", "2025-10-14T22:45:00+00:00,88,80,95"]
        with self.assertRaisesRegex(ValueError, "row 2"):
            backfill.parse(lines)

    def test_a_negative_count_is_refused(self):
        with self.assertRaisesRegex(ValueError, "negative"):
            backfill.parse(["2025-10-14T22:40:00+00:00,-3,0,0"])

    def test_a_short_row_is_refused(self):
        with self.assertRaisesRegex(ValueError, "4 columns"):
            backfill.parse(["2025-10-14T22:40:00+00:00,87"])

    def test_a_timestamp_without_an_offset_is_refused(self):
        with self.assertRaisesRegex(ValueError, "offset"):
            backfill.parse(["2025-10-14T22:40:00,87,80,95"])


class CleanTest(unittest.TestCase):
    def test_an_ordinary_day_is_kept_whole(self):
        rows = ordinary_day(TERM_DAY)
        report = backfill.clean(rows)

        self.assertEqual(report.kept, rows)
        self.assertEqual(report.excluded_days, {})
        self.assertEqual(report.frozen, [], "hours of zeros overnight are an empty gym")

    def test_a_day_genuinely_over_capacity_is_kept(self):
        """2026-09-03 really did peak at 157 of 150; the ceiling must not catch it."""
        rows = ordinary_day(TERM_DAY, peak=157)
        self.assertEqual(backfill.clean(rows).kept, rows)

    def test_a_day_past_the_ceiling_is_dropped_whole(self):
        report = backfill.clean(ordinary_day(TERM_DAY, peak=181))

        self.assertFalse(any(backfill.local_day(r) == TERM_DAY for r in report.kept))
        self.assertIn("181", report.excluded_days[TERM_DAY])

    def test_a_day_still_counting_people_after_closing_is_dropped_whole(self):
        after_closing = local(TERM_DAY + timedelta(days=1), 0, 30)
        report = backfill.clean(with_counts(ordinary_day(TERM_DAY), {after_closing: 21}))

        self.assertIn(TERM_DAY, report.excluded_days)
        self.assertFalse(any(backfill.local_day(r) == TERM_DAY for r in report.kept))

    def test_twenty_left_after_closing_is_tolerated(self):
        after_closing = local(TERM_DAY + timedelta(days=1), 0, 30)
        report = backfill.clean(with_counts(ordinary_day(TERM_DAY), {after_closing: 20}))

        self.assertEqual(report.excluded_days, {})

    def test_a_frozen_stretch_is_dropped_and_the_rest_of_the_day_kept(self):
        """The commonest fault in the real file: a small leftover held from
        late evening until the nightly reset."""
        frozen_from = local(TERM_DAY, 21, 10)
        frozen_to = local(TERM_DAY + timedelta(days=1), 1, 50)
        rows = [backfill.Row(r.observed_at, 16) if frozen_from <= r.observed_at <= frozen_to else r
                for r in ordinary_day(TERM_DAY)]
        report = backfill.clean(rows)

        self.assertEqual(len(report.frozen), 1)
        self.assertEqual(report.kept,
                         [r for r in rows if not frozen_from <= r.observed_at <= frozen_to])
        self.assertEqual(report.excluded_days, {}, "16 at 00:30 is under the leftover limit")

    def test_five_unchanged_readings_are_not_a_freeze(self):
        start = local(TERM_DAY, 12)
        steady = {start + timedelta(minutes=10 * i): 60 for i in range(5)}
        report = backfill.clean(with_counts(ordinary_day(TERM_DAY), steady))

        self.assertEqual(report.frozen, [])

    def test_six_unchanged_readings_are_a_freeze(self):
        start = local(TERM_DAY, 12)
        steady = {start + timedelta(minutes=10 * i): 60 for i in range(6)}
        report = backfill.clean(with_counts(ordinary_day(TERM_DAY), steady))

        self.assertEqual(len(report.frozen), 1)
        self.assertEqual(report.frozen_rows, 6)

    def test_nothing_from_the_pandemic_months_is_kept(self):
        report = backfill.clean(ordinary_day(date(2021, 9, 5)) + ordinary_day(date(2021, 9, 6)))

        self.assertEqual(min(backfill.local_day(r) for r in report.kept), date(2021, 9, 6))
        self.assertGreater(report.before_cutoff, 0)

    def test_live_collection_wins_from_its_first_reading(self):
        first_live = local(TERM_DAY, 12)
        report = backfill.clean(ordinary_day(TERM_DAY), first_live=first_live)

        self.assertTrue(report.kept)
        self.assertTrue(all(r.observed_at < first_live for r in report.kept))
        self.assertGreater(report.after_live, 0)


class LoadTest(testing.DatabaseTest):
    def history_rows(self):
        return self.connection.execute("SELECT COUNT(*) AS n FROM history").fetchone()["n"]

    def test_loading_twice_adds_nothing_the_second_time(self):
        rows = ordinary_day(TERM_DAY)

        self.assertEqual(backfill.load(self.connection, rows), len(rows))
        self.assertEqual(backfill.load(self.connection, rows), 0)
        self.assertEqual(self.history_rows(), len(rows))

    def test_replace_starts_over(self):
        backfill.load(self.connection, ordinary_day(TERM_DAY))
        backfill.load(self.connection, ordinary_day(TERM_DAY + timedelta(days=7))[:10], replace=True)

        self.assertEqual(self.history_rows(), 10)


class OccupancyViewTest(testing.DatabaseTest):
    """Analytics read live readings and history together, never overlapping."""

    def day(self):
        return store.occupancy_between(self.connection, local(TERM_DAY, 0), local(TERM_DAY, 23))

    def test_history_only_fills_the_time_before_live_collection(self):
        backfill.load(self.connection, [backfill.Row(local(TERM_DAY, 10), 40),
                                        backfill.Row(local(TERM_DAY, 14), 90)])
        store.save(self.connection, Reading(70, 150, local(TERM_DAY, 12)))

        self.assertEqual([(r.observed_at, r.count) for r in self.day()],
                         [(local(TERM_DAY, 10), 40), (local(TERM_DAY, 12), 70)])

    def test_history_is_measured_against_todays_capacity(self):
        backfill.load(self.connection, [backfill.Row(local(TERM_DAY, 10), 75)])
        [reading] = self.day()

        self.assertEqual(reading.capacity, 150)

    def test_everything_that_reads_live_data_still_sees_only_live_data(self):
        backfill.load(self.connection, [backfill.Row(local(TERM_DAY, 10), 40)])

        self.assertEqual(store.between(self.connection, local(TERM_DAY, 0), local(TERM_DAY, 23)), [])
        self.assertIsNone(store.earliest(self.connection))


class CalendarTest(unittest.TestCase):
    """Transcribed by hand from the Registrar's PDFs, so it is checked here."""

    @classmethod
    def setUpClass(cls):
        cls.days = store.calendar_days_from()

    def calendar(self, text):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "calendar.csv"
        path.write_text(text)
        return path

    def test_every_day_of_the_history_has_a_period(self):
        day, missing = date(2021, 9, 6), []
        while day <= date(2027, 5, 31):
            if day not in self.days:
                missing.append(day)
            day += timedelta(days=1)

        self.assertEqual(missing, [])

    def test_periods_land_where_the_registrar_puts_them(self):
        expected = {
            date(2021, 10, 12): "instruction",
            date(2021, 11, 11): "holiday",     # Veterans Day, inside term
            date(2021, 11, 24): "break",       # the Wednesday before Thanksgiving
            date(2021, 12, 8): "rrr",
            date(2021, 12, 15): "finals",
            date(2022, 1, 4): "break",
            date(2022, 3, 22): "break",        # spring recess
            date(2022, 7, 12): "summer",
            date(2026, 9, 7): "holiday",       # Labor Day
            date(2026, 9, 14): "instruction",
        }
        self.assertEqual({day: self.days[day][0] for day in expected}, expected)

    def test_a_holiday_outranks_the_term_around_it(self):
        path = self.calendar("start,end,kind,label\n"
                             "2026-08-19,2026-12-06,instruction,Fall 2026\n"
                             "2026-09-07,2026-09-07,holiday,Labor Day\n")

        self.assertEqual(store.calendar_days_from(path)[date(2026, 9, 7)], ("holiday", "Labor Day"))

    def test_overlapping_kinds_of_equal_rank_are_refused(self):
        path = self.calendar("start,end,kind,label\n"
                             "2026-08-01,2026-08-31,summer,Summer 2026\n"
                             "2026-08-19,2026-12-06,instruction,Fall 2026\n")

        with self.assertRaisesRegex(ValueError, "both summer and instruction"):
            store.calendar_days_from(path)

    def test_an_unknown_kind_is_refused(self):
        path = self.calendar("start,end,kind,label\n2026-08-19,2026-12-06,semester,Fall 2026\n")

        with self.assertRaisesRegex(ValueError, "unknown kind"):
            store.calendar_days_from(path)


class CalendarSyncTest(testing.DatabaseTest):
    def test_the_checked_in_calendar_reaches_the_database(self):
        store.sync_calendar(self.connection)

        self.assertEqual(store.period_of(self.connection, date(2026, 9, 14)), "instruction")
        self.assertIsNone(store.period_of(self.connection, date(2019, 1, 1)))


if __name__ == "__main__":
    unittest.main()
