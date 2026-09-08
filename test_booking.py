import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import app
import store

TZ = ZoneInfo("America/Los_Angeles")


class BookingStoreTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(f"/tmp/rsf-booking-{id(self)}.db")
        self.connection = store.connect(self.path)
        self.addCleanup(self.connection.close)
        self.addCleanup(lambda: [
            self.path.with_name(self.path.name + s).unlink(missing_ok=True)
            for s in ("", "-wal", "-shm")
        ])
        self.day = date(2026, 9, 8)
        self.start = datetime(2026, 9, 8, 14, tzinfo=TZ)

    def save(self, event_id, hour=14):
        start = self.start.replace(hour=hour)
        store.save_booking(self.connection, self.day, event_id,
                           start, start + timedelta(hours=1), 0.35)

    def test_a_second_booking_replaces_rather_than_duplicating(self):
        self.save("event-one", hour=14)
        self.save("event-two", hour=18)
        rows = self.connection.execute("SELECT * FROM bookings").fetchall()

        self.assertEqual(len(rows), 1, "one booking per day")
        self.assertEqual(store.booking_on(self.connection, self.day)["event_id"], "event-two")

    def test_cancelling_removes_it(self):
        self.save("event-one")
        store.delete_booking(self.connection, self.day)

        self.assertIsNone(store.booking_on(self.connection, self.day))

    def test_the_calendar_id_is_remembered(self):
        self.assertIsNone(store.get_state(self.connection, "app_calendar_id"))
        store.set_state(self.connection, "app_calendar_id", "cal-123")

        self.assertEqual(store.get_state(self.connection, "app_calendar_id"), "cal-123")


class BookingEndpointTest(unittest.TestCase):
    """The endpoint acts on the policy's suggestion, never on arbitrary input."""

    def setUp(self):
        self.start = datetime.now(TZ).replace(hour=14, minute=0, second=0, microsecond=0)
        self.window = {
            "start": self.start.isoformat(),
            "end": (self.start + timedelta(hours=1)).isoformat(),
            "predictedPct": 0.35, "spread": 0.05, "section": "Afternoon",
        }

    def client_signed_in(self):
        client = app.app.test_client()
        with client.session_transaction() as session:
            session["email"] = "andrewsapshin@gmail.com"
        return client

    def test_refuses_a_window_the_policy_did_not_suggest(self):
        view = {"suggestions": {"windows": [self.window], "refusal": None}}
        with patch.dict(app.app.config, {"SECRET_KEY": "k"}), \
             patch.object(app, "day_view", return_value=view):
            response = self.client_signed_in().post(
                "/api/book", json={"start": "2026-01-01T03:00:00-08:00"}
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer suggested", response.get_json()["error"])

    def test_requires_sign_in(self):
        with patch.dict(app.app.config, {"SECRET_KEY": "k"}):
            response = app.app.test_client().post("/api/book", json={"start": self.window["start"]})

        self.assertEqual(response.status_code, 401)

    def test_books_a_suggested_window(self):
        view = {"suggestions": {"windows": [self.window], "refusal": None}}
        with patch.dict(app.app.config, {"SECRET_KEY": "k"}), \
             patch.object(app, "day_view", return_value=view), \
             patch.object(app, "access_token_for", return_value="ya29"), \
             patch.object(app, "app_calendar_id", return_value="cal-1"), \
             patch.object(app.google_auth, "create_event", return_value="evt-1") as create, \
             patch.object(app.store, "booking_on", return_value=None), \
             patch.object(app.store, "save_booking") as saved:
            response = self.client_signed_in().post(
                "/api/book", json={"start": self.window["start"]}
            )

        self.assertEqual(response.status_code, 200)
        create.assert_called_once()
        saved.assert_called_once()

    def test_rebooking_deletes_the_previous_event_first(self):
        view = {"suggestions": {"windows": [self.window], "refusal": None}}
        previous = {"event_id": "old-event"}
        with patch.dict(app.app.config, {"SECRET_KEY": "k"}), \
             patch.object(app, "day_view", return_value=view), \
             patch.object(app, "access_token_for", return_value="ya29"), \
             patch.object(app, "app_calendar_id", return_value="cal-1"), \
             patch.object(app.google_auth, "create_event", return_value="evt-2"), \
             patch.object(app.google_auth, "delete_event") as delete, \
             patch.object(app.store, "booking_on", return_value=previous), \
             patch.object(app.store, "save_booking"):
            self.client_signed_in().post("/api/book", json={"start": self.window["start"]})

        delete.assert_called_once_with("ya29", "cal-1", "old-event")

    def test_a_calendar_failure_does_not_leave_a_phantom_booking(self):
        view = {"suggestions": {"windows": [self.window], "refusal": None}}
        with patch.dict(app.app.config, {"SECRET_KEY": "k"}), \
             patch.object(app, "day_view", return_value=view), \
             patch.object(app, "access_token_for", return_value="ya29"), \
             patch.object(app, "app_calendar_id", return_value="cal-1"), \
             patch.object(app.google_auth, "create_event", side_effect=OSError("down")), \
             patch.object(app.store, "booking_on", return_value=None), \
             patch.object(app.store, "save_booking") as saved:
            response = self.client_signed_in().post(
                "/api/book", json={"start": self.window["start"]}
            )

        self.assertEqual(response.status_code, 502)
        saved.assert_not_called()


class CalendarIdTest(unittest.TestCase):
    def test_the_calendar_is_created_once_and_reused(self):
        connection = store.connect(Path(f"/tmp/rsf-cal-{id(self)}.db"))
        self.addCleanup(connection.close)
        with patch.object(app.google_auth, "create_calendar", return_value="cal-new") as create:
            first = app.app_calendar_id("ya29", connection)
            second = app.app_calendar_id("ya29", connection)

        self.assertEqual((first, second), ("cal-new", "cal-new"))
        create.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class PredictionLoggingTest(unittest.TestCase):
    """Suggestions are recorded as shown, once, however often the page renders."""

    def setUp(self):
        self.path = Path(f"/tmp/rsf-predlog-{id(self)}.db")
        self.connection = store.connect(self.path)
        self.addCleanup(self.connection.close)
        self.addCleanup(lambda: [
            self.path.with_name(self.path.name + s).unlink(missing_ok=True)
            for s in ("", "-wal", "-shm")
        ])
        self.day = date(2026, 9, 8)
        self.start = datetime(2026, 9, 8, 14, tzinfo=TZ)

    def log(self, hour=14, pct=0.35):
        start = self.start.replace(hour=hour)
        return store.log_prediction(self.connection, self.day, start,
                                    start + timedelta(hours=1), pct, 3, 0.1)

    def test_logging_the_same_window_twice_makes_one_row(self):
        first, second = self.log(), self.log()

        self.assertEqual(first, second, "same row id returned both times")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM predictions").fetchone()[0], 1
        )

    def test_different_windows_on_a_day_are_separate_rows(self):
        self.log(hour=9)
        self.log(hour=14)

        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM predictions").fetchone()[0], 2
        )

    def test_confidence_at_the_time_is_kept(self):
        row_id = self.log(pct=0.42)
        row = self.connection.execute(
            "SELECT predicted_pct, basis_weeks, basis_spread FROM predictions WHERE id = ?",
            (row_id,),
        ).fetchone()

        self.assertAlmostEqual(row["predicted_pct"], 0.42)
        self.assertEqual(row["basis_weeks"], 3)

    def test_unanswered_feedback_is_none_not_false(self):
        row_id = self.log()

        self.assertIsNone(store.feedback_for(self.connection, row_id),
                          "never asked is not the same as answered no")

    def test_answering_is_recorded_and_replaceable(self):
        row_id = self.log()
        store.record_feedback(self.connection, row_id, went=False)
        self.assertIs(store.feedback_for(self.connection, row_id), False)

        store.record_feedback(self.connection, row_id, went=True)
        self.assertIs(store.feedback_for(self.connection, row_id), True)


class FeedbackPromptTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(f"/tmp/rsf-prompt-{id(self)}.db")
        self.connection = store.connect(self.path)
        self.addCleanup(self.connection.close)
        self.addCleanup(lambda: [
            self.path.with_name(self.path.name + s).unlink(missing_ok=True)
            for s in ("", "-wal", "-shm")
        ])
        self.day = date(2026, 9, 7)
        self.start = datetime(2026, 9, 7, 14, tzinfo=TZ)

    def book(self, with_prediction=True):
        prediction_id = None
        if with_prediction:
            prediction_id = store.log_prediction(
                self.connection, self.day, self.start,
                self.start + timedelta(hours=1), 0.3, 3, 0.1,
            )
        store.save_booking(self.connection, self.day, "evt-1", self.start,
                           self.start + timedelta(hours=1), 0.3, prediction_id)
        return prediction_id

    def test_no_prompt_for_today(self):
        self.book()
        self.assertIsNone(app.feedback_prompt(self.connection, self.day, is_today=True))

    def test_no_prompt_without_a_booking(self):
        self.assertIsNone(app.feedback_prompt(self.connection, self.day, is_today=False))

    def test_prompt_for_a_past_booked_day(self):
        self.book()
        prompt = app.feedback_prompt(self.connection, self.day, is_today=False)

        self.assertIsNotNone(prompt)
        self.assertIsNone(prompt["answered"], "unanswered until asked")

    def test_prompt_reflects_the_answer_once_given(self):
        prediction_id = self.book()
        store.record_feedback(self.connection, prediction_id, went=True)
        prompt = app.feedback_prompt(self.connection, self.day, is_today=False)

        self.assertIs(prompt["answered"], True)
