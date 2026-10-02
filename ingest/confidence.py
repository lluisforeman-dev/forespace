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


# ── Source independence (§8a) ────────────────────────────────────────────────
# "Two corroborations only count if the sources are independent. Three trade
# outlets reprinting the same press release is one source."
#
# v1 rule (deterministic, no embeddings):
#   · same document row                                  → NOT independent
#   · identical content hash                             → NOT independent
#   · same publication domain (reprints, syndication)    → NOT independent
#   · different domains                                  → independent
#
# Known v1 limitation: two different domains covering the same underlying press
# release still count as independent. Content-level story dedup (embedding
# similarity) is the v2 upgrade and can only make trust STRICTER.

def document_domain(doc) -> str:
    """Best-effort publication domain for a document ('' when unknown)."""
    if doc is None:
        return ''
    url = getattr(doc, 'url', None)
    if url:
        netloc = urlparse(url).netloc.lower().removeprefix('www.')
        if netloc:
            return netloc
    return (getattr(getattr(doc, 'source', None), 'domain', '') or '') \
        .lower().removeprefix('www.')


def documents_independent(doc_a, doc_b) -> bool:
    """True when two documents count as separate evidence for the same fact."""
    if doc_a is None or doc_b is None:
        return False
    if getattr(doc_a, 'pk', None) == getattr(doc_b, 'pk', None):
        return False
    sha_a = getattr(doc_a, 'content_sha256', None)
    sha_b = getattr(doc_b, 'content_sha256', None)
    if sha_a and sha_b and sha_a == sha_b:
        return False
    domain_a = document_domain(doc_a)
    domain_b = document_domain(doc_b)
    if not domain_a or not domain_b:
        # Unknown provenance on either side — do not grant corroboration credit
        return False
    return domain_a != domain_b


# ── Confidence combination (§8a v2 — Bayesian odds, no invented constants) ───
# A source's trust IS the claim's evidence weight. The extractor LLM's
# self-reported confidence is no longer mapped to magic numbers (45/62/78):
# self-reported certainty is not a measurement. The only numeric inputs are
# source trust (curated, per-source) and the structure of corroboration.
#
#   odds_source = T / (100 - T)          # T=80 → 4:1 right, T=50 → coin flip
#   single source:   confidence = T      # exactly the source's trust
#   corroboration:   odds *= max(1, odds_i)   # independent sources only
#
# Agreement from a source at or below coin-flip trust contributes nothing —
# it cannot confirm, it can only fail to. Same-domain echoes are already
# excluded upstream (documents_independent). Converges monotonically toward
# 99 and never reaches it.

def source_odds(trust: int) -> float:
    """Odds that a source of this trust is right about a claim it reports."""
    t = _clamp(trust)
    return t / max(1, 100 - t)


def combine(base_trust: int, corroborant_trusts=None) -> int:
    """Combine source trust (+ independent corroboration) into a 1-99 confidence.

    base_trust: the primary source's trust (0-100) — the claim's prior.
    corroborant_trusts: trusts of additional independent sources confirming
    the same value. Bayesian odds multiplication; order-independent.
    """
    odds = source_odds(base_trust)
    for t in corroborant_trusts or []:
        o = source_odds(t)
        if o > 1:  # agreement from an unreliable source is worthless, not harmful
            odds *= o
    p = odds / (1 + odds)
    return max(1, min(99, round(100 * p)))


def effective_staleness(effective_at, volatility_days: int | None) -> dict:
    """How far past its volatility window a claim has decayed.

    effective_at is the date the value was TRUE in the world (assertion
    valid_range.lower, i.e. as_of) — not the date we learned it. A "100 employees
    in 2016" claim is stale in 2026 even if we only researched it yesterday.

    Returns {'days_stale', 'periods_stale', 'stale'}; immutable attributes
    (volatility_days=None) are treated as ~10-year windows per §8d.
    """
    if volatility_days is None:
        volatility_days = 3650
    if effective_at is None:
        return {'days_stale': 0, 'periods_stale': 0.0, 'stale': False}
    now = datetime.now(timezone.utc)
    if effective_at.tzinfo is None:
        effective_at = effective_at.replace(tzinfo=timezone.utc)
    age_days = (now - effective_at).days
    excess = max(0, age_days - volatility_days)
    periods = excess / max(volatility_days, 1)
    return {'days_stale': excess, 'periods_stale': round(periods, 2), 'stale': excess > 0}


def display_trust(
    confidence: int,
    observed_at: datetime,
    volatility_days: int | None,
    effective_at: datetime | None = None,
) -> float:
    """Compute the read-time trust score for API consumers (§8d).

    Returned on a 0.1–1.0 scale. Decay is anchored to *effective_at* (when the
    value was true in the world — e.g. assertion valid_range.lower) when provided,
    falling back to *observed_at* (when we learned it). Never stored on the
    assertion row — always computed at read time.

    Each full volatility period past the window costs 0.05 trust, penalised up to
    0.7 — so a volatile claim ~14 periods past its window floors at 0.1, i.e. it
    is effectively no longer valid ("100 employees in 2016" in 2026).
    """
    if volatility_days is None:
        volatility_days = 3650  # ~10 years for "immutable" attributes
    when = effective_at or observed_at
    base = confidence / 100
    info = effective_staleness(when, volatility_days)
    age_penalty = min(0.7, 0.05 * info['periods_stale'])
    return round(max(0.1, min(1.0, base - age_penalty)), 1)
