"""Tests for ingest.confidence — §8a odds combination and §8d display_trust."""
from datetime import datetime, timedelta, timezone

import pytest

from ingest.confidence import _clamp, combine, display_trust, source_odds


# ── _clamp ─────────────────────────────────────────────────────────────────

def test_clamp_within():
    assert _clamp(50) == 50

def test_clamp_below_zero():
    assert _clamp(-10) == 0

def test_clamp_above_hundred():
    assert _clamp(110) == 100


# ── source_odds ──────────────────────────────────────────────────────────────

def test_source_odds_coin_flip_at_50():
    assert source_odds(50) == 1.0

def test_source_odds_monotonic():
    assert source_odds(80) > source_odds(60) > source_odds(40)

def test_source_odds_high_trust():
    assert source_odds(80) == 4.0


# ── combine ──────────────────────────────────────────────────────────────────

def test_combine_single_source_equals_trust():
    """The operator's rule: one source → confidence IS the trust."""
    assert combine(80) == 80
    assert combine(62) == 62
    assert combine(30) == 30

def test_combine_independent_corroboration_increases():
    assert combine(80, [80]) > 80

def test_combine_two_equal_sources():
    """80 + 80 → odds 4*4=16 → p≈94.1 → 94."""
    assert combine(80, [80]) == 94

def test_combine_converges_upward():
    """More independent corroboration → monotonically upward, saturating at 99."""
    seq = [combine(80, [80] * n) for n in range(0, 6)]
    assert seq == sorted(seq)                    # never decreases
    assert seq[1] > seq[0]                       # corroboration helps
    assert all(s <= 99 for s in seq)             # saturates, never hits 100
    assert seq[-1] == 99

def test_combine_coin_flip_corroboration_worthless():
    """A 50-trust source agreeing adds nothing — odds are exactly 1."""
    assert combine(80, [50]) == combine(80)

def test_combine_unreliable_corroboration_worthless_not_harmful():
    """Agreement from a low-trust source never LOWERS confidence."""
    assert combine(80, [30]) == combine(80)

def test_combine_order_independent():
    assert combine(70, [80, 60]) == combine(70, [60, 80])

def test_combine_always_1_to_99():
    assert combine(0) == 1
    assert combine(100) == 99
    assert combine(100, [100, 100, 100]) == 99

def test_combine_real_sources():
    """Reuters (82) corroborated by SpaceNews (80): 82→odds 4.56, ×4 → 95."""
    assert combine(82, [80]) == 95


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
