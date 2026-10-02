"""Stage 1 — Acquire.

Fetches a URL, hashes the content for deduplication, creates a Document row
(with the raw response preserved in raw_content), and enqueues the parse task.
Never calls the LLM. Honors robots.txt and paces requests per domain.
"""
import hashlib
import logging
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests
from celery import shared_task
from django.db import transaction
from django.utils import timezone

from core.models import Source, Document

logger = logging.getLogger(__name__)

_HEADERS = {
    'User-Agent': (
        'EigenGraph/0.1 (space-industry knowledge graph; '
        # TODO: still the registered contact domain — switch to eigengraph.io
        # once that domain is live and mail is reachable there.
        'contact@forespace.io) +https://forespace.io/bot'
    ),
    'Accept': 'text/html,application/xhtml+xml,*/*',
}

_UA_TOKEN = 'EigenGraph'
_DOMAIN_PACE_SECONDS = 2.0
_ROBOTS_CACHE_TTL = 24 * 3600


def _robots_parser(domain: str) -> urllib.robotparser.RobotFileParser | None:
    """Fetch and cache a domain's robots.txt. Returns None if unreachable
    (default: allow — most professional crawlers fail open on missing robots)."""
    import redis as _redis
    from django.conf import settings as _settings

    key = f'eigengraph:robots:{domain}'
    try:
        r = _redis.from_url(_settings.CELERY_BROKER_URL)
        cached = r.get(key)
        if cached is not None:
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(cached.decode('utf-8', errors='replace').splitlines())
            return rp
    except Exception:
        r = None

    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(f'https://{domain}/robots.txt')
    try:
        resp = requests.get(
            f'https://{domain}/robots.txt',
            headers=_HEADERS, timeout=10, allow_redirects=True,
        )
        if resp.status_code == 200 and resp.text:
            rp.parse(resp.text.splitlines())
            if r is not None:
                try:
                    r.setex(key, _ROBOTS_CACHE_TTL, resp.text[:100_000])
                except Exception:
                    pass
        else:
            return None  # no robots.txt (404 etc.) — fail open
    except Exception as exc:
        logger.debug('robots fetch failed for %s: %s', domain, exc)
        return None
    return rp


def _pace_domain(domain: str) -> None:
    """Simple per-domain politeness: never two fetches within _DOMAIN_PACE_SECONDS."""
    import redis as _redis
    from django.conf import settings as _settings

    key = f'eigengraph:crawl:last:{domain}'
    try:
        r = _redis.from_url(_settings.CELERY_BROKER_URL)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            acquired = r.set(key, '1', nx=True, ex=int(_DOMAIN_PACE_SECONDS))
            if acquired:
                return
            time.sleep(0.4)
    except Exception:
        return  # Redis down — crawl anyway


@shared_task(bind=True, queue='crawl', max_retries=3, default_retry_delay=60)
def crawl_url(
    self,
    url: str,
    source_name: str,
    source_kind: str = 'trade_press',
    source_trust: int = 60,
):
    """Fetch *url*, deduplicate by SHA-256, create Document, enqueue parse."""
    from ingest.pause import is_paused
    if is_paused('crawl'):
        return

    parsed = urlparse(url)
    domain = parsed.netloc

    # Politeness: robots.txt + per-domain pacing
    rp = _robots_parser(domain)
    if rp is not None and not rp.can_fetch(_UA_TOKEN, url):
        logger.info('crawl_url: robots.txt disallows %s — skipping', url)
        return {'status': 'robots_disallowed', 'url': url}
    _pace_domain(domain)

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=30, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning('crawl_url failed for %s: %s', url, exc)
        raise self.retry(exc=exc)

    raw_bytes = resp.content
    raw_text = raw_bytes.decode('utf-8', errors='replace')
    sha256 = hashlib.sha256(raw_bytes).hexdigest()

    if Document.objects.filter(content_sha256=sha256).exists():
        logger.info('Skipping duplicate %s (sha256=%s…)', url, sha256[:12])
        return {'status': 'duplicate', 'sha256': sha256}

    domain_short = domain[:255]
    source, _ = Source.objects.get_or_create(
        name=source_name,
        defaults={'kind': source_kind, 'base_trust': source_trust, 'domain': domain_short},
    )

    with transaction.atomic():
        doc = Document.objects.create(
            source=source,
            url=url,
            content_sha256=sha256,
            storage_key='inline',        # no object storage yet — raw kept in DB
            media_type=(resp.headers.get('Content-Type') or '')[:100],
            raw_content=raw_text,        # preserved forever — parse never touches this
            text_content=raw_text,       # working copy, replaced by the parse stage
            pipeline_status='fetched',
        )

    from ingest.tasks.parse import parse_document
    parse_document.delay(str(doc.id))

    logger.info('crawl_url: fetched %s → doc %s', url, doc.id)
    return {'status': 'created', 'document_id': str(doc.id)}
