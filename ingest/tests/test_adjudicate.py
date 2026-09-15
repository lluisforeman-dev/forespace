"""Tests for adjudication helper functions — §8b rules.

These test the pure logic helpers (_values_equal, _is_out_of_range,
_is_clearly_newer) which have no DB dependency.
The full _adjudicate_one function requires Django DB; those are integration tests.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ingest.tasks.adjudicate import _is_clearly_newer, _is_out_of_range, _values_equal


def _make_assertion(**kwargs):
    """Build a minimal mock Assertion with sensible defaults."""
    defaults = dict(
        value_text=None,
        value_num=None,
        value_bool=None,
        value_date=None,
        observed_at=datetime.now(timezone.utc),
        document=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ── _values_equal ────────────────────────────────────────────────────────────

def test_values_equal_same_text():
    a = _make_assertion(value_text='SpaceX')
    b = _make_assertion(value_text='SpaceX')
    assert _values_equal(a, b)

def test_values_equal_different_text():
    a = _make_assertion(value_text='SpaceX')
    b = _make_assertion(value_text='Rocket Lab')
    assert not _values_equal(a, b)

def test_values_equal_same_num():
    a = _make_assertion(value_num=Decimal('1000000'))
    b = _make_assertion(value_num=Decimal('1000000'))
    assert _values_equal(a, b)

def test_values_equal_different_num():
    a = _make_assertion(value_num=Decimal('1000000'))
    b = _make_assertion(value_num=Decimal('2000000'))
    assert not _values_equal(a, b)

def test_values_equal_same_bool():
    a = _make_assertion(value_bool=True)
    b = _make_assertion(value_bool=True)
    assert _values_equal(a, b)

def test_values_equal_both_none():
    a = _make_assertion()
    b = _make_assertion()
    assert not _values_equal(a, b)  # no populated field → not equal


# ── _is_out_of_range ─────────────────────────────────────────────────────────

def test_out_of_range_5x_increase():
    a = _make_assertion(value_num=Decimal('6000000'))
    b = _make_assertion(value_num=Decimal('1000000'))
    assert _is_out_of_range(a, b)

def test_out_of_range_5x_decrease():
    a = _make_assertion(value_num=Decimal('100000'))
    b = _make_assertion(value_num=Decimal('1000000'))
    assert _is_out_of_range(a, b)

def test_not_out_of_range_2x():
    a = _make_assertion(value_num=Decimal('2000000'))
    b = _make_assertion(value_num=Decimal('1000000'))
    assert not _is_out_of_range(a, b)

def test_out_of_range_zero_existing():
    a = _make_assertion(value_num=Decimal('1000000'))
    b = _make_assertion(value_num=Decimal('0'))
    assert not _is_out_of_range(a, b)  # zero denominator → safe, no flag

def test_out_of_range_none_values():
    a = _make_assertion()
    b = _make_assertion()
    assert not _is_out_of_range(a, b)

def test_out_of_range_exactly_5x():
    a = _make_assertion(value_num=Decimal('5000000'))
    b = _make_assertion(value_num=Decimal('1000000'))
    assert not _is_out_of_range(a, b)  # 5× exactly is not >5

def test_out_of_range_just_over_5x():
    a = _make_assertion(value_num=Decimal('5100000'))
    b = _make_assertion(value_num=Decimal('1000000'))
    assert _is_out_of_range(a, b)


# ── _is_clearly_newer ────────────────────────────────────────────────────────

def _doc(published_at):
    return SimpleNamespace(published_at=published_at)


def test_clearly_newer_by_document_date():
    new_pub = datetime.now(timezone.utc)
    old_pub = new_pub - timedelta(days=45)
    a_new = _make_assertion(document=_doc(new_pub))
    a_old = _make_assertion(document=_doc(old_pub))
    assert _is_clearly_newer(a_new, a_old)

def test_not_clearly_newer_only_29_days():
    new_pub = datetime.now(timezone.utc)
    old_pub = new_pub - timedelta(days=29)
    a_new = _make_assertion(document=_doc(new_pub))
    a_old = _make_assertion(document=_doc(old_pub))
    assert not _is_clearly_newer(a_new, a_old)

def test_clearly_newer_falls_back_to_observed_at():
    """No document dates — use observed_at."""
    now = datetime.now(timezone.utc)
    a_new = _make_assertion(observed_at=now)
    a_old = _make_assertion(observed_at=now - timedelta(days=60))
    assert _is_clearly_newer(a_new, a_old)

def test_not_clearly_newer_same_observed_at():
    now = datetime.now(timezone.utc)
    a_new = _make_assertion(observed_at=now)
    a_old = _make_assertion(observed_at=now - timedelta(days=5))
    assert not _is_clearly_newer(a_new, a_old)
