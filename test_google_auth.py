import json
import os
import sqlite3
import unittest
from base64 import urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import requests

import google_auth

KEY = "Zn9sT2xQaVJtYkR2WmxKZ0hqTnBLd0V4Q3NBdVl0M2c="   # a valid Fernet key


def env(**extra):
    base = {
        "GOOGLE_CLIENT_ID": "client-123.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "secret-xyz",
        "TOKEN_ENCRYPTION_KEY": KEY,
        "ALLOWED_EMAILS": "andrewsapshin@gmail.com",
    }
    base.update(extra)
    return patch.dict(os.environ, base)


def id_token_for(email):
    claims = urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"header.{claims}.signature"


class AuthorizeUrlTest(unittest.TestCase):
    @env()
    def test_requests_only_freebusy_plus_identity(self):
        query = parse_qs(urlparse(google_auth.authorize_url("https://x/cb", "st")).query)
        scopes = query["scope"][0].split()

        self.assertIn("https://www.googleapis.com/auth/calendar.freebusy", scopes)
        self.assertNotIn("https://www.googleapis.com/auth/calendar", scopes)
        self.assertNotIn("https://www.googleapis.com/auth/calendar.readonly", scopes)
        self.assertNotIn("https://www.googleapis.com/auth/calendar.events", scopes)

    @env()
    def test_asks_for_a_refresh_token_explicitly(self):
        query = parse_qs(urlparse(google_auth.authorize_url("https://x/cb", "st")).query)

        # without both of these Google returns an access token only, and the
        # agent would need re-consent on every run
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["prompt"], ["consent"])
        self.assertEqual(query["state"], ["st"])


class TokenStorageTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)

    @env()
    def test_round_trips_a_refresh_token(self):
        google_auth.save_refresh_token(self.connection, "Andrewsapshin@Gmail.com", "1//refresh")

        self.assertEqual(
            google_auth.load_refresh_token(self.connection, "andrewsapshin@gmail.com"),
            "1//refresh",
            "email lookup must be case-insensitive",
        )

    @env()
    def test_the_token_is_not_stored_in_plaintext(self):
        google_auth.save_refresh_token(self.connection, "a@b.com", "1//secret-value")
        stored = self.connection.execute("SELECT refresh_token FROM google_tokens").fetchone()[0]

        self.assertNotIn("1//secret-value", stored)

    @env()
    def test_forgetting_removes_it(self):
        google_auth.save_refresh_token(self.connection, "a@b.com", "1//x")
        google_auth.forget(self.connection, "a@b.com")

        self.assertIsNone(google_auth.load_refresh_token(self.connection, "a@b.com"))

    def test_a_changed_encryption_key_yields_none_not_a_crash(self):
        with env():
            google_auth.save_refresh_token(self.connection, "a@b.com", "1//x")
        other = "qPDEnLWAafrI04jL4ghxw9Y89G0tstqnXsg9wfw-NJc="   # a different, valid Fernet key
        with env(TOKEN_ENCRYPTION_KEY=other):
            self.assertIsNone(google_auth.load_refresh_token(self.connection, "a@b.com"))


class RefreshTest(unittest.TestCase):
    @env()
    def test_invalid_grant_asks_for_reauth_rather_than_failing_opaquely(self):
        response = Mock(status_code=400, text='{"error": "invalid_grant"}')
        with patch.object(requests, "post", return_value=response):
            with self.assertRaises(google_auth.NeedsReauth):
                google_auth.access_token_from("1//dead")

    @env()
    def test_returns_the_access_token(self):
        response = Mock(status_code=200)
        response.json.return_value = {"access_token": "ya29.fresh"}
        with patch.object(requests, "post", return_value=response):
            self.assertEqual(google_auth.access_token_from("1//ok"), "ya29.fresh")


class IdentityTest(unittest.TestCase):
    def test_reads_the_email_claim(self):
        self.assertEqual(
            google_auth.email_from_id_token(id_token_for("Person@Example.com")),
            "person@example.com",
        )

    def test_malformed_tokens_yield_none(self):
        self.assertIsNone(google_auth.email_from_id_token("not-a-jwt"))
        self.assertIsNone(google_auth.email_from_id_token(""))

    @env(ALLOWED_EMAILS="")
    def test_an_empty_allowlist_admits_nobody(self):
        self.assertEqual(google_auth.allowed_emails(), set())

    @env(ALLOWED_EMAILS=" A@b.com , c@D.com ")
    def test_the_allowlist_is_normalised(self):
        self.assertEqual(google_auth.allowed_emails(), {"a@b.com", "c@d.com"})


class FreeBusyTest(unittest.TestCase):
    def test_parses_busy_blocks(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "calendars": {"primary": {"busy": [
                {"start": "2026-09-07T21:00:00Z", "end": "2026-09-07T22:00:00Z"},
            ]}}
        }
        with patch.object(requests, "post", return_value=response):
            blocks = google_auth.busy_intervals(
                "ya29", datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(days=1)
            )

        self.assertEqual(len(blocks), 1)
        start, end = blocks[0]
        self.assertEqual((end - start), timedelta(hours=1))

    def test_a_free_day_is_an_empty_list_not_an_error(self):
        response = Mock(status_code=200)
        response.json.return_value = {"calendars": {"primary": {"busy": []}}}
        with patch.object(requests, "post", return_value=response):
            self.assertEqual(
                google_auth.busy_intervals("ya29", datetime.now(timezone.utc),
                                           datetime.now(timezone.utc)),
                [],
            )
