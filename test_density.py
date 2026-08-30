import os
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests

import density

EXCHANGE = {"access_token": "eyJfake", "token_type": "Bearer", "expires_in": "900"}

DISPLAY = {
    "id": "dsp_956223069054042646",
    "name": "RSF — Weight Rooms",
    # The real API has no top-level current_count. This decoy is here on
    # purpose: if fetch_reading ever reads the top level instead of
    # dedicated_space, it picks up 999 and the assertion fails loudly.
    "current_count": 999,
    "capacity": 999,
    "dedicated_space": {"current_count": 141, "capacity": 150, "safe_capacity": 150},
}


def fake_response(payload, ok=True):
    """Stand-in for a requests Response"""
    response = Mock()
    response.json.return_value = payload
    if not ok:
        response.raise_for_status.side_effect = requests.HTTPError("403 Forbidden")
    return response


@patch.dict(os.environ, {"DENSITY_SHARE_TOKEN": "shr_test"})
@patch("density.requests.get")
@patch("density.requests.post")
class FetchReadingTest(unittest.TestCase):
    def test_reads_the_numbers_from_dedicated_space(self, post, get):
        post.return_value = fake_response(EXCHANGE)
        get.return_value = fake_response(DISPLAY)

        reading = density.fetch_reading()

        self.assertEqual(reading.count, 141)
        self.assertEqual(reading.capacity, 150)

    def test_sends_the_exchanged_token_to_the_display_endpoint(self, post, get):
        post.return_value = fake_response(EXCHANGE)
        get.return_value = fake_response(DISPLAY)

        density.fetch_reading()

        # the long-lived share token is only ever sent to the exchange endpoint
        self.assertEqual(post.call_args.args[0], density.IDENTITY_URL)
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"], "Bearer shr_test"
        )
        # the display endpoint gets the short-lived token, never the share token
        self.assertEqual(get.call_args.args[0], density.DISPLAY_URL)
        self.assertEqual(
            get.call_args.kwargs["headers"]["Authorization"], "Bearer eyJfake"
        )

    def test_stamps_observed_at_with_aware_utc_time(self, post, get):
        post.return_value = fake_response(EXCHANGE)
        get.return_value = fake_response(DISPLAY)

        before = datetime.now(timezone.utc)
        reading = density.fetch_reading()
        after = datetime.now(timezone.utc)

        self.assertIsNotNone(reading.observed_at.tzinfo, "must be timezone-aware")
        self.assertTrue(before <= reading.observed_at <= after)

    def test_raises_when_the_share_token_is_rejected(self, post, get):
        post.return_value = fake_response({}, ok=False)

        with self.assertRaises(requests.HTTPError):
            density.fetch_reading()

        get.assert_not_called()

    def test_raises_when_the_display_call_fails(self, post, get):
        post.return_value = fake_response(EXCHANGE)
        get.return_value = fake_response({}, ok=False)

        with self.assertRaises(requests.HTTPError):
            density.fetch_reading()


class PercentageTest(unittest.TestCase):
    def test_computes_fraction_full(self):
        self.assertAlmostEqual(density.percentage(141, 150), 0.94)

    def test_empty_gym_is_zero(self):
        self.assertEqual(density.percentage(0, 150), 0.0)


if __name__ == "__main__":
    unittest.main()
