"""Sonar-powered research task.

Calls Perplexity Sonar (via OpenRouter) which searches the web in real time,
then writes three types of structured knowledge directly to the DB:
  1. Assertions  — key-value facts with confidence
  2. Events      — discrete moments in history (funding, launches, pivots, failures)
  3. Fragments   — rich narrative paragraphs (tech, strategy, challenges, competition)
"""
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone as tz

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from psycopg2.extras import DateTimeTZRange

from core.models import Assertion, AttributeDef, Document, Event, ExtractionRun, KnowledgeFragment, Source
from ingest.ai import get_client
from ingest.confidence import domain_trust, score as compute_score
from ingest.cost import log_call
from ingest.tasks.resolve import resolve_mention

logger = logging.getLogger(__name__)

_SONAR_SOURCE_NAME = 'Perplexity Sonar'

_SYSTEM = """\
You are a structured data extractor for a space-industry knowledge graph.
Search the web for current, verifiable information and return JSON with THREE sections.

━━ SECTION 1: claims ━━
Structured key-value facts. For EACH claim:
  subject_mention        - exact entity name
  subject_type           - organization | asset | person | facility | event | program
  attribute_key          - one of the ALLOWED KEYS listed below (no others)
  value                  - extracted value as string or number, or null
  unit                   - unit of measurement or null
  as_of                  - ISO date YYYY-MM-DD when the value was true, or null
  quote                  - exact sentence from your web source
  source_url             - URL of the web page, or null
  extractor_confidence   - "high", "medium", or "low"

━━ SECTION 2: events ━━
Discrete events in an entity's history. For EACH event:
  subject_mention  - exact entity name (primary subject)
  event_type       - funding_round | launch | contract_award | partnership |
                     acquisition | failure | pivot | regulatory | milestone | leadership
  title            - short descriptive title (e.g. "Series B — £40M led by Airbus Ventures")
  date             - ISO date YYYY-MM-DD, YYYY-MM, or YYYY — best precision available
  description      - 2-3 sentences: what happened and why it matters
  amount_usd       - numeric amount in USD if applicable, else null
  significance     - "high" | "medium" | "low"
  participants     - list of other entity names directly involved
  source_url       - URL, or null
  confidence       - "high" | "medium" | "low"

━━ SECTION 3: fragments ━━
Rich narrative paragraphs about entities. For EACH meaningful piece of intelligence:
  subject_mention       - exact entity name
  category              - technical | financial | competitive | regulatory |
                          strategic | operational | people | challenge
  text                  - verbatim or close paraphrase of a full paragraph of intelligence
  date_of_information   - approximate date the info was current, YYYY-MM or YYYY, or null
  source_url            - URL, or null

RULES:
- In claims, only use attribute_key values from the allowed list below.
- Extract as many events and fragments as you find — do not summarise, capture everything.
- Fragments must be substantive (> 2 sentences). Capture challenges, pivots, tech choices,
  competitive dynamics, strategic rationale, key people decisions.
- Return valid JSON only, no markdown fences:
  {"claims": [...], "events": [...], "fragments": [...]}"""


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
    m = re.search(r'```(?:json)?\s*([\s\S]+?)\s*```', text)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Truncated response — salvage complete claim objects
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
            return {'claims': claims, 'events': [], 'fragments': []}
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


def _get_or_create_url_doc(source_url: str, sonar_source: Source) -> Document:
    src_trust = domain_trust(source_url)
    url_sha = hashlib.sha256(source_url.encode()).hexdigest()
    doc, _ = Document.objects.get_or_create(
        content_sha256=url_sha,
        defaults={
            'source': sonar_source,
            'url': source_url,
            'storage_key': 'sonar-url',
            'title': source_url[:200],
            'pipeline_status': 'done',
            'trust_override': src_trust,
        },
    )
    return doc


def _parse_date_flexible(date_str) -> tuple:
    """Parse YYYY, YYYY-MM, or YYYY-MM-DD. Returns (date, precision)."""
    if not date_str:
        return None, 'year'
    s = str(date_str).strip()
    try:
        if len(s) >= 10:
            return parse_date(s[:10]), 'day'
        if len(s) == 7:
            return parse_date(s + '-01'), 'month'
        if len(s) == 4:
            return parse_date(s + '-01-01'), 'year'
    except Exception:
        pass
    return None, 'year'


def _seen_urls_for_company(topic: str) -> str:
    from django.db.models import Q
    from core.models import Entity
    from core.normalize import normalize_name
    norm = normalize_name(topic)
    entity = (
        Entity.objects
        .filter(status__in=('active', 'stub'))
        .filter(Q(canonical_name__iexact=topic) | Q(aliases__alias_norm=norm))
        .first()
    )
    if not entity:
        return ''
    urls = (
        Document.objects
        .filter(assertions__entity=entity, url__isnull=False)
        .exclude(url='')
        .values_list('url', flat=True)
        .distinct()
    )
    return '\n'.join(urls)


def _seen_urls_recent(days: int, limit: int = 50) -> str:
    cutoff = datetime.now(tz=tz.utc) - timedelta(days=days)
    urls = (
        Document.objects
        .filter(url__isnull=False, fetched_at__gte=cutoff)
        .exclude(url='')
        .values_list('url', flat=True)
        .order_by('-fetched_at')[:limit]
    )
    return '\n'.join(urls)


def _sonar_source() -> Source:
    source, _ = Source.objects.get_or_create(
        name=_SONAR_SOURCE_NAME,
        defaults={'kind': 'llm', 'base_trust': 65, 'domain': 'perplexity.ai'},
    )
    return source


_CONF_MAP = {'high': 78, 'medium': 62, 'low': 45}

_TYPE_TO_TOPIC = {
    'organization': 'company',
    'asset': 'company',
    'program': 'question',
    'facility': 'question',
    'person': 'question',
    'event': 'question',
}


def _store_events(events: list, name_to_id: dict, fallback_doc: Document, sonar_source: Source) -> int:
    """Persist extracted events, resolving participant entity names."""
    valid_types = {t[0] for t in Event.EVENT_TYPES}
    valid_sig = {s[0] for s in Event.SIGNIFICANCE}
    stored = 0

    for ev in events:
        mention = (ev.get('subject_mention') or '').strip()
        description = (ev.get('description') or '').strip()
        title = (ev.get('title') or '').strip()
        if not mention or not description or not title:
            continue

        entity_id = name_to_id.get(mention)
        if not entity_id:
            try:
                entity_id = resolve_mention(mention, document_id=str(fallback_doc.id), entity_type='organization')
            except Exception:
                continue

        event_type = ev.get('event_type', 'milestone')
        if event_type not in valid_types:
            event_type = 'milestone'

        date_val, date_precision = _parse_date_flexible(ev.get('date'))

        significance = ev.get('significance', 'medium')
        if significance not in valid_sig:
            significance = 'medium'

        confidence = _CONF_MAP.get(ev.get('confidence', 'medium'), 62)

        source_url = ev.get('source_url')
        ev_doc = _get_or_create_url_doc(source_url, sonar_source) if source_url else fallback_doc

        amount_raw = ev.get('amount_usd')
        amount_usd = None
        if amount_raw is not None:
            try:
                amount_usd = float(amount_raw)
            except (TypeError, ValueError):
                pass

        try:
            event_obj = Event.objects.create(
                entity_id=entity_id,
                event_type=event_type,
                title=title[:500],
                date=date_val,
                date_precision=date_precision,
                description=description,
                amount_usd=amount_usd,
                significance=significance,
                confidence=confidence,
                source=ev_doc,
            )
            for pname in (ev.get('participants') or [])[:8]:
                pname = str(pname).strip()
                if not pname:
                    continue
                pid = name_to_id.get(pname)
                if not pid:
                    try:
                        pid = resolve_mention(pname, document_id=str(fallback_doc.id), entity_type='organization')
                    except Exception:
                        continue
                if pid != entity_id:
                    event_obj.participants.add(pid)
            stored += 1
        except Exception as e:
            logger.warning('_store_events: %s — %s', mention, e)

    return stored


def _store_fragments(fragments: list, name_to_id: dict, fallback_doc: Document, sonar_source: Source) -> int:
    """Persist knowledge fragments."""
    valid_cats = {c[0] for c in KnowledgeFragment.CATEGORIES}
    stored = 0

    for frag in fragments:
        mention = (frag.get('subject_mention') or '').strip()
        text = (frag.get('text') or '').strip()
        if not mention or len(text) < 40:
            continue

        entity_id = name_to_id.get(mention)
        if not entity_id:
            try:
                entity_id = resolve_mention(mention, document_id=str(fallback_doc.id), entity_type='organization')
            except Exception:
                continue

        category = frag.get('category', 'strategic')
        if category not in valid_cats:
            category = 'strategic'

        date_val, _ = _parse_date_flexible(frag.get('date_of_information'))

        source_url = frag.get('source_url')
        frag_doc = _get_or_create_url_doc(source_url, sonar_source) if source_url else fallback_doc

        try:
            KnowledgeFragment.objects.create(
                entity_id=entity_id,
                category=category,
                text=text,
                date_of_information=date_val,
                confidence=65,
                source=frag_doc,
            )
            stored += 1
        except Exception as e:
            logger.warning('_store_fragments: %s — %s', mention, e)

    return stored


@shared_task(bind=True, queue='extract', max_retries=2, default_retry_delay=30)
def research_topic(self, topic: str, topic_type: str = 'company', cascade_depth: int = 0):
    """
    Use Perplexity Sonar to research a topic and write structured knowledge to the DB.
    topic_type: 'company' | 'question' | 'news'
    """
    vocab = _attr_vocab()
    if not vocab:
        logger.error('research_topic: AttributeDef is empty — migration 0008 may not have run')
        return

    def _seen_block(urls: str) -> str:
        return f'\n\nAlready ingested sources — do NOT use these, find alternative URLs:\n{urls}' if urls else ''

    if topic_type == 'company':
        seen = _seen_urls_for_company(topic)
        user_msg = (
            f'Research the space-industry company or organisation "{topic}". '
            f'Find current facts, events, and intelligence from recent web sources. '
            f'Extract as much as possible: funding history, launches, contracts, challenges, '
            f'technology choices, competitive position, key people, strategic pivots.\n\n'
            f'Allowed attribute keys:\n{vocab}'
            f'{_seen_block(seen)}'
        )
    elif topic_type == 'news':
        seen = _seen_urls_recent(days=7, limit=50)
        user_msg = (
            f'What are the most significant space-industry developments from the past 7 days? '
            f'For each event identify the organisations involved and extract structured facts, '
            f'events, and intelligence fragments.\n\n'
            f'Allowed attribute keys:\n{vocab}'
            f'{_seen_block(seen)}'
        )
    else:  # question
        seen = _seen_urls_recent(days=14, limit=50)
        user_msg = (
            f'Research the following question about the space industry: "{topic}"\n'
            f'Find and extract all relevant factual claims, events, and intelligence from recent web sources.\n\n'
            f'Allowed attribute keys:\n{vocab}'
            f'{_seen_block(seen)}'
        )

    model = settings.AI_MODEL_SONAR
    run = ExtractionRun.objects.create(
        task=f'research_{topic_type}',
        prompt_sha256=hashlib.sha256(user_msg.encode()).hexdigest(),
        model=model,
        code_version='sonar-v2',
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
    except Exception as exc:
        logger.error('research_topic "%s" error: %s', topic, exc)
        run.status = 'failed'
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'finished_at'])
        raise self.retry(exc=exc)

    claims    = data.get('claims', [])
    events    = data.get('events', [])
    fragments = data.get('fragments', [])

    # Synthetic Document for the Sonar response
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

    # ── Store claims (assertions) ─────────────────────────────────────────
    valid_attrs = {a.key: a for a in AttributeDef.objects.all()}
    accepted = rejected = 0
    new_ids: list[int] = []
    rejection_log: list[dict] = []

    for claim in claims:
        attr_key = claim.get('attribute_key')
        if attr_key not in valid_attrs:
            rejection_log.append({
                'reason': 'unknown_attribute',
                'attribute_key': attr_key,
                'subject': claim.get('subject_mention', '?'),
            })
            rejected += 1
            continue

        attr = valid_attrs[attr_key]
        mention = claim.get('subject_mention') or topic
        entity_type_hint = claim.get('subject_type', 'organization')
        quote = claim.get('quote') or mention
        value = claim.get('value')
        unit = claim.get('unit')
        extractor_conf = claim.get('extractor_confidence', 'medium')
        source_url = claim.get('source_url') or None

        src_trust = domain_trust(source_url)
        claim_doc = _get_or_create_url_doc(source_url, source) if source_url else doc

        confidence = compute_score(
            extractor_confidence=extractor_conf,
            source_base_trust=src_trust,
            source_kind='trade_press' if src_trust >= 70 else 'aggregator',
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
                entity_id = resolve_mention(
                    mention,
                    document_id=str(doc.id),
                    entity_type=entity_type_hint,
                )
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
            rejection_log.append({
                'reason': 'db_error',
                'attribute_key': attr_key,
                'subject': mention,
                'detail': str(e)[:120],
            })
            rejected += 1

    # ── Collect entity map for events + fragments ─────────────────────────
    from core.models import Entity as _Entity
    entity_ids = list(
        Assertion.objects.filter(pk__in=new_ids)
        .values_list('entity_id', flat=True).distinct()
    )
    entity_name_map = {
        str(e.id): e.canonical_name
        for e in _Entity.objects.filter(id__in=entity_ids).only('id', 'canonical_name')
    }
    name_to_id = {v: k for k, v in entity_name_map.items()}

    # ── Store events ──────────────────────────────────────────────────────
    events_stored = _store_events(events, name_to_id, doc, source)

    # ── Store fragments ───────────────────────────────────────────────────
    fragments_stored = _store_fragments(fragments, name_to_id, doc, source)

    # ── Finalise run ──────────────────────────────────────────────────────
    run.status = 'completed'
    run.finished_at = timezone.now()
    run.stats = {
        'accepted':   accepted,
        'rejected':   rejected,
        'events':     events_stored,
        'fragments':  fragments_stored,
        'topic':      topic,
        'rejections': rejection_log,
    }
    run.save(update_fields=['status', 'finished_at', 'stats'])
    logger.info(
        'research_topic "%s": %d claims, %d events, %d fragments',
        topic, accepted, events_stored, fragments_stored,
    )

    # ── Downstream tasks ─────────────────────────────────────────────────
    if new_ids:
        from ingest.tasks.adjudicate import adjudicate_assertions
        from ingest.tasks.project import refresh_entity_current
        adjudicate_assertions.delay(new_ids)
        refresh_entity_current.apply_async(countdown=5)

    # Collect all touched entities (from claims + events + fragments)
    all_entity_ids = set(entity_ids)
    for ev in Event.objects.filter(source=doc).values_list('entity_id', flat=True):
        all_entity_ids.add(str(ev))
    for frag in KnowledgeFragment.objects.filter(source=doc).values_list('entity_id', flat=True):
        all_entity_ids.add(str(frag))
    entity_id_strs = list(all_entity_ids)

    if entity_id_strs:
        from ingest.tasks.classify import classify_entity
        from ingest.tasks.relate import extract_relations_sonar
        from ingest.tasks.evolve import evolve_taxonomy
        from ingest.tasks.summarise import synthesise_entity_summary

        for eid in entity_id_strs:
            classify_entity.apply_async(args=[eid, str(run.pk)], countdown=10)
            synthesise_entity_summary.apply_async(args=[eid], countdown=30)

        extract_relations_sonar.apply_async(
            args=[str(doc.id), list(entity_name_map.values())],
            countdown=20,
        )
        evolve_taxonomy.apply_async(args=[entity_id_strs], countdown=60)

        # Cascade: research newly discovered stubs
        if cascade_depth < 2:
            stubs_to_research = [
                name for name in entity_name_map.values()
                if _Entity.objects.filter(canonical_name=name, status='stub').exists()
                and not Assertion.objects.filter(entity__canonical_name=name, status='accepted').exists()
                and not ExtractionRun.objects.filter(
                    stats__topic=name,
                    started_at__gte=timezone.now() - timedelta(days=7),
                ).exists()
            ]
            for stub_name in stubs_to_research[:4]:
                stub_entity = _Entity.objects.filter(canonical_name=stub_name, status='stub').first()
                t_type = _TYPE_TO_TOPIC.get(stub_entity.entity_type, 'company') if stub_entity else 'company'
                research_topic.apply_async(
                    args=[stub_name, t_type],
                    kwargs={'cascade_depth': cascade_depth + 1},
                    countdown=90 + cascade_depth * 60,
                )
                logger.info('research_topic: cascade depth=%d queued for "%s"', cascade_depth + 1, stub_name)

    return {'accepted': accepted, 'rejected': rejected, 'events': events_stored, 'fragments': fragments_stored}
