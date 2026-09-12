"""Shared setup for tests that touch the database.

Tests now need a real Postgres, which is the cost of moving the weekday curve
into SQL: there is no in-memory substitute that has AT TIME ZONE and
percentile_cont, and testing against a different engine than production runs
would defeat the point.

Locally:  createdb rsf_test
In CI:    a postgres service container (see .github/workflows/ci.yml)
"""

import os
import unittest

TEST_DATABASE_URL = os.environ.get("RSF_TEST_DATABASE_URL", "postgresql:///rsf_test")

# Set at import, not in setUpClass: tests that exercise the app rather than the
# store still open the pool, and without this they would reach for the
# development database -- which exists locally and does not in CI.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

TABLES = "feedback, bookings, predictions, readings, history, calendar_days, app_state, google_tokens"


class DatabaseTest(unittest.TestCase):
    """Gives each test an empty database and a connection."""

    @classmethod
    def setUpClass(cls):
        import store

        os.environ["DATABASE_URL"] = TEST_DATABASE_URL
        store.reset_pool()

    def setUp(self):
        import store

        self._cm = store.connection()
        self.connection = self._cm.__enter__()
        self.addCleanup(self._cm.__exit__, None, None, None)
        # truncate rather than drop: the schema is created once by the pool
        self.connection.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
        self.connection.commit()
