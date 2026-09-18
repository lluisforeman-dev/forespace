"""Sonar-powered research task.

Calls Perplexity Sonar (via OpenRouter) which searches the web in real time,
then writes four types of structured knowledge directly to the DB:
  1. Assertions  — key-value facts with confidence
  2. Events      — discrete moments in history (funding, launches, pivots, failures)
  3. Fragments   — rich narrative paragraphs (tech, strategy, challenges, competition)
  4. Relations   — typed graph edges between entities (supplies, invested_in, etc.)
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

from core.models import Assertion, AttributeDef, Document, Event, ExtractionRun, KnowledgeFragment, PredicateDef, Relation, Source
from ingest.ai import get_client
from ingest.confidence import domain_trust, score as compute_score
from ingest.cost import log_call
from ingest.tasks.resolve import resolve_mention, _VALID_ENTITY_TYPES

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
  subject_type     - organization | asset | person | facility | program
  event_type       - funding_round | launch | contract_award | partnership |
                     acquisition | failure | pivot | regulatory | milestone | leadership
  title            - short descriptive title (e.g. "Series B — £40M led by Airbus Ventures")
  date             - ISO date YYYY-MM-DD, YYYY-MM, or YYYY — best precision available
  description      - 2-3 sentences: what happened and why it matters
  amount_usd       - numeric amount in USD if applicable, else null
  significance     - "high" | "medium" | "low"
  participants     - list of {"name": "...", "type": "organization|person|asset|program"} objects
  source_url       - URL, or null
  confidence       - "high" | "medium" | "low"

━━ SECTION 3: fragments ━━
Rich narrative paragraphs about entities. For EACH meaningful piece of intelligence:
  subject_mention       - exact entity name
  subject_type          - organization | asset | person | facility | program
  category              - technical | financial | competitive | regulatory |
                          strategic | operational | people | challenge
  text                  - verbatim or close paraphrase of a full paragraph of intelligence
  date_of_information   - approximate date the info was current, YYYY-MM or YYYY, or null
  source_url            - URL, or null

━━ SECTION 4: relations ━━
Explicit relationships between named entities. This is the MOST IMPORTANT section —
it builds the knowledge graph connecting organisations, assets, and people.
For EACH relationship:
  subject_mention  - entity name (who initiates / performs the relationship)
  subject_type     - organization | asset | person | facility | program
  predicate        - one of the ALLOWED PREDICATE KEYS listed below (no others)
  object_mention   - entity name (who receives the relationship)
  object_type      - organization | asset | person | facility | program
  qualifiers       - extra attributes as JSON object, e.g. {"amount_usd": 5000000, "date": "2024-03"}
                     or {} if none. Common qualifier keys: amount_usd, date, stake_pct, round_series,
                     vehicle, orbit, payload_kg, contract_value_usd, role, scope, product, service_type,
                     technology, programme, since, location.
  description      - 1 sentence explaining the relationship and its context
  source_url       - URL of source, or null
  confidence       - "high" | "medium" | "low"

EXTRACTION RULES FOR RELATIONS:
- Be thorough — every event involving two entities likely implies a relation.
  A funding round → invested_in. A launch → launches_for or launched_payload.
  A contract → contracted_by or supplies. A joint programme → co_develops or partnered_with.
- Extract relations for ALL entities mentioned, not just the primary research topic.
- Prefer specific predicates over generic ones (contracted_by over partnered_with when it was a contract).
- Only use predicate keys from the allowed list below.

RULES:
- SPACE FOCUS: Only extract information that has a direct connection to space.
  For companies whose primary business is not space, ignore their non-space activities
  entirely — only extract facts, events, and fragments about their space operations,
  satellite services, space investments, launch customers, ground infrastructure,
  spectrum holdings, or space partnerships.
  Example: extract Telefonica's satellite backhaul contracts and LEO investments,
  but ignore their 5G rollout, subscriber counts, or rivalry with Vodafone.
- In claims, only use attribute_key values from the allowed list below.
- Extract as many events, fragments, and relations as you find — do not summarise.
- Fragments must be substantive (> 2 sentences). Capture challenges, pivots, tech choices,
  competitive dynamics, strategic rationale, key people decisions.
- Return valid JSON only, no markdown fences:
  {"claims": [...], "events": [...], "fragments": [...], "relations": [...]}"""


def _predicate_vocab() -> str:
    rows = PredicateDef.objects.values('key', 'label', 'description')
    if not rows:
        return ''
    return '\n'.join(f"- {r['key']}: {r['label']}. {r['description']}" for r in rows)


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


def _entity_context_block(topic: str) -> str:
    """
    Build a compact summary of what we already know about an entity.
    Injected into the Sonar prompt so it searches for gaps, not duplicates.
    Returns empty string if entity not found or has no data.
    """
    from django.db.models import Q
    from core.models import Entity, Relation
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

    lines = [f'\n\nWhat we already know about "{entity.canonical_name}" (do not re-extract these):']

    # ── Known facts ───────────────────────────────────────────────────────
    accepted = (
        Assertion.objects
        .filter(entity=entity, status='accepted')
        .select_related('attribute')
        .order_by('-confidence')[:20]
    )
    if accepted:
        lines.append('Known facts:')
        for a in accepted:
            val = a.value_text or a.value_num or a.value_date or a.value_bool
            unit = f' {a.unit}' if a.unit else ''
            lines.append(f'  - {a.attribute.label}: {val}{unit}')

    # ── Recent events ─────────────────────────────────────────────────────
    recent_events = (
        Event.objects
        .filter(entity=entity)
        .order_by('-date', '-id')[:8]
    )
    if recent_events:
        lines.append('Recent events already captured:')
        for ev in recent_events:
            date_str = ev.date.strftime('%Y-%m') if ev.date else '?'
            lines.append(f'  - {date_str}: {ev.title}')

    # ── Known connections ─────────────────────────────────────────────────
    rel_out = (
        Relation.objects
        .filter(subject=entity, superseded_at__isnull=True)
        .select_related('object')[:10]
    )
    rel_in = (
        Relation.objects
        .filter(object=entity, superseded_at__isnull=True)
        .select_related('subject')[:10]
    )
    connections = (
        [f'  - {r.predicate_id} → {r.object.canonical_name}' for r in rel_out] +
        [f'  - {r.predicate_id} ← {r.subject.canonical_name}' for r in rel_in]
    )
    if connections:
        lines.append('Known relationships:')
        lines.extend(connections[:15])

    # ── Gaps: attributes with no accepted assertion ───────────────────────
    covered_keys = {a.attribute_id for a in accepted}
    all_keys = set(AttributeDef.objects.values_list('key', flat=True))
    gap_keys = all_keys - covered_keys
    gap_labels = list(
        AttributeDef.objects
        .filter(key__in=gap_keys)
        .values_list('label', flat=True)
        .order_by('label')[:15]
    )
    if gap_labels:
        lines.append(f'Missing data — prioritise finding: {", ".join(gap_labels)}')

    if len(lines) == 1:
        return ''  # only the header line, nothing useful
    return '\n'.join(lines)


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


# Keywords that strongly suggest space or space-adjacent activity
_SPACE_KEYWORDS = {
    'space', 'spacecraft', 'satellite', 'rocket', 'launch', 'orbit', 'orbital',
    'propulsion', 'propellant', 'thruster', 'payload', 'fairing', 'booster',
    'reusable', 'smallsat', 'cubesat', 'nanosat', 'microsat', 'constellation',
    'spaceport', 'launchpad', 'launch site', 'launch vehicle', 'upper stage',
    'earth observation', 'remote sensing', 'sar', 'optical imaging',
    'ground station', 'telemetry', 'mission control', 'flight software',
    'astronaut', 'cosmonaut', 'crewed', 'human spaceflight',
    'nasa', 'esa', 'roscosmos', 'jaxa', 'isro', 'csa', 'cnsa', 'uksa',
    'spacex', 'rocketlab', 'rocket lab', 'arianespace', 'ula ', 'virgin',
    'in-orbit', 'in orbit', 'debris', 'space debris', 'sts', 'iss ',
    'geostationary', 'geo ', 'leo ', 'meo ', 'sso ', 'vleo',
    'interplanetary', 'lunar', 'mars', 'moon ', 'asteroid',
    'space tourism', 'space station', 'in-space', 'space infrastructure',
    'new space', 'newspace', 'space industry', 'space economy',
}

# Keywords that strongly suggest the entity is NOT primarily space-focused
_NON_SPACE_KEYWORDS = {
    'telecom carrier', 'mobile network', 'internet service provider',
    'retail bank', 'investment bank', 'insurance company',
    'supermarket', 'retail chain', 'fast food', 'pharmaceutical',
    'automotive oem', 'car manufacturer', 'oil and gas', 'oil company',
    'mining company', 'steel manufacturer',
}


def _is_space_relevant(name: str, entity_type: str = 'organization') -> bool:
    """
    Fast heuristic: is this entity space or space-adjacent?
    Assets, facilities, events, programs are assumed relevant.
    Organizations and persons are checked against keyword lists.
    Returns True if relevant (or uncertain — we prefer false negatives over false positives).
    """
    if entity_type in ('asset', 'facility', 'event', 'program'):
        return True
    name_lower = name.lower()
    if any(kw in name_lower for kw in _SPACE_KEYWORDS):
        return True
    if any(kw in name_lower for kw in _NON_SPACE_KEYWORDS):
        return False
    # Uncertain — allow it, the prompt-level filter should have already excluded junk
    return True


_CONF_MAP = {'high': 78, 'medium': 62, 'low': 45}

_TYPE_TO_TOPIC = {
    'organization': 'company',
    'asset': 'company',
    'program': 'question',
    'facility': 'question',
    'person': 'question',
    'event': 'question',
}

# topic_types that are valid for ExtractionRun.task naming
_VALID_TOPIC_TYPES = {'company', 'news', 'question', 'space_angle'}


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

        subject_type = ev.get('subject_type', 'organization')
        if subject_type not in _VALID_ENTITY_TYPES:
            subject_type = 'organization'

        entity_id = name_to_id.get(mention)
        if not entity_id:
            try:
                entity_id = resolve_mention(mention, document_id=str(fallback_doc.id), entity_type=subject_type)
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
            for p in (ev.get('participants') or [])[:8]:
                # Support both old ["name"] and new [{"name": ..., "type": ...}] formats
                if isinstance(p, dict):
                    pname = str(p.get('name') or '').strip()
                    ptype = p.get('type', 'organization')
                    if ptype not in _VALID_ENTITY_TYPES:
                        ptype = 'organization'
                else:
                    pname = str(p).strip()
                    ptype = 'organization'
                if not pname:
                    continue
                pid = name_to_id.get(pname)
                if not pid:
                    try:
                        pid = resolve_mention(pname, document_id=str(fallback_doc.id), entity_type=ptype)
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

        subject_type = frag.get('subject_type', 'organization')
        if subject_type not in _VALID_ENTITY_TYPES:
            subject_type = 'organization'

        entity_id = name_to_id.get(mention)
        if not entity_id:
            try:
                entity_id = resolve_mention(mention, document_id=str(fallback_doc.id), entity_type=subject_type)
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


def _store_relations(relations: list, fallback_doc: Document, sonar_source: Source) -> int:
    """Persist inline-extracted relations from Sonar output."""
    valid_predicates = {p.key for p in PredicateDef.objects.all()}
    if not valid_predicates:
        logger.warning('_store_relations: no predicates — run seed_predicates first')
        return 0

    conf_map = {'high': 78, 'medium': 62, 'low': 45}
    stored = 0

    for rel in relations:
        predicate = (rel.get('predicate') or '').strip()
        subject_mention = (rel.get('subject_mention') or '').strip()
        object_mention = (rel.get('object_mention') or '').strip()
        if not predicate or not subject_mention or not object_mention:
            continue
        if subject_mention == object_mention:
            continue
        if predicate not in valid_predicates:
            logger.debug('_store_relations: unknown predicate "%s"', predicate)
            continue

        subject_type = rel.get('subject_type', 'organization')
        if subject_type not in _VALID_ENTITY_TYPES:
            subject_type = 'organization'
        object_type = rel.get('object_type', 'organization')
        if object_type not in _VALID_ENTITY_TYPES:
            object_type = 'organization'

        confidence = conf_map.get(rel.get('confidence', 'medium'), 62)
        qualifiers = rel.get('qualifiers') or {}
        description = (rel.get('description') or '').strip()[:500]

        source_url = rel.get('source_url')
        rel_doc = _get_or_create_url_doc(source_url, sonar_source) if source_url else fallback_doc

        try:
            with transaction.atomic():
                subject_id = resolve_mention(
                    subject_mention, document_id=str(fallback_doc.id), entity_type=subject_type
                )
                object_id = resolve_mention(
                    object_mention, document_id=str(fallback_doc.id), entity_type=object_type
                )

                existing = Relation.objects.filter(
                    subject_id=subject_id,
                    predicate_id=predicate,
                    object_id=object_id,
                    superseded_at__isnull=True,
                ).first()

                if existing and existing.document_id != rel_doc.id:
                    # Corroboration — compound confidence
                    gap = 100 - existing.confidence
                    bumped = min(99, existing.confidence + max(3, int(gap * confidence / 300)))
                    existing.confidence = bumped
                    existing.status = 'accepted' if bumped >= 65 else existing.status
                    existing.save(update_fields=['confidence', 'status'])
                else:
                    Relation.objects.update_or_create(
                        subject_id=subject_id,
                        predicate_id=predicate,
                        object_id=object_id,
                        document=rel_doc,
                        defaults={
                            'qualifiers': qualifiers,
                            'quote': description,
                            'confidence': confidence,
                            'method': 'extracted',
                            'status': 'accepted' if confidence >= 65 else 'candidate',
                        },
                    )
            stored += 1
        except Exception as e:
            logger.debug('_store_relations: %s →%s→ %s: %s', subject_mention, predicate, object_mention, e)

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

    pred_vocab = _predicate_vocab()

    def _seen_block(urls: str) -> str:
        return f'\n\nAlready ingested sources — do NOT use these, find alternative URLs:\n{urls}' if urls else ''

    def _vocab_block() -> str:
        base = f'Allowed attribute keys:\n{vocab}'
        if pred_vocab:
            base += f'\n\nAllowed predicate keys:\n{pred_vocab}'
        return base

    if topic_type == 'company':
        seen = _seen_urls_for_company(topic)
        ctx = _entity_context_block(topic)
        user_msg = (
            f'Research the space-industry company or organisation "{topic}". '
            f'Find current facts, events, intelligence, and relationships from recent web sources. '
            f'Extract as much as possible: funding history, launches, contracts, challenges, '
            f'technology choices, competitive position, key people, strategic pivots, '
            f'and all relationships with other companies, agencies, and assets.\n\n'
            f'{_vocab_block()}'
            f'{ctx}'
            f'{_seen_block(seen)}'
        )
    elif topic_type == 'news':
        seen = _seen_urls_recent(days=7, limit=50)
        user_msg = (
            f'What are the most significant space-industry developments from the past 7 days? '
            f'For each event identify the organisations involved and extract structured facts, '
            f'events, intelligence fragments, and relationships between entities.\n\n'
            f'{_vocab_block()}'
            f'{_seen_block(seen)}'
        )
    elif topic_type == 'space_angle':
        seen = _seen_urls_for_company(topic)
        ctx = _entity_context_block(topic)
        user_msg = (
            f'Research "{topic}" specifically for its involvement in the space industry. '
            f'What satellite services does it operate or use? What space investments, '
            f'partnerships, or contracts does it have? What launch customers, ground '
            f'infrastructure, or spectrum assets are relevant? '
            f'Extract all relationships to space companies, agencies, and assets. '
            f'Ignore all non-space activities entirely.\n\n'
            f'{_vocab_block()}'
            f'{ctx}'
            f'{_seen_block(seen)}'
        )
    else:  # question
        seen = _seen_urls_recent(days=14, limit=50)
        user_msg = (
            f'Research the following question about the space industry: "{topic}"\n'
            f'Find and extract all relevant factual claims, events, intelligence, '
            f'and relationships between entities from recent web sources.\n\n'
            f'{_vocab_block()}'
            f'{_seen_block(seen)}'
        )

    model = settings.AI_MODEL_SONAR
    run = ExtractionRun.objects.create(
        task=f'research_{topic_type}',
        prompt_sha256=hashlib.sha256(user_msg.encode()).hexdigest(),
        model=model,
        code_version='sonar-v3',
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
    relations = data.get('relations', [])

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

    # ── Store relations (inline — Sonar had full web context) ─────────────
    relations_stored = _store_relations(relations, doc, source)

    # ── Finalise run ──────────────────────────────────────────────────────
    run.status = 'completed'
    run.finished_at = timezone.now()
    run.stats = {
        'accepted':   accepted,
        'rejected':   rejected,
        'events':     events_stored,
        'fragments':  fragments_stored,
        'relations':  relations_stored,
        'topic':      topic,
        'rejections': rejection_log,
    }
    run.save(update_fields=['status', 'finished_at', 'stats'])
    logger.info(
        'research_topic "%s": %d claims, %d events, %d fragments, %d relations',
        topic, accepted, events_stored, fragments_stored, relations_stored,
    )

    # ── Downstream tasks ─────────────────────────────────────────────────
    if new_ids:
        from ingest.tasks.adjudicate import adjudicate_assertions
        from ingest.tasks.project import refresh_entity_current
        adjudicate_assertions.delay(new_ids)
        refresh_entity_current.apply_async(countdown=5)

    # Collect ALL touched entities: claims + event subjects + event participants + fragments + relations
    all_entity_ids = set(entity_ids)
    for ev_obj in Event.objects.filter(source=doc).prefetch_related('participants'):
        all_entity_ids.add(str(ev_obj.entity_id))
        for p in ev_obj.participants.all():
            all_entity_ids.add(str(p.id))
    for frag in KnowledgeFragment.objects.filter(source=doc).values_list('entity_id', flat=True):
        all_entity_ids.add(str(frag))
    for rel in Relation.objects.filter(document=doc).values_list('subject_id', 'object_id'):
        all_entity_ids.add(str(rel[0]))
        all_entity_ids.add(str(rel[1]))
    entity_id_strs = list(all_entity_ids)

    if entity_id_strs:
        from ingest.tasks.classify import classify_entity
        from ingest.tasks.evolve import evolve_taxonomy
        from ingest.tasks.summarise import synthesise_entity_summary

        for eid in entity_id_strs:
            classify_entity.apply_async(args=[eid, str(run.pk)], countdown=10)
            synthesise_entity_summary.apply_async(args=[eid], countdown=30)

        if relations_stored:
            from ingest.tasks.project import refresh_relation_current
            refresh_relation_current.apply_async(countdown=5)

        evolve_taxonomy.apply_async(args=[entity_id_strs], countdown=60)

        # Cascade: queue every discovered entity not recently researched,
        # sorted by accepted assertion count ascending — entities with the least
        # known information go first, naturally balancing coverage across the graph.
        from django.db.models import Count
        cutoff = timezone.now() - timedelta(days=7)
        recently_researched = set(
            ExtractionRun.objects
            .filter(started_at__gte=cutoff)
            .exclude(stats__topic=None)
            .values_list('stats__topic', flat=True)
        )

        # Annotate each entity with how many accepted assertions it has
        from django.db.models import Q as _Q
        all_discovered = (
            _Entity.objects
            .filter(id__in=entity_id_strs)
            .exclude(status='merged')
            .annotate(assertion_count=Count(
                'assertions',
                filter=_Q(assertions__status='accepted'),
            ))
            .order_by('assertion_count')  # least known first
            .only('id', 'canonical_name', 'entity_type', 'status')
        )

        for i, discovered in enumerate(all_discovered):
            name = discovered.canonical_name
            if name in recently_researched:
                continue
            is_primary = _is_space_relevant(name, discovered.entity_type)
            t_type = _TYPE_TO_TOPIC.get(discovered.entity_type, 'company')
            if not is_primary and t_type == 'company':
                t_type = 'space_angle'
            # Stagger: 2 min base + 45s per slot so queue fills gradually
            countdown = 120 + i * 45
            research_topic.apply_async(
                args=[name, t_type],
                kwargs={'cascade_depth': cascade_depth + 1},
                countdown=countdown,
            )
            logger.info(
                'research_topic: cascade queued "%s" (depth=%d, assertions=%d, in %ds)',
                name, cascade_depth + 1, discovered.assertion_count, countdown,
            )

    return {'accepted': accepted, 'rejected': rejected, 'events': events_stored, 'fragments': fragments_stored, 'relations': relations_stored}
