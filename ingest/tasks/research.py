"""Sonar-powered research task.

Calls Perplexity Sonar (via OpenRouter) which searches the web in real time,
then writes Assertions directly — no crawl/parse/triage steps needed.
"""
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone as tz

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from psycopg2.extras import DateTimeTZRange

from core.models import Assertion, AttributeDef, Document, ExtractionRun, Source
from ingest.ai import get_client
from ingest.confidence import score as compute_score
from ingest.cost import log_call
from ingest.tasks.resolve import resolve_mention

logger = logging.getLogger(__name__)

_SONAR_SOURCE_NAME = 'Perplexity Sonar'

_SYSTEM = """\
You are a structured data extractor for a space-industry knowledge graph.
Search the web for current, verifiable facts and return them as JSON.

For EACH claim provide:
  subject_mention  - exact company or entity name
  attribute_key    - one of the allowed keys below (no others)
  value            - the extracted value as string or number, or null
  unit             - unit of measurement (e.g. "USD", "kg") or null
  as_of            - ISO date YYYY-MM-DD when the value was true, or null
  quote            - exact sentence from your web source supporting the claim
  source_url       - URL of the web page where you found this fact, or null
  extractor_confidence - "high", "medium", or "low"

RULES:
- Only use attribute_key values from the allowed list.
- Only report facts found in web sources.
- Return valid JSON only, no markdown fences: {"claims": [...]}"""


def _attr_vocab() -> str:
    rows = AttributeDef.objects.values('key', 'label', 'description', 'datatype')
    if not rows:
        return ''
    return '\n'.join(
        f"- {r['key']} ({r['datatype']}): {r['label']}. {r['description']}"
        for r in rows
    )


def _parse_json(text: str) -> dict:
    text = text.strip()
    match = re.search(r'```(?:json)?\s*([\s\S]+?)\s*```', text)
    if match:
        text = match.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Response was truncated — salvage every complete claim object
        candidates = re.findall(r'\{(?:[^{}]|\{[^{}]*\})*\}', text)
        claims = []
        for c in candidates:
            try:
                obj = json.loads(c)
                if isinstance(obj, dict) and 'attribute_key' in obj:
                    claims.append(obj)
            except json.JSONDecodeError:
                pass
        if claims:
            logger.warning('Truncated JSON: recovered %d claims', len(claims))
            return {'claims': claims}
        raise


def _map_value(value, unit, datatype: str) -> dict:
    if datatype in ('int', 'decimal', 'money') and value is not None:
        try:
            return {'value_num': float(value), 'unit': unit}
        except (TypeError, ValueError):
            return {'value_text': str(value)}
    if datatype == 'date' and value is not None:
        parsed = parse_date(str(value))
        if parsed:
            return {'value_date': parsed}
    if datatype == 'bool' and value is not None:
        return {'value_bool': bool(value)}
    return {'value_text': str(value) if value is not None else ''}


def _sonar_source() -> Source:
    source, _ = Source.objects.get_or_create(
        name=_SONAR_SOURCE_NAME,
        defaults={'kind': 'llm', 'base_trust': 65, 'domain': 'perplexity.ai'},
    )
    return source


@shared_task(bind=True, queue='extract', max_retries=2, default_retry_delay=30)
def research_topic(self, topic: str, topic_type: str = 'company'):
    """
    Use Perplexity Sonar to research a topic and write Assertions directly to the DB.
    topic_type: 'company' | 'question' | 'news'
    """
    vocab = _attr_vocab()
    if not vocab:
        logger.error('research_topic: AttributeDef is empty — migration 0008 may not have run')
        return

    if topic_type == 'company':
        user_msg = (
            f'Research the space-industry company or organisation "{topic}". '
            f'Find current facts from recent web sources and extract as many structured claims as possible.\n\n'
            f'Allowed attribute keys:\n{vocab}'
        )
    elif topic_type == 'news':
        user_msg = (
            f'What are the most significant space-industry developments from the past 7 days? '
            f'For each event identify the organisations involved and extract structured facts.\n\n'
            f'Allowed attribute keys:\n{vocab}'
        )
    else:  # question
        user_msg = (
            f'Research the following question about the space industry: "{topic}"\n'
            f'Find and extract all relevant factual claims from recent web sources.\n\n'
            f'Allowed attribute keys:\n{vocab}'
        )

    model = settings.AI_MODEL_SONAR
    run = ExtractionRun.objects.create(
        task=f'research_{topic_type}',
        prompt_sha256=hashlib.sha256(user_msg.encode()).hexdigest(),
        model=model,
        code_version='sonar-v1',
    )

    try:
        t0 = time.monotonic()
        resp = get_client().chat.completions.create(
            model=model,
            messages=[
                {'role': 'system', 'content': _SYSTEM},
                {'role': 'user', 'content': user_msg},
            ],
            max_tokens=30000,
            temperature=0,
        )
        log_call(f'research_{topic_type}', model, resp,
                 run=run, duration_ms=int((time.monotonic() - t0) * 1000))
        raw = resp.choices[0].message.content
        data = _parse_json(raw)
        claims = data.get('claims', [])
    except Exception as exc:
        logger.error('research_topic "%s" error: %s', topic, exc)
        run.status = 'failed'
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'finished_at'])
        raise self.retry(exc=exc)

    # Synthetic Document to hold the Sonar response for traceability
    source = _sonar_source()
    content_sha = hashlib.sha256(raw.encode()).hexdigest()
    doc, _ = Document.objects.get_or_create(
        content_sha256=content_sha,
        defaults={
            'source': source,
            'storage_key': 'sonar',
            'text_content': raw,
            'pipeline_status': 'done',
            'title': f'Sonar: {topic[:200]}',
        },
    )

    valid_attrs = {a.key: a for a in AttributeDef.objects.all()}
    accepted = rejected = 0
    new_ids: list[int] = []

    for claim in claims:
        attr_key = claim.get('attribute_key')
        if attr_key not in valid_attrs:
            logger.debug('Unknown attribute_key "%s", skipping', attr_key)
            rejected += 1
            continue

        attr = valid_attrs[attr_key]
        mention = claim.get('subject_mention') or topic
        quote = claim.get('quote') or mention
        value = claim.get('value')
        unit = claim.get('unit')
        extractor_conf = claim.get('extractor_confidence', 'medium')
        source_url = claim.get('source_url') or None

        # Use a per-URL document if the LLM provided a source link
        if source_url:
            url_sha = hashlib.sha256(source_url.encode()).hexdigest()
            claim_doc, _ = Document.objects.get_or_create(
                content_sha256=url_sha,
                defaults={
                    'source': source,
                    'url': source_url,
                    'storage_key': 'sonar-url',
                    'title': source_url[:200],
                    'pipeline_status': 'done',
                },
            )
        else:
            claim_doc = doc

        confidence = compute_score(
            extractor_confidence=extractor_conf,
            source_base_trust=65,
            source_kind='llm',
            document_published_at=None,
            volatility_days=attr.volatility_days,
        )

        as_of_raw = claim.get('as_of')
        as_of = parse_date(str(as_of_raw)) if as_of_raw else None
        range_start = (
            datetime.combine(as_of, datetime.min.time()).replace(tzinfo=tz.utc)
            if as_of else timezone.now()
        )

        try:
            with transaction.atomic():
                entity_id = resolve_mention(mention, document_id=str(doc.id))
                a = Assertion.objects.create(
                    entity_id=entity_id,
                    attribute_id=attr_key,
                    document=claim_doc,
                    run=run,
                    quote=quote,
                    method='structured_api',
                    confidence=confidence,
                    status='candidate',
                    valid_range=DateTimeTZRange(range_start, None),
                    **_map_value(value, unit, attr.datatype),
                )
            new_ids.append(a.pk)
            accepted += 1
        except Exception as e:
            logger.debug('Skipping duplicate claim %s.%s: %s', mention, attr_key, e)
            rejected += 1

    run.status = 'completed'
    run.finished_at = timezone.now()
    run.stats = {'accepted': accepted, 'rejected': rejected, 'topic': topic}
    run.save(update_fields=['status', 'finished_at', 'stats'])
    logger.info('research_topic "%s": %d accepted, %d rejected', topic, accepted, rejected)

    if new_ids:
        from ingest.tasks.adjudicate import adjudicate_assertions
        from ingest.tasks.project import refresh_entity_current
        adjudicate_assertions.delay(new_ids)
        refresh_entity_current.apply_async(countdown=5)

    entity_ids = list(
        Assertion.objects.filter(pk__in=new_ids)
        .values_list('entity_id', flat=True).distinct()
    )
    if entity_ids:
        from ingest.tasks.classify import classify_entity
        for eid in entity_ids:
            classify_entity.apply_async(args=[str(eid), str(run.pk)], countdown=10)

    return {'accepted': accepted, 'rejected': rejected}
