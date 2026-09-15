"""Relation extraction task — §5b graph edges.

Extracts typed relations (invested_in, acquired, supplies, launches_for, …)
from a document and writes Relation rows for graph edges.

Anti-self-confirmation (§9): the LLM sees only the document text and the
controlled predicate vocabulary — no existing graph state.
"""
from __future__ import annotations

import json
import logging
from typing import Literal

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from pydantic import BaseModel

from core.models import Document, Entity, PredicateDef, Relation
from ingest.ai import get_client
from ingest.cost import log_call
from ingest.tasks.resolve import resolve_mention

logger = logging.getLogger(__name__)


class ExtractedRelation(BaseModel):
    subject_mention: str           # entity name as it appears in text
    predicate: str                 # predicate key, e.g. 'invested_in'
    object_mention: str            # entity name as it appears in text
    qualifiers: dict               # e.g. {"amount_usd": 5000000, "round_series": "A"}
    quote: str                     # verbatim supporting sentence from the document
    confidence: Literal["high", "medium", "low"]


class RelationResult(BaseModel):
    relations: list[ExtractedRelation]


_SYSTEM = """\
You are a relation extractor for a space-industry knowledge graph.
Read the document and identify relationships between named entities (companies, people, funds).

For each relationship provide:
  subject_mention  – the entity initiating or performing the relationship
  predicate        – one of the allowed predicate keys (no others)
  object_mention   – the entity receiving the relationship
  qualifiers       – a JSON object of extra attributes (empty {} if none)
  quote            – verbatim sentence from the document supporting this relation
  confidence       – "high" | "medium" | "low"

CRITICAL RULES:
- quote MUST be an exact substring of the document text.
- Only use predicate keys from the allowed list.
- Never invent relations not stated in the document.
- Return JSON: {"relations": [...]}"""


def _predicate_vocab() -> str:
    rows = PredicateDef.objects.values('key', 'label', 'description')
    if not rows:
        return '(run: python manage.py seed_predicates)'
    return '\n'.join(f"- {r['key']}: {r['label']}. {r['description']}" for r in rows)


@shared_task(bind=True, queue='extract', max_retries=2)
def extract_relations(self, document_id: str):
    """Extract typed relations from a document and write Relation rows."""
    try:
        doc = Document.objects.select_related('source').get(pk=document_id)
    except Document.DoesNotExist:
        return

    text = doc.text_content
    if not text:
        return

    vocab = _predicate_vocab()
    if 'run: python manage.py' in vocab:
        logger.warning('extract_relations: no predicates found — run seed_predicates first')
        return

    model = settings.AI_MODEL
    user_msg = f"Allowed predicate keys:\n{vocab}\n\nDocument text:\n---\n{text[:6000]}\n---"

    try:
        import time as _time
        _t0 = _time.monotonic()
        resp = get_client().chat.completions.create(
            model=model,
            messages=[
                {'role': 'system', 'content': _SYSTEM},
                {'role': 'user', 'content': user_msg},
            ],
            response_format={'type': 'json_object'},
            max_tokens=1500,
            temperature=0,
        )
        log_call('extract', model, resp, duration_ms=int((_time.monotonic() - _t0) * 1000))
        result = RelationResult.model_validate_json(resp.choices[0].message.content)
    except Exception as exc:
        logger.error('extract_relations %s error: %s', document_id, exc)
        raise self.retry(exc=exc)

    valid_predicates = {p.key for p in PredicateDef.objects.all()}
    created = skipped = 0

    for rel in result.relations:
        # Quote verification — same anti-hallucination guard as assertion extraction
        if rel.quote.strip() not in text:
            logger.warning('extract_relations: quote not found in doc %s, skipping', document_id)
            skipped += 1
            continue

        if rel.predicate not in valid_predicates:
            logger.warning('extract_relations: unknown predicate "%s", skipping', rel.predicate)
            skipped += 1
            continue

        conf_map = {'high': 85, 'medium': 65, 'low': 45}
        confidence = conf_map[rel.confidence]

        with transaction.atomic():
            subject_id = resolve_mention(rel.subject_mention, document_id=document_id)
            object_id = resolve_mention(rel.object_mention, document_id=document_id)

            Relation.objects.update_or_create(
                subject_id=subject_id,
                predicate_id=rel.predicate,
                object_id=object_id,
                document=doc,
                defaults={
                    'qualifiers': rel.qualifiers or {},
                    'confidence': confidence,
                    'method': 'extracted',
                    'status': 'accepted' if confidence >= 65 else 'candidate',
                },
            )
        created += 1

    logger.info('extract_relations %s: %d created, %d skipped', document_id, created, skipped)

    if created:
        from ingest.tasks.project import refresh_relation_current
        refresh_relation_current.apply_async(countdown=5)
