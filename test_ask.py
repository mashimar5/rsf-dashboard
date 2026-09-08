import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import app
import ask
import store
import testing
from density import Reading

TZ = ZoneInfo("America/Los_Angeles")


def call(tool, **kwargs):
    """@beta_tool wraps the function; reach the callable underneath."""
    fn = getattr(tool, "_func", None) or getattr(tool, "func", None) or tool
    return json.loads(fn(**kwargs))


class ToolTest(testing.DatabaseTest):
    """The model can only reach the data through these, so they are the
    security boundary as well as the interface."""

    def seed(self, day, hour, count):
        store.save(self.connection, Reading(
            count, 150, datetime(day.year, day.month, day.day, hour, tzinfo=TZ)))

    def test_bad_dates_are_refused_not_raised(self):
        self.assertIn("error", call(ask.occupancy_stats,
                                    start_date="yesterday", end_date="2026-09-08"))

    def test_an_unknown_weekday_is_refused(self):
        result = call(ask.occupancy_stats, start_date="2026-09-01",
                      end_date="2026-09-08", weekday="Blursday")
        self.assertIn("error", result)

    def test_stats_narrow_by_weekday_and_hour(self):
        from datetime import date
        self.seed(date(2026, 9, 7), 18, 120)   # a Monday evening
        self.seed(date(2026, 9, 8), 18, 30)    # a Tuesday evening

        monday = call(ask.occupancy_stats, start_date="2026-09-01",
                      end_date="2026-09-30", weekday="Monday", start_hour=17, end_hour=20)
        self.assertEqual(monday["samples"], 1)
        self.assertAlmostEqual(monday["mean_pct"], 0.8, places=2)

    def test_an_empty_slice_says_so_rather_than_dividing_by_zero(self):
        result = call(ask.occupancy_stats, start_date="2020-01-01", end_date="2020-01-02")
        self.assertEqual(result["samples"], 0)

    def test_the_curve_refuses_below_three_instances(self):
        from datetime import date
        self.seed(date(2026, 9, 7), 9, 90)
        result = call(ask.typical_weekday_curve, weekday="Monday")

        self.assertLess(result["instances"], 3)
        self.assertIn("note", result)

    def test_sessions_reports_nothing_when_nothing_is_booked(self):
        self.assertEqual(call(ask.my_sessions)["sessions"], [])


class RateLimitTest(unittest.TestCase):
    def setUp(self):
        ask._recent.clear()
        self.addCleanup(ask._recent.clear)

    def test_the_limit_eventually_refuses(self):
        allowed = sum(1 for _ in range(ask.RATE_LIMIT_PER_HOUR) if ask.within_rate_limit())

        self.assertEqual(allowed, ask.RATE_LIMIT_PER_HOUR)
        self.assertFalse(ask.within_rate_limit(), "an unbounded loop of paid calls")

    def test_old_calls_stop_counting(self):
        ask._recent.extend([0.0] * ask.RATE_LIMIT_PER_HOUR)   # an hour ago
        self.assertTrue(ask.within_rate_limit())


class AnswerTest(unittest.TestCase):
    """The model is mocked: tests must not spend money."""

    def runner_yielding(self, *messages):
        client = Mock()
        client.beta.messages.tool_runner.return_value = iter(messages)
        return client

    def message(self, *blocks):
        return SimpleNamespace(content=list(blocks))

    def text(self, value):
        return SimpleNamespace(type="text", text=value)

    def tool_use(self, name):
        return SimpleNamespace(type="tool_use", name=name)

    def test_a_blank_question_never_reaches_the_model(self):
        client = self.runner_yielding()
        self.assertIsNone(ask.answer("  ", client=client))
        client.beta.messages.tool_runner.assert_not_called()

    def test_the_final_text_and_the_tools_used_come_back(self):
        client = self.runner_yielding(
            self.message(self.tool_use("data_range"), self.tool_use("occupancy_stats")),
            self.message(self.text("Monday evenings average 80% full.")),
        )
        result = ask.answer("are Mondays busy?", client=client)

        self.assertEqual(result["answer"], "Monday evenings average 80% full.")
        self.assertEqual(result["toolsUsed"], ["data_range", "occupancy_stats"])

    def test_a_failure_returns_none_rather_than_raising(self):
        client = Mock()
        client.beta.messages.tool_runner.side_effect = RuntimeError("api down")

        self.assertIsNone(ask.answer("anything", client=client))

    def test_an_answer_with_no_text_is_treated_as_a_failure(self):
        client = self.runner_yielding(self.message(self.tool_use("data_range")))
        self.assertIsNone(ask.answer("anything", client=client))


class EndpointTest(unittest.TestCase):
    def test_requires_sign_in(self):
        with patch.dict(app.app.config, {"SECRET_KEY": "k"}):
            response = app.app.test_client().post("/api/ask", json={"question": "hi"})
        self.assertEqual(response.status_code, 401)

    def test_a_rate_limited_caller_is_refused_before_the_model_runs(self):
        # the secret key must exist before a session can be written
        with patch.dict(app.app.config, {"SECRET_KEY": "k"}), \
             patch.object(app.ask, "available", return_value=True), \
             patch.object(app.ask, "within_rate_limit", return_value=False), \
             patch.object(app.ask, "answer") as answered:
            client = app.app.test_client()
            with client.session_transaction() as session:
                session["email"] = "andrewsapshin@gmail.com"
            response = client.post("/api/ask", json={"question": "hi"})

        self.assertEqual(response.status_code, 429)
        answered.assert_not_called()


if __name__ == "__main__":
    unittest.main()
