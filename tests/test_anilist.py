"""
Unit tests for the AniList request-layer rate-limit throttle.

get_query paces itself off AniList's own X-RateLimit-Remaining header rather
than a fixed sleep between requests: no delay while budget remains, a short
back-off only once the previous response showed we're near the cap. These tests
pin that behaviour — header parsing, the threshold, and the throttle/record
ordering inside get_query.

Run with: python -m pytest tests/test_anilist.py -v
"""

import unittest
from unittest.mock import MagicMock, patch

from seadexarr.modules import anilist


def _resp(remaining=None, payload=None):
    """A stand-in requests.Response with the given X-RateLimit-Remaining."""
    resp = MagicMock()
    resp.headers = {} if remaining is None else {"X-RateLimit-Remaining": remaining}
    resp.json.return_value = payload if payload is not None else {"data": {}}
    resp.raise_for_status.return_value = None
    return resp


class TestRecordRateLimit(unittest.TestCase):

    def setUp(self):
        # Reset module state so tests don't leak the remaining count into each other.
        anilist._rate_limit_remaining = None

    def test_parses_header_to_int(self):
        anilist._record_rate_limit(_resp(remaining="42"))
        self.assertEqual(anilist._rate_limit_remaining, 42)

    def test_missing_header_leaves_previous_value(self):
        anilist._rate_limit_remaining = 7
        anilist._record_rate_limit(_resp(remaining=None))
        self.assertEqual(anilist._rate_limit_remaining, 7)

    def test_unparseable_header_leaves_previous_value(self):
        anilist._rate_limit_remaining = 7
        anilist._record_rate_limit(_resp(remaining="not-a-number"))
        self.assertEqual(anilist._rate_limit_remaining, 7)


class TestThrottleIfNeeded(unittest.TestCase):

    def setUp(self):
        anilist._rate_limit_remaining = None

    def test_no_sleep_when_remaining_unknown(self):
        anilist._rate_limit_remaining = None
        with patch.object(anilist.time, "sleep") as sleep:
            anilist._throttle_if_needed()
        sleep.assert_not_called()

    def test_no_sleep_with_ample_budget(self):
        anilist._rate_limit_remaining = anilist._LOW_REMAINING_THRESHOLD + 1
        with patch.object(anilist.time, "sleep") as sleep:
            anilist._throttle_if_needed()
        sleep.assert_not_called()

    def test_sleeps_at_threshold(self):
        anilist._rate_limit_remaining = anilist._LOW_REMAINING_THRESHOLD
        with patch.object(anilist.time, "sleep") as sleep:
            anilist._throttle_if_needed()
        sleep.assert_called_once_with(anilist._THROTTLE_SLEEP)

    def test_sleeps_below_threshold(self):
        anilist._rate_limit_remaining = 0
        with patch.object(anilist.time, "sleep") as sleep:
            anilist._throttle_if_needed()
        sleep.assert_called_once_with(anilist._THROTTLE_SLEEP)


class TestGetQueryThrottling(unittest.TestCase):

    def setUp(self):
        anilist._rate_limit_remaining = None

    def test_records_remaining_from_response(self):
        with patch.object(anilist._SESSION, "post", return_value=_resp(remaining="13")):
            with patch.object(anilist.time, "sleep"):
                anilist.get_query(123)
        self.assertEqual(anilist._rate_limit_remaining, 13)

    def test_no_throttle_on_first_call_then_throttles_when_low(self):
        # First response leaves us near the cap; the *next* call must back off
        # before firing, proving the throttle reads the prior response.
        with patch.object(anilist.time, "sleep") as sleep:
            with patch.object(anilist._SESSION, "post", return_value=_resp(remaining="2")):
                anilist.get_query(1)
                # First call saw no prior remaining -> no sleep yet.
                sleep.assert_not_called()
                anilist.get_query(2)
                # Second call saw remaining=2 (<= threshold) -> one back-off.
                sleep.assert_called_once_with(anilist._THROTTLE_SLEEP)

    def test_throttle_runs_before_request(self):
        # Pre-seed a low remaining; the sleep must happen before post is called.
        anilist._rate_limit_remaining = 0
        calls = []
        with patch.object(anilist.time, "sleep", side_effect=lambda *_: calls.append("sleep")):
            def _post(*a, **k):
                calls.append("post")
                return _resp(remaining="90")
            with patch.object(anilist._SESSION, "post", side_effect=_post):
                anilist.get_query(99)
        self.assertEqual(calls, ["sleep", "post"])


if __name__ == "__main__":
    unittest.main()
