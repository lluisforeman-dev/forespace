"""Confidence scoring — §8a of the blueprint.

The score drives adjudication order and acceptance policy.
It is NOT exposed directly to third parties; display_trust() is computed at read time.
"""
from __future__ import annotations
from datetime import datetime, timezone
from urllib.parse import urlparse

# Hand-curated domain trust scores (0-100).
# Unknown domains default to 55.  .gov/.int/.edu get structural bonuses below.
_DOMAIN_TRUST: dict[str, int] = {
    # Government / intergovernmental
    'nasa.gov': 92, 'esa.int': 92, 'jaxa.jp': 88, 'roscosmos.ru': 80,
    'isro.gov.in': 85, 'cnes.fr': 85, 'dlr.de': 85, 'ukspace.agency': 82,
    # Primary company sources
    'spacex.com': 85, 'rocketlabusa.com': 83, 'blueorigin.com': 83,
    'virgingalactic.com': 80, 'planet.com': 80, 'maxar.com': 80,
    'airbus.com': 82, 'boeing.com': 82, 'lockheedmartin.com': 82,
    'northropgrumman.com': 82, 'arianespace.com': 83, 'ula.com': 82,
    # Trade press
    'spacenews.com': 80, 'spaceflightnow.com': 78, 'nasaspaceflight.com': 76,
    'aviationweek.com': 78, 'thespacereview.com': 72, 'parabolicarc.com': 68,
    'spacedaily.com': 65, 'universetoday.com': 65,
    # General quality press
    'reuters.com': 82, 'bloomberg.com': 80, 'ft.com': 82, 'wsj.com': 80,
    'bbc.com': 78, 'theguardian.com': 74, 'nytimes.com': 76,
    'techcrunch.com': 65, 'wired.com': 65, 'arstechnica.com': 70,
    # Reference
    'wikipedia.org': 55,
    # Low-trust
    'twitter.com': 32, 'x.com': 32, 'reddit.com': 28, 'facebook.com': 28,
}


def domain_trust(url: str | None) -> int:
    """Return a 0-100 trust score for the domain of a URL."""
    if not url:
        return 55
    try:
        host = urlparse(url).netloc.lower().removeprefix('www.')
        if host in _DOMAIN_TRUST:
            return _DOMAIN_TRUST[host]
        if host.endswith('.gov'):
            return 88
        if host.endswith('.int'):
            return 85
        if host.endswith('.edu'):
            return 75
        if host.endswith('.mil'):
            return 85
    except Exception:
        pass
    return 55


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
