"""Tests for ingest.confidence — §8a scoring and §8d display_trust."""
from datetime import datetime, timedelta, timezone

import pytest

from ingest.confidence import _clamp, _recency_penalty, display_trust, score


# ── _clamp ─────────────────────────────────────────────────────────────────

def test_clamp_within():
    assert _clamp(50) == 50

def test_clamp_below_zero():
    assert _clamp(-10) == 0

def test_clamp_above_hundred():
    assert _clamp(110) == 100


# ── _recency_penalty ────────────────────────────────────────────────────────

def test_recency_penalty_no_published_at():
    assert _recency_penalty(None, 30) == 0

def test_recency_penalty_no_volatility():
    pub = datetime.now(timezone.utc) - timedelta(days=100)
    assert _recency_penalty(pub, None) == 0

def test_recency_penalty_within_window():
    pub = datetime.now(timezone.utc) - timedelta(days=10)
    assert _recency_penalty(pub, 30) == 0

def test_recency_penalty_past_window():
    pub = datetime.now(timezone.utc) - timedelta(days=60)
    penalty = _recency_penalty(pub, 30)
    assert 0 < penalty <= 30

def test_recency_penalty_caps_at_30():
    pub = datetime.now(timezone.utc) - timedelta(days=3650)
    assert _recency_penalty(pub, 30) == 30


# ── score ───────────────────────────────────────────────────────────────────

def test_score_high_confidence_adds_points():
    s_high = score('high', 70, 'primary', None, None)
    s_med = score('medium', 70, 'primary', None, None)
    assert s_high > s_med

def test_score_low_confidence_subtracts():
    s_low = score('low', 70, 'primary', None, None)
    s_med = score('medium', 70, 'primary', None, None)
    assert s_low < s_med

def test_score_aggregator_penalty():
    s_primary = score('medium', 70, 'primary', None, None)
    s_agg = score('medium', 70, 'aggregator', None, None)
    assert s_agg < s_primary

def test_score_corroboration_bump():
    s_0 = score('medium', 60, 'primary', None, None, corroboration_count=0)
    s_3 = score('medium', 60, 'primary', None, None, corroboration_count=3)
    assert s_3 > s_0

def test_score_corroboration_caps_at_15():
    s_3 = score('medium', 60, 'primary', None, None, corroboration_count=3)
    s_10 = score('medium', 60, 'primary', None, None, corroboration_count=10)
    assert s_10 == s_3 + (15 - 15)  # both hit the 15-point cap, delta is 0
    assert s_10 - s_3 == 0

def test_score_imputed_penalty():
    s_extracted = score('medium', 70, 'primary', None, None, method='extracted')
    s_imputed = score('medium', 70, 'primary', None, None, method='imputed')
    assert s_imputed < s_extracted

def test_score_always_0_to_100():
    for trust in (0, 50, 100):
        for conf in ('high', 'medium', 'low'):
            for kind in ('primary', 'aggregator'):
                s = score(conf, trust, kind, None, None)
                assert 0 <= s <= 100, f"Out of range: {s}"


# ── display_trust ────────────────────────────────────────────────────────────

def test_display_trust_fresh_claim():
    observed = datetime.now(timezone.utc)
    trust = display_trust(80, observed, volatility_days=365)
    assert trust == 0.8

def test_display_trust_decays_over_time():
    old = datetime.now(timezone.utc) - timedelta(days=1095)  # 3× past window
    trust = display_trust(80, old, volatility_days=365)
    assert trust < 0.8

def test_display_trust_no_volatility_no_decay():
    """Immutable attributes (volatility_days=None) should not decay much."""
    ancient = datetime.now(timezone.utc) - timedelta(days=3650)
    trust_fresh = display_trust(80, datetime.now(timezone.utc), None)
    trust_old = display_trust(80, ancient, None)
    # decay exists but is minimal (capped at 0.3 penalty)
    assert trust_old >= trust_fresh - 0.3

def test_display_trust_range():
    for age_days in (0, 100, 1000, 5000):
        observed = datetime.now(timezone.utc) - timedelta(days=age_days)
        t = display_trust(75, observed, volatility_days=365)
        assert 0.0 <= t <= 1.0, f"Out of range at age={age_days}: {t}"
