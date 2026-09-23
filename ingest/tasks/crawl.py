"""Stage 1 — Acquire.

Fetches a URL, hashes the content for deduplication, creates a Document row,
and enqueues the parse task.  Never calls the LLM.
"""
import hashlib
import logging
from urllib.parse import urlparse

import requests
from celery import shared_task
from django.db import transaction
from django.utils import timezone

from core.models import Source, Document

logger = logging.getLogger(__name__)

_HEADERS = {
    'User-Agent': (
        'ForeSpace/0.1 (space-industry knowledge graph; '
        'contact@forespace.io) +https://forespace.io/bot'
    ),
    'Accept': 'text/html,application/xhtml+xml,*/*',
}


@shared_task(bind=True, queue='crawl', max_retries=3, default_retry_delay=60)
def crawl_url(
    self,
    url: str,
    source_name: str,
    source_kind: str = 'trade_press',
    source_trust: int = 60,
):
    from ingest.pause import is_paused
    if is_paused('crawl'):
        return
    """Fetch *url*, deduplicate by SHA-256, create Document, enqueue parse."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=30, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning('crawl_url failed for %s: %s', url, exc)
        raise self.retry(exc=exc)

    raw_bytes = resp.content
    sha256 = hashlib.sha256(raw_bytes).hexdigest()

    if Document.objects.filter(content_sha256=sha256).exists():
        logger.info('Skipping duplicate %s (sha256=%s…)', url, sha256[:12])
        return {'status': 'duplicate', 'sha256': sha256}

    domain = urlparse(url).netloc[:255]
    source, _ = Source.objects.get_or_create(
        name=source_name,
        defaults={'kind': source_kind, 'base_trust': source_trust, 'domain': domain},
    )

    with transaction.atomic():
        doc = Document.objects.create(
            source=source,
            url=url,
            content_sha256=sha256,
            storage_key='inline',        # no object storage in Phase 2
            media_type=(resp.headers.get('Content-Type') or '')[:100],
            text_content=raw_bytes.decode('utf-8', errors='replace'),
            pipeline_status='fetched',
        )

    from ingest.tasks.parse import parse_document
    parse_document.delay(str(doc.id))

    logger.info('crawl_url: fetched %s → doc %s', url, doc.id)
    return {'status': 'created', 'document_id': str(doc.id)}
