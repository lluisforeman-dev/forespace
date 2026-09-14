"""Stage 2 — Parse.

Converts raw HTML stored in text_content into clean prose using trafilatura,
then enqueues triage.  No LLM calls.
"""
import logging

import trafilatura
from celery import shared_task

from core.models import Document

logger = logging.getLogger(__name__)


@shared_task(bind=True, queue='parse', max_retries=2)
def parse_document(self, document_id: str):
    """Extract clean text from raw HTML, update Document, enqueue triage."""
    try:
        doc = Document.objects.select_related('source').get(pk=document_id)
    except Document.DoesNotExist:
        logger.error('parse_document: document %s not found', document_id)
        return

    raw_html = doc.text_content
    if not raw_html:
        Document.objects.filter(pk=document_id).update(pipeline_status='failed')
        return

    clean_text = trafilatura.extract(
        raw_html,
        include_tables=True,
        include_comments=False,
        deduplicate=True,
        favor_recall=True,
    )

    if not clean_text:
        logger.info('parse_document: trafilatura returned no text for %s', document_id)
        Document.objects.filter(pk=document_id).update(pipeline_status='skipped')
        return

    updates = {'text_content': clean_text, 'pipeline_status': 'parsed'}

    meta = trafilatura.extract_metadata(raw_html)
    if meta:
        if meta.title and not doc.title:
            updates['title'] = meta.title[:500]
        if meta.date and not doc.published_at:
            from django.utils.dateparse import parse_datetime
            dt = parse_datetime(meta.date)
            if dt:
                updates['published_at'] = dt

    Document.objects.filter(pk=document_id).update(**updates)

    from ingest.tasks.triage import triage_document
    triage_document.delay(document_id)
    logger.info('parse_document: %s → %d chars', document_id, len(clean_text))
