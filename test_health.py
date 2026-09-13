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


# 23:00 in Berkeley. In the sensor's history, counts froze mostly from late
# evening until its nightly reset at 09:00 UTC.
NOW = datetime(2026, 9, 11, 6, 0, tzinfo=timezone.utc)


class FrozenCountTest(testing.DatabaseTest):
    """A stalled sensor keeps answering on time with the same count, so age
    alone stays green while the number is wrong. Ten or more people, unchanged
    for an hour, fails freshness too."""

    def get(self):
        with patch.object(app, "_now", return_value=NOW):
            response = app.app.test_client().get("/health/freshness")
        return response.status_code, response.get_json()

    def hold(self, count, since, until):
        """`count` at `since` and at `until`, polled every four minutes between."""
        at = until
        while at > since:
            store.save(self.connection, Reading(count, 150, at))
            at -= timedelta(minutes=4)
        store.save(self.connection, Reading(count, 150, since))

    def test_a_count_held_for_an_hour_fails_freshness_but_not_health(self):
        self.hold(12, since=NOW - timedelta(minutes=92), until=NOW - timedelta(minutes=2))
        status, body = self.get()
        health_status = app.app.test_client().get("/health").status_code

        self.assertEqual(status, 503)
        self.assertFalse(body["fresh"])
        self.assertFalse(body["stale"], "the readings themselves are on time")
        self.assertTrue(body["frozen"])
        self.assertIn("frozen at 12 for 90 minutes", body["reason"])
        self.assertEqual(body["unchangedSince"],
                         (NOW - timedelta(minutes=92)).astimezone(app.LOCAL_TZ).isoformat())
        self.assertEqual(health_status, 200, "/health must not restart on a frozen sensor")

    def test_ten_people_for_exactly_an_hour_is_frozen(self):
        """At least ten, for sixty minutes or more: both bounds are inclusive."""
        self.hold(10, since=NOW - timedelta(minutes=61), until=NOW - timedelta(minutes=1))
        status, body = self.get()

        self.assertEqual(status, 503)
        self.assertTrue(body["frozen"])

    def test_fewer_than_ten_people_may_hold_still_for_hours(self):
        """An empty gym reads 0 all night, and a nearly empty one barely moves."""
        self.hold(9, since=NOW - timedelta(hours=5), until=NOW - timedelta(minutes=1))
        status, body = self.get()

        self.assertEqual(status, 200)
        self.assertFalse(body["frozen"])

    def test_a_minute_short_of_an_hour_is_not_yet_frozen(self):
        """Timed from the first reading of the held count, not the one before it."""
        store.save(self.connection, Reading(13, 150, NOW - timedelta(minutes=64)))
        self.hold(12, since=NOW - timedelta(minutes=60), until=NOW - timedelta(minutes=1))
        status, body = self.get()

        self.assertEqual(status, 200)
        self.assertFalse(body["frozen"])

    def test_any_change_restarts_the_hour(self):
        """Even when the count comes back to the value it held before."""
        self.hold(12, since=NOW - timedelta(minutes=150), until=NOW - timedelta(minutes=46))
        store.save(self.connection, Reading(14, 150, NOW - timedelta(minutes=42)))
        self.hold(12, since=NOW - timedelta(minutes=38), until=NOW - timedelta(minutes=2))
        status, body = self.get()

        self.assertEqual(status, 200)
        self.assertFalse(body["frozen"])

    def test_the_hour_is_measured_between_readings_not_up_to_now(self):
        """Readings that stop are stale, on their own threshold; silence is not
        evidence that the count held."""
        self.hold(12, since=NOW - timedelta(minutes=64), until=NOW - timedelta(minutes=10))
        status, body = self.get()

        self.assertEqual(status, 200)
        self.assertFalse(body["frozen"])

    def test_a_count_that_froze_and_then_stopped_reports_both(self):
        self.hold(12, since=NOW - timedelta(minutes=120), until=NOW - timedelta(minutes=30))
        status, body = self.get()

        self.assertEqual(status, 503)
        self.assertTrue(body["stale"])
        self.assertTrue(body["frozen"])
        self.assertIn("no reading for 30 minutes", body["reason"])
        self.assertIn("frozen at 12 for 90 minutes", body["reason"])

    def test_imported_history_cannot_extend_a_run_into_the_live_feed(self):
        """Health describes live collection only. History runs right up to the
        first live reading, so reading it here would join the two."""
        for minutes in range(100, 0, -10):
            self.connection.execute("INSERT INTO history (observed_at, count) VALUES (%s, 12)",
                                    (NOW - timedelta(minutes=minutes),))
        self.connection.commit()
        store.save(self.connection, Reading(12, 150, NOW - timedelta(minutes=2)))
        status, body = self.get()

        self.assertEqual(status, 200)
        self.assertFalse(body["frozen"])
