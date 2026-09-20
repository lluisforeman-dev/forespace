"""Stage 5b — Synthesis.

When assertions for the same (entity, attribute) conflict,
ask the LLM to reason about the most likely truth and produce a single
accepted assertion. Both conflicting assertions are marked superseded.
"""
import hashlib
import json
import logging
import re
import time

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from psycopg2.extras import DateTimeTZRange

from core.models import Assertion, Document, Source
from ingest.ai import get_client
from ingest.cost import log_call
from ingest.prompts import get_prompt

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are a structured data synthesiser for a space-industry knowledge graph.
Given conflicting claims about the same fact for a specific entity, determine the most likely true value.

Return JSON only (no markdown fences):
{
  "value": <most likely true value as string or number>,
  "confidence": <integer 0-100 reflecting certainty given the evidence>,
  "reasoning": <1-3 sentences explaining your choice>
}

Confidence guidelines:
- Sources largely agree on magnitude, differ on exact figure → 65-80
- High-trust official source vs low-trust secondary → 70-85 for official
- Sources genuinely contradict with no clear winner → 45-65
- Possible unit mismatch (×1000, kg vs lb, M vs B) → resolve it, confidence 60-75
- Never exceed 90 for a synthesised claim"""


def _map_value(value, unit, datatype: str) -> dict:
    unit = str(unit)[:20] if unit else unit
    if datatype in ('int', 'decimal', 'money') and value is not None:
        try:
            return {'value_num': float(value), 'unit': unit}
        except (TypeError, ValueError):
            return {'value_text': str(value)[:500]}
    if datatype == 'date' and value is not None:
        parsed = parse_date(str(value))
        if parsed:
            return {'value_date': parsed}
    if datatype == 'bool' and value is not None:
        return {'value_bool': bool(value)}
    return {'value_text': str(value)[:500] if value is not None else ''}


@shared_task(bind=True, queue='adjudicate', max_retries=1, default_retry_delay=60)
def synthesize_conflict(self, entity_id: str, attribute_key: str, assertion_ids: list):
    """LLM synthesis of conflicting assertions → single accepted assertion."""
    assertions = list(
        Assertion.objects
        .filter(pk__in=assertion_ids)
        .select_related('attribute', 'document__source', 'entity')
    )
    if len(assertions) < 2:
        return

    attr = assertions[0].attribute
    entity = assertions[0].entity

    evidence_lines = []
    for a in assertions:
        doc = getattr(a, 'document', None)
        src_name = getattr(getattr(doc, 'source', None), 'name', 'unknown') if doc else 'unknown'
        src_trust = (
            getattr(doc, 'trust_override', None)
            or getattr(getattr(doc, 'source', None), 'base_trust', 55)
        ) if doc else 55
        val = a.value_text or a.value_num or a.value_bool or a.value_date or '?'
        unit_str = f' {a.unit}' if a.unit else ''
        evidence_lines.append(
            f'- Source: {src_name} (trust {src_trust}/100)\n'
            f'  Value: {val}{unit_str}\n'
            f'  Quote: {a.quote or "(none)"}'
        )

    evidence = '\n'.join(evidence_lines)
    user_msg = (
        f'Entity: {entity.canonical_name} ({entity.entity_type})\n'
        f'Attribute: {attr.label} — {attr.description}\n\n'
        f'Conflicting claims:\n{evidence}\n\n'
        f'What is the most likely true value for "{attr.label}"?'
    )

    model = settings.AI_MODEL
    try:
        t0 = time.monotonic()
        resp = get_client().chat.completions.create(
            model=model,
            messages=[
                {'role': 'system', 'content': get_prompt('synthesize_conflict', _SYSTEM)},
                {'role': 'user', 'content': user_msg},
            ],
            max_tokens=400,
            temperature=0,
        )
        log_call('synthesize_conflict', model, resp,
                 duration_ms=int((time.monotonic() - t0) * 1000))
        content = resp.choices[0].message.content
        if not content:
            logger.warning('synthesize_conflict entity=%s attr=%s: empty response', entity_id, attribute_key)
            return
        raw = content.strip()
    except Exception as exc:
        logger.error('synthesize_conflict entity=%s attr=%s: %s', entity_id, attribute_key, exc)
        raise self.retry(exc=exc)

    text = raw
    match = re.search(r'```(?:json)?\s*([\s\S]+?)\s*```', text)
    if match:
        text = match.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.error(
            'synthesize_conflict bad JSON for entity=%s attr=%s: %r',
            entity_id, attribute_key, raw[:200],
        )
        return

    synth_value = data.get('value')
    synth_confidence = min(90, max(30, int(data.get('confidence', 60))))
    reasoning = data.get('reasoning', '')

    if synth_value is None:
        logger.warning(
            'synthesize_conflict: LLM returned null value for entity=%s attr=%s',
            entity_id, attribute_key,
        )
        return

    value_fields = _map_value(synth_value, None, attr.datatype)

    source, _ = Source.objects.get_or_create(
        name='LLM Synthesis',
        defaults={'kind': 'llm', 'base_trust': 70, 'domain': 'internal'},
    )
    content_sha = hashlib.sha256(raw.encode()).hexdigest()
    synth_doc, _ = Document.objects.get_or_create(
        content_sha256=content_sha,
        defaults={
            'source': source,
            'storage_key': 'synthesis',
            'text_content': reasoning,
            'pipeline_status': 'done',
            'title': f'Synthesis: {entity.canonical_name} / {attribute_key}',
        },
    )

    now = timezone.now()
    with transaction.atomic():
        # Supersede ALL open-ended assertions for this (entity, attribute) —
        # not just the conflicting ones — to satisfy the no_overlapping_validity constraint.
        Assertion.objects.filter(
            entity_id=entity_id,
            attribute_id=attribute_key,
            superseded_at__isnull=True,
        ).update(superseded_at=now, status='superseded')
        synth = Assertion.objects.create(
            entity_id=entity_id,
            attribute_id=attribute_key,
            document=synth_doc,
            quote=f'[Synthesised] {reasoning}',
            method='synthesis',
            confidence=synth_confidence,
            status='accepted',
            valid_range=DateTimeTZRange(now, None),
            **value_fields,
        )

    logger.info(
        'synthesize_conflict: entity=%s attr=%s → value=%s conf=%d (from %d sources)',
        entity_id, attribute_key, synth_value, synth_confidence, len(assertions),
    )
    return synth.pk
