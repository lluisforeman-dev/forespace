"""Confidence scoring — §8a of the blueprint.

The score drives adjudication order and acceptance policy.
It is NOT exposed directly to third parties; display_trust() is computed at read time.
"""
from __future__ import annotations
from datetime import datetime, timezone


def _clamp(v: int, lo: int = 0, hi: int = 100) -> int:
    return max(lo, min(hi, v))


def _recency_penalty(published_at, volatility_days: int | None) -> int:
    """Penalise claims whose source is older than the attribute's volatility window."""
    if not published_at or not volatility_days:
        return 0
    now = datetime.now(timezone.utc)
    age_days = (now - published_at).days
    if age_days <= volatility_days:
        return 0
    excess = age_days - volatility_days
    return min(30, int(excess / max(volatility_days, 1) * 10))


def score(
    extractor_confidence: str,    # "high" | "medium" | "low"
    source_base_trust: int,        # 0-100, hand-set per Source
    source_kind: str,              # 'regulator' | 'primary' | 'trade_press' | 'aggregator' | ...
    document_published_at,
    volatility_days: int | None,
    corroboration_count: int = 0,
    method: str = 'extracted',
) -> int:
    s = source_base_trust
    if extractor_confidence == "high":
        s += 10
    elif extractor_confidence == "low":
        s -= 15
    s -= _recency_penalty(document_published_at, volatility_days)
    s += min(15, 5 * corroboration_count)
    if source_kind == "aggregator":
        s -= 20
    if method == "imputed":
        s -= 25
    return _clamp(s)


def display_trust(confidence: int, observed_at: datetime, volatility_days: int | None) -> float:
    """Compute the read-time trust score for API consumers (§8d).

    Returned on a 0.1–1.0 scale, decays as the claim ages past volatility_days.
    Never stored on the assertion row — always computed at read time.
    """
    if volatility_days is None:
        volatility_days = 3650  # ~10 years for "immutable" attributes
    base = confidence / 100
    staleness = max(0, (datetime.now(timezone.utc) - observed_at).days - volatility_days)
    age_penalty = min(0.3, 0.05 * (staleness / max(volatility_days, 1)))
    return round(_clamp(int((base - age_penalty) * 100)) / 100, 1)
