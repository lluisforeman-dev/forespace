"""Stage 3 — Triage.

Cheap LLM call: does this document contain any extractable space-industry facts?
Most documents won't — this gate keeps the expensive extract queue lean.
"""
import json
import logging

from celery import shared_task
from django.conf import settings

from core.models import Document
from ingest.ai import get_client

logger = logging.getLogger(__name__)

_SYSTEM = (
    'You are a relevance classifier for a space-industry knowledge graph. '
    'Reply only with valid JSON.'
)

_USER = """\
Does the following document contain factual claims about a space-industry company,
launch vehicle, satellite programme, or related organisation that could be extracted
into structured data (e.g. funding round, founding year, employee count, payload capacity)?

---
{snippet}
---

Reply: {{"relevant": true, "reason": "..."}} or {{"relevant": false, "reason": "..."}}"""


@shared_task(bind=True, queue='triage', max_retries=2)
def triage_document(self, document_id: str):
    """Route: relevant → extract queue; irrelevant → mark skipped."""
    try:
        doc = Document.objects.get(pk=document_id)
    except Document.DoesNotExist:
        return

    if not doc.text_content:
        Document.objects.filter(pk=document_id).update(pipeline_status='skipped')
        return

    snippet = doc.text_content[:2000]

    try:
        import time
        from ingest.cost import log_call
        t0 = time.monotonic()
        resp = get_client().chat.completions.create(
            model=settings.AI_MODEL_FAST,
            messages=[
                {'role': 'system', 'content': _SYSTEM},
                {'role': 'user', 'content': _USER.format(snippet=snippet)},
            ],
            response_format={'type': 'json_object'},
            max_tokens=100,
            temperature=0,
        )
        log_call('triage', settings.AI_MODEL, resp, duration_ms=int((time.monotonic() - t0) * 1000))
        content = resp.choices[0].message.content
        if not content:
            logger.warning('triage_document %s: empty response, marking skipped', document_id)
            Document.objects.filter(pk=document_id).update(pipeline_status='skipped')
            return
        result = json.loads(content)
        relevant = bool(result.get('relevant', False))
    except Exception as exc:
        logger.warning('triage_document %s error: %s', document_id, exc)
        raise self.retry(exc=exc)

    if not relevant:
        Document.objects.filter(pk=document_id).update(pipeline_status='skipped')
        logger.info('triage: %s not relevant — %s', document_id, result.get('reason', ''))
        return

    Document.objects.filter(pk=document_id).update(pipeline_status='triaged')

    from ingest.tasks.extract import extract_document
    extract_document.delay(document_id)
    logger.info('triage: %s relevant, queued for extraction', document_id)
