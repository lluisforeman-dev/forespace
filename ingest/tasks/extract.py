"""Stage 4 — Extract.

LLM-based structured extraction with mandatory quote verification (§7c).
Every claim that cannot be verified as a verbatim substring of the document is rejected.
No current graph state is passed to the LLM — see §9 (anti-self-confirmation).
"""
import hashlib
import json
import logging
import subprocess

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from psycopg2.extras import DateTimeTZRange

from core.models import Assertion, AttributeDef, Document, ExtractionRun
from ingest.ai import get_client
from ingest.confidence import score as compute_score
from ingest.cost import log_call
from ingest.schemas import ExtractedClaim, ExtractionResult
from ingest.tasks.resolve import resolve_mention

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are a structured data extractor for a space-industry knowledge graph.
Extract factual claims from the document text below.

For EACH claim provide:
  subject_mention       – exact company/entity name as it appears in the text
  subject_mention_full  – if subject_mention is an acronym or abbreviation, provide the full
                          expanded name (e.g. "ICGC" → "Institut Cartogràfic i Geològic de Catalunya",
                          "ESA" → "European Space Agency"). Omit (null) if already a full name.
  attribute_key    – one of the allowed keys listed below (no others)
  value            – extracted value as string or number, or null
  unit             – unit of measurement (e.g. "USD", "kg") or null
  as_of            – ISO date (YYYY-MM-DD) when the value was true, or null
  quote            – verbatim sentence from the document that supports the claim
  char_start       – character offset of quote start in the document
  char_end         – character offset of quote end
  extractor_confidence – "high", "medium", or "low"

CRITICAL RULES:
- quote MUST be an exact substring of the document text provided.
- Never invent values. If unsure, use low confidence.
- Only use attribute keys from the allowed list — no others.
- Do not pass graph state into claims (extraction sees only this document).
- Return JSON: {"claims": [...]}"""


def _attr_vocab() -> str:
    rows = AttributeDef.objects.values('key', 'label', 'description', 'datatype')
    if not rows:
        return '(run: python manage.py seed_attributes)'
    return '\n'.join(
        f"- {r['key']} ({r['datatype']}): {r['label']}. {r['description']}"
        for r in rows
    )


def _prompt_hash(system: str, vocab: str) -> str:
    return hashlib.sha256((system + vocab).encode()).hexdigest()


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'], text=True
        ).strip()
    except Exception:
        return 'unknown'


def _map_value(claim: ExtractedClaim, datatype: str) -> dict:
    """Map claim.value to the correct typed Assertion column."""
    v = claim.value
    if datatype in ('int', 'decimal', 'money') and v is not None:
        try:
            return {'value_num': float(v), 'unit': claim.unit}
        except (TypeError, ValueError):
            return {'value_text': str(v)}
    if datatype == 'date' and v is not None:
        parsed = parse_date(str(v))
        if parsed:
            return {'value_date': parsed}
    if datatype == 'bool' and v is not None:
        return {'value_bool': bool(v)}
    return {'value_text': str(v) if v is not None else None}


def _ensure_alias(entity_id: str, surface_form: str, document_id: str | None) -> None:
    """Add surface_form as an alias for entity_id if not already present."""
    from core.models import EntityAlias
    from core.normalize import normalize_name
    norm = normalize_name(surface_form)
    EntityAlias.objects.get_or_create(
        entity_id=entity_id,
        alias_norm=norm,
        defaults={
            'alias': surface_form,
            'alias_kind': 'abbrev',
            'document_id': document_id,
        },
    )


@shared_task(bind=True, queue='extract', max_retries=2)
def extract_document(self, document_id: str):
    """Run LLM extraction on a triaged document and write candidate assertions."""
    try:
        doc = Document.objects.select_related('source').get(pk=document_id)
    except Document.DoesNotExist:
        return

    text = doc.text_content
    if not text:
        return

    vocab = _attr_vocab()
    p_hash = _prompt_hash(_SYSTEM, vocab)
    model = settings.AI_MODEL_PROSE
    user_msg = f"Allowed attribute keys:\n{vocab}\n\nDocument text:\n---\n{text[:6000]}\n---"

    run = ExtractionRun.objects.create(
        task='company_profile',
        prompt_sha256=p_hash,
        model=model,
        code_version=_git_sha(),
    )

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
            max_tokens=2000,
            temperature=0,
        )
        log_call('extract', model, resp, run=run, duration_ms=int((_time.monotonic() - _t0) * 1000))
        result = ExtractionResult.model_validate_json(resp.choices[0].message.content)
    except Exception as exc:
        logger.error('extract_document %s LLM error: %s', document_id, exc)
        run.status = 'failed'
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'finished_at'])
        raise self.retry(exc=exc)

    valid_attrs = {a.key: a for a in AttributeDef.objects.all()}
    accepted = rejected = 0
    new_assertion_ids: list[int] = []

    for claim in result.claims:
        # ── Quote verification (§7c) — reject anything the model fabricated ──
        if claim.quote.strip() not in text:
            logger.warning('Quote not found in doc %s, rejecting %s', document_id, claim.attribute_key)
            rejected += 1
            continue

        if claim.attribute_key not in valid_attrs:
            logger.warning('Unknown attribute_key "%s", skipping', claim.attribute_key)
            rejected += 1
            continue

        attr = valid_attrs[claim.attribute_key]
        confidence = compute_score(
            extractor_confidence=claim.extractor_confidence,
            source_base_trust=doc.effective_trust,
            source_kind=doc.source.kind,
            document_published_at=doc.published_at,
            volatility_days=attr.volatility_days,
        )

        # Acceptance policy §8c
        status = 'accepted' if confidence >= 50 else 'candidate'

        # valid_range: from as_of (or now) to open
        if claim.as_of:
            from datetime import datetime, timezone as tz
            range_start = datetime.combine(claim.as_of, datetime.min.time()).replace(tzinfo=tz.utc)
        else:
            range_start = timezone.now()
        valid_range = DateTimeTZRange(range_start, None)

        with transaction.atomic():
            # Resolve by full name when available so trigram matching works on the
            # expanded form; then register the acronym as an alias so future
            # mentions of the short form hit Level-2 exact match.
            lookup_name = claim.subject_mention_full or claim.subject_mention
            entity_id = resolve_mention(lookup_name, document_id=document_id)
            if (
                claim.subject_mention_full
                and claim.subject_mention != claim.subject_mention_full
            ):
                _ensure_alias(entity_id, claim.subject_mention, document_id)
            assertion = Assertion.objects.create(
                entity_id=entity_id,
                attribute_id=claim.attribute_key,
                quote=claim.quote,
                char_start=claim.char_start,
                char_end=claim.char_end,
                document=doc,
                run=run,
                method='extracted',
                confidence=confidence,
                status=status,
                valid_range=valid_range,
                **_map_value(claim, attr.datatype),
            )
        new_assertion_ids.append(assertion.pk)
        accepted += 1

    run.status = 'completed'
    run.finished_at = timezone.now()
    run.stats = {'claims_accepted': accepted, 'claims_rejected': rejected}
    run.save(update_fields=['status', 'finished_at', 'stats'])

    Document.objects.filter(pk=document_id).update(pipeline_status='extracted')
    logger.info(
        'extract_document %s: %d accepted, %d rejected',
        document_id, accepted, rejected,
    )

    if new_assertion_ids:
        from ingest.tasks.adjudicate import adjudicate_assertions
        from ingest.tasks.project import refresh_entity_current
        from ingest.tasks.relate import extract_relations
        adjudicate_assertions.delay(new_assertion_ids)
        refresh_entity_current.apply_async(countdown=5)  # slight delay so adjudicate finishes first
        extract_relations.delay(document_id)

    # Classify every entity touched by this extraction
    entity_ids = list(
        Assertion.objects.filter(pk__in=new_assertion_ids).values_list('entity_id', flat=True).distinct()
    )
    if entity_ids:
        from ingest.tasks.classify import classify_entity
        for eid in entity_ids:
            classify_entity.apply_async(args=[str(eid), str(run.pk)], countdown=10)
