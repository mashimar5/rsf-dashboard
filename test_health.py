import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import app
import store
import testing
from density import Reading


class HealthTest(testing.DatabaseTest):
    """HTTP status means "a restart might help"; staleness does not."""

    def get(self):
        response = app.app.test_client().get("/health")
        return response.status_code, response.get_json()

    def save(self, minutes_ago):
        store.save(self.connection, Reading(
            80, 150, datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)))

    def test_healthy_with_recent_data(self):
        self.save(2)
        status, body = self.get()

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["stale"])
        self.assertLess(body["ageSeconds"], 300)

    def test_stale_data_is_reported_but_does_not_fail_the_check(self):
        """A dead upstream is not fixed by restarting; a crash loop would make
        one outage into two."""
        self.save(60)
        status, body = self.get()

        self.assertEqual(status, 200, "still restart-healthy")
        self.assertTrue(body["stale"])
        self.assertTrue(body["ok"])

    def test_an_unreachable_database_fails_the_check(self):
        with patch.object(app, "db", side_effect=OSError("no route")):
            status, body = self.get()

        self.assertEqual(status, 503)
        self.assertFalse(body["ok"])
        self.assertEqual(body["database"], "unreachable")

    def test_a_dead_collector_thread_fails_the_check(self):
        """gunicorn can keep serving happily while the collector thread is
        gone, which is invisible without this."""
        self.save(2)
        dead = type("T", (), {"is_alive": lambda self: False})()
        with patch.object(app, "COLLECT_INTERVAL", 240), patch.object(app, "_collector", dead):
            status, body = self.get()

        self.assertEqual(status, 503)
        self.assertEqual(body["collector"], "dead")

    def test_the_worst_recent_gap_is_reported(self):
        self.save(2)
        self.save(40)
        _, body = self.get()

        self.assertGreater(body["gapMinutes"], 30)
        self.assertIsInstance(body["gapMinutes"], float)

    def test_no_data_at_all_is_not_a_restartable_condition(self):
        status, body = self.get()

        self.assertEqual(status, 200)
        self.assertIsNone(body["lastReadingAt"])


class FreshnessEndpointTest(testing.DatabaseTest):
    """The endpoint an external monitor watches. Unlike /health, staleness
    here *is* a failure -- nothing restarts on it, a human is told."""

    def get(self):
        response = app.app.test_client().get("/health/freshness")
        return response.status_code, response.get_json()

    def save(self, minutes_ago):
        store.save(self.connection, Reading(
            80, 150, datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)))

    def test_recent_data_passes(self):
        self.save(2)
        status, body = self.get()

        self.assertEqual(status, 200)
        self.assertTrue(body["fresh"])

    def test_stale_data_fails_here_even_though_it_does_not_fail_health(self):
        self.save(60)
        status, body = self.get()
        health_status, _ = app.app.test_client().get("/health").status_code, None

        self.assertEqual(status, 503)
        self.assertFalse(body["fresh"])
        self.assertIn("60 minutes", body["reason"])
        self.assertEqual(health_status, 200, "/health must not restart on staleness")

    def test_an_empty_database_fails_rather_than_looking_fine(self):
        status, body = self.get()

        self.assertEqual(status, 503)
        self.assertIn("no readings", body["reason"])
