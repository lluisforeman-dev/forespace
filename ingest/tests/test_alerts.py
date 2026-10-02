"""Tests for the credits-exhausted operator alert."""
from unittest.mock import MagicMock, patch

from ingest.alerts import clear_credits_alert, credits_alert, mark_credits_exhausted


def _redis_store_mock():
    store = {}

    def _key(k):
        return k.encode() if isinstance(k, str) else k

    r = MagicMock()
    r.exists.side_effect = lambda k: _key(k) in store
    r.get.side_effect = lambda k: store.get(_key(k))
    r.set.side_effect = lambda k, v: store.__setitem__(_key(k), v)
    r.delete.side_effect = lambda k: store.pop(_key(k), None)
    return r, store


def test_mark_sets_flag_sends_email_once():
    r, _ = _redis_store_mock()
    with patch('ingest.alerts._redis', return_value=r), \
         patch('ingest.alerts._send_email') as email:
        mark_credits_exhausted()
        assert email.call_count == 1
        # second 402 must not duplicate the email
        mark_credits_exhausted()
        assert email.call_count == 1


def test_alert_visible_until_cleared():
    r, _ = _redis_store_mock()
    with patch('ingest.alerts._redis', return_value=r), \
         patch('ingest.alerts._send_email'):
        assert credits_alert() is None
        mark_credits_exhausted()
        alert = credits_alert()
        assert alert is not None and 'since' in alert
        clear_credits_alert()
        assert credits_alert() is None
