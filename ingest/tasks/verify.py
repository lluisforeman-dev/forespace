"""Stage 4b — Mechanical quote verification for web-research claims (§7c).

Eigensearch returns claims that cite web sources, but the cited pages were
never fetched — until now the RSS path had the mandatory verbatim-quote guard
and the dominant research path did not. This task closes that hole:

  1. Fetch the claim's cited URL (robots.txt honored, per-domain paced,
     raw text archived on the Document — never refetched).
  2. Verify the claim's quote is a verbatim substring of the source
     (raw HTML or trafilatura-extracted prose).
  3. Score the outcome:
       verified   → quote_verified=True, +5 confidence (a claim the source
                    actually says is worth more than one the model paraphrased)
       failed     → quote_verified=False, −20 confidence; accepted claims
                    falling below 50 drop back to candidate (hallucination
                    suspect)
       too weak   → quote shorter than 20 chars (the `quote = mention`
                    fallback) → quote_verified=False, no fetch wasted
       unfetchable→ 404 / blocked / timeout → left unverified (null), NO
                    penalty — a paywalled source is not a lying source

Pure network + string comparison. No LLM. Runs on the crawl queue (I/O-bound).
"""
import logging
from urllib.parse import urlparse

import trafilatura
from celery import shared_task
from django.utils import timezone

from core.models import Assertion

logger = logging.getLogger(__name__)

_MIN_QUOTE_LEN = 20          # shorter quotes cannot be meaningfully verified
_VERIFY_BONUS = 5
_FAIL_PENALTY = 20
_RAW_LIMIT = 2_000_000       # cap archived raw text at ~2MB


def verify_assertion_quote(assertion: Assertion) -> str:
    """Verify one assertion's quote against its cited source.

    Returns 'verified' | 'failed' | 'weak' | 'unfetchable' | 'skipped'.
    Writes quote_verified / verified_at / confidence / status as appropriate.
    """
    import requests
    from django.db.models import F

    from ingest.tasks.crawl import _HEADERS, _pace_domain, _robots_parser

    doc = assertion.document
    quote = (assertion.quote or '').strip()

    if doc is None or not doc.url or not quote:
        return 'skipped'

    if len(quote) < _MIN_QUOTE_LEN:
        # The `quote = mention` fallback — too weak to verify, treat as unverified
        Assertion.objects.filter(pk=assertion.pk).update(quote_verified=False)
        return 'weak'

    text = doc.raw_content
    if not text:
        domain = urlparse(doc.url).netloc
        rp = _robots_parser(domain)
        if rp is not None and not rp.can_fetch('EigenGraph', doc.url):
            logger.debug('verify: robots disallows %s — left unverified', doc.url)
            return 'unfetchable'
        _pace_domain(domain)
        try:
            resp = requests.get(doc.url, headers=_HEADERS, timeout=15, allow_redirects=True)
            if resp.status_code != 200 or not resp.content:
                return 'unfetchable'
            text = resp.content.decode('utf-8', errors='replace')
            doc.raw_content = text[:_RAW_LIMIT]
            doc.save(update_fields=['raw_content'])
        except requests.RequestException:
            # Source unreachable or blocking bots — not evidence of falsehood
            return 'unfetchable'

    # Verify against raw HTML OR readable prose (LLM quotes prose; raw HTML
    # often mangles the sentence with tags)
    hit = quote in text
    if not hit:
        prose = trafilatura.extract(text, include_comments=False, favor_recall=True) or ''
        hit = quote in prose

    if hit:
        Assertion.objects.filter(pk=assertion.pk).update(
            quote_verified=True,
            verified_at=timezone.now(),
            confidence=F('confidence') + _VERIFY_BONUS,
        )
        return 'verified'

    update = {'quote_verified': False, 'verified_at': timezone.now(),
              'confidence': max(20, assertion.confidence - _FAIL_PENALTY)}
    if assertion.status == 'accepted' and update['confidence'] < 50:
        update['status'] = 'candidate'
    Assertion.objects.filter(pk=assertion.pk).update(**update)
    return 'failed'


@shared_task(bind=True, queue='crawl', max_retries=1)
def verify_assertion_quotes(self, assertion_ids: list):
    """Batch-verify quotes for freshly stored assertions."""
    from ingest.pause import is_paused
    if is_paused('verify'):
        return

    assertions = (
        Assertion.objects
        .filter(id__in=assertion_ids, document__isnull=False)
        .exclude(quote_verified=True)
        .select_related('document', 'document__source')
    )
    counts = {'verified': 0, 'failed': 0, 'weak': 0, 'unfetchable': 0, 'skipped': 0}
    for a in assertions:
        try:
            counts[verify_assertion_quote(a)] += 1
        except Exception as exc:
            logger.warning('verify_assertion_quote %s error: %s', a.pk, exc)
            counts['skipped'] += 1

    logger.info(
        'verify_assertion_quotes: %d checked — %d verified, %d failed, '
        '%d weak, %d unfetchable, %d skipped',
        sum(counts.values()), counts['verified'], counts['failed'],
        counts['weak'], counts['unfetchable'], counts['skipped'],
    )
    return counts
