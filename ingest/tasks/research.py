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
from ingest.prompts import get_prompt
from ingest.tasks.resolve import resolve_mention, _VALID_ENTITY_TYPES

logger = logging.getLogger(__name__)

_SONAR_SOURCE_NAME = 'Perplexity Sonar'

_SYSTEM = """\
You are a structured data extractor for a space-industry knowledge graph.
Search the web for current, verifiable information and return JSON with THREE sections.

━━ SECTION 1: claims ━━
Structured key-value facts. For EACH claim:
  subject_mention        - exact entity name
  subject_type           - company | investor | entity | university | asset | person | facility | event | program | funding_program | end_user
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
  subject_type     - company | investor | entity | university | asset | person | facility | program | funding_program | end_user
  event_type       - funding_round | grant_award | grant_call | ipo | spac |
                     debt_financing | convertible | crowdfunding |
                     launch | contract_award | partnership |
                     acquisition | failure | pivot | regulatory | milestone | leadership |
                     publication | research_grant
  title            - short descriptive title (e.g. "Series B — £40M led by Airbus Ventures")
  date             - ISO date YYYY-MM-DD, YYYY-MM, or YYYY — best precision available
  description      - 2-3 sentences: what happened and why it matters
  amount_usd       - numeric amount in USD if applicable, else null
  significance     - "high" | "medium" | "low"
  participants     - list of {"name": "...", "type": "company|investor|entity|university|person|asset|program"} objects
  source_url       - URL, or null
  confidence       - "high" | "medium" | "low"
  call_url         - for grant_call events only: direct URL to the application portal or official call page, or null
  call_status      - for grant_call events only: "open" | "upcoming" | "closed"

━━ SECTION 3: fragments ━━
Rich narrative paragraphs about entities. For EACH meaningful piece of intelligence:
  subject_mention       - exact entity name
  subject_type          - organization | asset | person | facility | program | funding_program | end_user
  category              - technical | financial | competitive | regulatory |
                          strategic | operational | people | challenge | research
  text                  - verbatim or close paraphrase of a full paragraph of intelligence
  date_of_information   - approximate date the info was current, YYYY-MM or YYYY, or null
  source_url            - URL, or null

━━ SECTION 4: relations ━━
Explicit relationships between named entities. This is the MOST IMPORTANT section —
it builds the knowledge graph connecting organisations, assets, and people.
For EACH relationship:
  subject_mention  - entity name (who initiates / performs the relationship)
  subject_type     - company | investor | entity | university | asset | person | facility | program | funding_program | end_user
  predicate        - one of the ALLOWED PREDICATE KEYS listed below (no others)
  object_mention   - entity name (who receives the relationship)
  object_type      - company | investor | entity | university | asset | person | facility | program | funding_program | end_user
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
- ★ SUPPLY CHAIN-CRITICAL: For any company, investor, or entity, ALWAYS extract if findable:
  primary_product (what they make/sell), technology_domain (their tech area), value_chain_tier
  (where in the chain: raw_material|component|subsystem|system|integrator|operator|data_service|end_user).
  These three fields power the supply chain graph — extract them for EVERY organisation.
- ★ MAP-CRITICAL: For any company, investor, or entity, ALWAYS extract if findable:
  headquarters_city, headquarters_country, employee_count, total_funding_usd, founding_year.
  These fields power geographic maps and funding charts — search every source for them.
  Do not skip these even if the document is primarily about something else.
  For EVERY office, facility, or operational presence (HQ and additional locations):
  add a has_office_in relation → the city geography entity.
  Qualifiers MUST include office_type: "hq" | "office" | "facility" | "rd_center".
  Include address (full street address) in qualifiers whenever findable.
  A company with offices in Madrid, London, and Houston gets THREE has_office_in relations.
- ★ RESEARCH-CRITICAL: For ANY entity — companies, universities, research institutes, people,
  government agencies — extract publication_count and research_focus when findable.
  For individual papers or presentations use event_type=publication with:
    title: exact paper or presentation title (not a paraphrase)
    conference/journal in the description: e.g. "IAC 2025 Milan", "ION GNSS+ 2026", "Acta Astronautica vol. 210"
    specific findings: what was demonstrated, measured, or proposed — include numbers if available
    participants: all co-authors and their organisations
    source_url: direct URL to the paper, abstract, or conference proceedings page
  For funding use event_type=research_grant with granting body, programme name, and amount.
  For research programmes use category=research in fragments with specific technical details,
  performance numbers, collaborators, and institutional partners — not generic summaries.
  Dig into: IAC, AIAA SciTech/Aviation/Propulsion, ION GNSS+, ESA symposia (EDHPC, ESTEC),
  IEEE Aerospace, SmallSat, Reinventing Space, arXiv, Acta Astronautica, JGCD, JSR.
  Companies like SpaceX, Airbus, OHB, and Thales publish research — do not skip them.
- SPACE FOCUS: Only extract information that has a direct connection to space.
  For companies whose primary business is not space, ignore their non-space activities
  entirely — only extract facts, events, and fragments about their space operations,
  satellite services, space investments, launch customers, ground infrastructure,
  spectrum holdings, or space partnerships.
  Example: extract Telefonica's satellite backhaul contracts and LEO investments,
  but ignore their 5G rollout, subscriber counts, or rivalry with Vodafone.
- subject_mention in ALL sections MUST be an organisation, company, institution, person,
  or named asset. NEVER use a country, region, city, or continent as subject_mention —
  these are values (e.g. headquarters_country = "United Kingdom"), not subjects.
  Geographic areas may appear as object_mention in relations (e.g. operates_in → "United Kingdom").
- Always use the full institutional name for government bodies and funding agencies —
  "Government of Catalonia" or "Generalitat de Catalunya" not "Catalonia",
  "European Commission" not "EU", "NASA" not "United States government".
- Distinguish SPACE COMPANIES from END USERS — default to company/entity when uncertain:
  entity_type=company : the entity builds hardware, launches rockets, operates satellites,
    processes satellite data as a product, or provides space-derived connectivity.
    Its primary mission involves space. Examples: SpaceX, Planet Labs, GMV, Open Cosmos.
  entity_type=entity  : an institution, agency, or body with a meaningful space role but not
    purely commercial — government agencies, intergovernmental bodies, research labs, NGOs
    with a space department. Examples: ESA, NASA, DLR, CNES, Eutelsat.
  entity_type=end_user: ONLY use this when it is UNAMBIGUOUS that the entity's entire
    purpose is outside the space industry and space is merely a passive data input.
    Examples: Open Arms (humanitarian NGO using satellite imagery for sea rescue with zero
    space staff), a supermarket chain using GPS for fleet logistics.
  DEFAULT RULE: if there is any doubt, label the entity company or entity — it is better
  to over-include in the space industry than to incorrectly exclude a space-adjacent actor.
- Distinguish INSTITUTION, FUNDING PROGRAMME, and SPACE PROGRAMME:
  entity_type=entity|investor: European Commission, ESA, Generalitat de Catalunya, EIB, Innovate UK, BlackRock
  entity_type=funding_program: Horizon Europe, ESA ARTES, EIC Accelerator, Préstecs ICF, BlackRock Space Fund
    — a funding_program is a deployable instrument with calls, deadlines, budgets, and eligibility criteria.
    — a company receives capital FROM a funding_program, not from the institution directly.
  entity_type=program: Artemis, ISS, SpaceX Rideshare Program, Ariane 6
    — a program is a space/commercial operational programme, NOT a funding instrument.
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


_ANGLES_SYSTEM = """\
You are a research strategist for a space-industry knowledge graph.
Given an entity name and type, generate 2 targeted search angles that would uncover
different facets of that entity's activities, relationships, and history.

Each angle should be a short search-focused description (not a question).
Think about: technology & products, key people & leadership,
contracts & customers, partnerships & competition, regulatory & licensing history.

Return JSON only: {"angles": ["...", "..."]}
Include the entity name or a clear disambiguator in each angle so Sonar doesn't confuse
it with unrelated entities (e.g. if the entity could be mistaken for something else,
add a clarifying term like "space", "aerospace", "satellite", etc.)."""


def _generate_search_angles(topic: str, entity_type: str) -> list[str]:
    """Use AI_MODEL_FAST to generate 3 diverse search angles for a company/entity."""
    try:
        resp = get_client().chat.completions.create(
            model=settings.AI_MODEL_FAST,
            messages=[
                {'role': 'system', 'content': get_prompt('research_angles', _ANGLES_SYSTEM)},
                {'role': 'user', 'content': f'Entity: {topic}\nType: {entity_type}'},
            ],
            response_format={'type': 'json_object'},
            max_tokens=200,
            temperature=0.3,
        )
        log_call('search_angles', settings.AI_MODEL_FAST, resp)
        content = resp.choices[0].message.content
        if not content:
            return []
        data = json.loads(content)
        llm_angles = [str(a).strip() for a in data.get('angles', []) if a]
    except Exception as exc:
        logger.warning('_generate_search_angles "%s": %s', topic, exc)
        llm_angles = []

    # Always lead with a guaranteed baseline angle for map-critical fields
    baseline = f'{topic} headquarters location employees headcount funding raised'
    # All entities get a dedicated publications angle — not just universities
    research_angle = f'{topic} publications papers conference presentations space research IAC AIAA ION GNSS ESA IEEE preprint 2024 2025 2026'
    return [baseline, research_angle] + llm_angles[:1]


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


def _is_space_relevant(name: str, entity_type: str = 'company') -> bool:
    """
    Fast heuristic: is this entity space or space-adjacent?
    Assets, facilities, events, programs are assumed relevant.
    Organizations and persons are checked against keyword lists.
    Returns True if relevant (or uncertain — we prefer false negatives over false positives).
    """
    if entity_type == 'geography':
        return False  # never auto-research geographic entities
    if entity_type in ('asset', 'facility', 'event', 'program', 'funding_program', 'end_user'):
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
    'company': 'company',
    'investor': 'company',
    'entity': 'company',
    'university': 'company',
    'asset': 'company',
    'funding_program': 'funding_program',
    'end_user': 'end_user',      # focused: only the space connection, leaf node
    'program': 'question',
    'facility': 'question',
    'person': 'person',
    'event': 'question',
}

# topic_types that are valid for ExtractionRun.task naming
_VALID_TOPIC_TYPES = {'company', 'news', 'question', 'space_angle', 'research', 'funding', 'funding_program', 'person', 'end_user', 'location'}


def _synthesise_event_description(existing: str, new: str, title: str) -> str:
    """
    Merge two descriptions of the same event from different sources into one
    richer, more complete description using AI_MODEL_FAST.
    Falls back to the longer description if the LLM call fails.
    """
    # Skip synthesis if texts are nearly identical (>85% word overlap)
    def _word_set(s: str) -> set:
        return set(s.lower().split())
    a, b = _word_set(existing), _word_set(new)
    if a and b:
        jaccard = len(a & b) / len(a | b)
        if jaccard > 0.85:
            return existing if len(existing) >= len(new) else new

    prompt = (
        f'Two sources describe the same event: "{title}"\n\n'
        f'Source A: {existing}\n\n'
        f'Source B: {new}\n\n'
        f'Synthesise these into one comprehensive description (2-4 sentences) that captures '
        f'all unique facts and details from both sources. Be concise and factual. '
        f'Return only the description text, no preamble.'
    )
    try:
        resp = get_client().chat.completions.create(
            model=settings.AI_MODEL_FAST,
            messages=[{'role': 'user', 'content': prompt}],
            max_tokens=300,
            temperature=0,
        )
        log_call('event_synthesis', settings.AI_MODEL_FAST, resp)
        result = (resp.choices[0].message.content or '').strip()
        if result:
            return result
    except Exception as exc:
        logger.warning('_synthesise_event_description: %s', exc)
    return existing if len(existing) >= len(new) else new


def _is_same_story(desc_a: str, desc_b: str, title_a: str, title_b: str) -> bool:
    """
    Ask AI_MODEL_FAST whether two event descriptions are the same real-world
    announcement covered by different sources. Returns True if yes.
    Fast shortcut: if word-overlap on titles+descriptions is already >70%, assume yes.
    """
    def _words(s: str) -> set:
        return set(s.lower().split())
    combined_a = _words(title_a + ' ' + desc_a)
    combined_b = _words(title_b + ' ' + desc_b)
    if combined_a and combined_b:
        jaccard = len(combined_a & combined_b) / len(combined_a | combined_b)
        if jaccard > 0.70:
            return True
        if jaccard < 0.15:
            return False  # clearly different events, skip LLM call

    prompt = (
        f'Are these two event descriptions about the same real-world announcement '
        f'covered by different news sources? Answer only "yes" or "no".\n\n'
        f'Event A title: {title_a}\nEvent A: {desc_a[:300]}\n\n'
        f'Event B title: {title_b}\nEvent B: {desc_b[:300]}'
    )
    try:
        resp = get_client().chat.completions.create(
            model=settings.AI_MODEL_FAST,
            messages=[{'role': 'user', 'content': prompt}],
            max_tokens=5,
            temperature=0,
        )
        log_call('event_same_story', settings.AI_MODEL_FAST, resp)
        answer = (resp.choices[0].message.content or '').strip().lower()
        return answer.startswith('yes')
    except Exception as exc:
        logger.warning('_is_same_story: %s', exc)
        return False


def _store_events(events: list, name_to_id: dict, fallback_doc: Document, sonar_source: Source, subject_context: str = '', primary_entity_id: str | None = None, primary_entity_norm: str | None = None) -> tuple[int, set]:
    """Persist extracted events, resolving participant entity names.
    Returns (stored_count, entity_ids) where entity_ids includes all subjects and participants."""
    valid_types = {t[0] for t in Event.EVENT_TYPES}
    valid_sig = {s[0] for s in Event.SIGNIFICANCE}
    stored = 0
    entity_ids: set = set()

    for ev in events:
        mention = (ev.get('subject_mention') or '').strip()
        description = (ev.get('description') or '').strip()
        title = (ev.get('title') or '').strip()
        if not mention or not description or not title:
            continue

        subject_type = ev.get('subject_type', 'company')
        if subject_type not in _VALID_ENTITY_TYPES:
            subject_type = 'company'

        entity_id = name_to_id.get(mention)
        if not entity_id:
            try:
                entity_id = resolve_mention(mention, document_id=str(fallback_doc.id), entity_type=subject_type, subject_context=subject_context, primary_entity_id=primary_entity_id, primary_entity_norm=primary_entity_norm)
            except Exception:
                continue
        entity_ids.add(str(entity_id))

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

        call_url = ev.get('call_url') or None
        call_status_raw = ev.get('call_status') or None
        valid_call_statuses = {s[0] for s in Event.CALL_STATUS}
        call_status = call_status_raw if call_status_raw in valid_call_statuses else None

        try:
            # ── Fast path: exact title + date match → same story ─────────────
            event_obj, created = Event.objects.get_or_create(
                entity_id=entity_id,
                event_type=event_type,
                title=title[:500],
                date=date_val,
                defaults={
                    'date_precision': date_precision,
                    'description': description,
                    'amount_usd': amount_usd,
                    'significance': significance,
                    'confidence': confidence,
                    'source': ev_doc,
                    'call_url': call_url,
                    'call_status': call_status,
                },
            )

            if created and date_val:
                # ── Fuzzy path: same day + same type but different headline ──
                # Same-day announcements often come in different narrative styles.
                # Check if any existing event on this date is actually the same story.
                same_day_candidates = Event.objects.filter(
                    entity_id=entity_id,
                    event_type=event_type,
                    date=date_val,
                ).exclude(pk=event_obj.pk)
                for candidate in same_day_candidates:
                    # Exact amount match is near-certain proof of same event
                    amount_match = (
                        amount_usd is not None
                        and candidate.amount_usd is not None
                        and abs(candidate.amount_usd - amount_usd) < 1
                    )
                    if amount_match or _is_same_story(candidate.description, description, candidate.title, title):
                        # It's the same announcement — delete the row we just created
                        # and merge into the existing one instead.
                        event_obj.delete()
                        event_obj = candidate
                        created = False
                        break

            if not created:
                # Compound sources: synthesise both descriptions into a richer account.
                update_fields = []
                if event_obj.description != description:
                    merged = _synthesise_event_description(event_obj.description, description, event_obj.title)
                    if merged != event_obj.description:
                        event_obj.description = merged
                        update_fields.append('description')
                if confidence > event_obj.confidence:
                    event_obj.confidence = confidence
                    update_fields.append('confidence')
                # Always refresh call_url and call_status — new research may have better data
                if call_url and call_url != event_obj.call_url:
                    event_obj.call_url = call_url
                    update_fields.append('call_url')
                if call_status and call_status != event_obj.call_status:
                    event_obj.call_status = call_status
                    update_fields.append('call_status')
                if update_fields:
                    event_obj.save(update_fields=update_fields)
            for p in (ev.get('participants') or [])[:8]:
                # Support both old ["name"] and new [{"name": ..., "type": ...}] formats
                if isinstance(p, dict):
                    pname = str(p.get('name') or '').strip()
                    ptype = p.get('type', 'company')
                    if ptype not in _VALID_ENTITY_TYPES:
                        ptype = 'company'
                else:
                    pname = str(p).strip()
                    ptype = 'company'
                if not pname:
                    continue
                pid = name_to_id.get(pname)
                if not pid:
                    try:
                        pid = resolve_mention(pname, document_id=str(fallback_doc.id), entity_type=ptype, subject_context=subject_context, primary_entity_id=primary_entity_id, primary_entity_norm=primary_entity_norm)
                    except Exception:
                        continue
                if pid != entity_id:
                    event_obj.participants.add(pid)
                entity_ids.add(str(pid))
            stored += 1
        except Exception as e:
            logger.warning('_store_events: %s — %s', mention, e)

    return stored, entity_ids


def _store_fragments(fragments: list, name_to_id: dict, fallback_doc: Document, sonar_source: Source, subject_context: str = '', primary_entity_id: str | None = None, primary_entity_norm: str | None = None) -> tuple[int, set]:
    """Persist knowledge fragments. Returns (stored_count, entity_ids)."""
    valid_cats = {c[0] for c in KnowledgeFragment.CATEGORIES}
    stored = 0
    entity_ids: set = set()

    for frag in fragments:
        mention = (frag.get('subject_mention') or '').strip()
        text = (frag.get('text') or '').strip()
        if not mention or len(text) < 40:
            continue

        subject_type = frag.get('subject_type', 'company')
        if subject_type not in _VALID_ENTITY_TYPES:
            subject_type = 'company'

        entity_id = name_to_id.get(mention)
        if not entity_id:
            try:
                entity_id = resolve_mention(mention, document_id=str(fallback_doc.id), entity_type=subject_type, subject_context=subject_context, primary_entity_id=primary_entity_id, primary_entity_norm=primary_entity_norm)
            except Exception:
                continue
        entity_ids.add(str(entity_id))

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

    return stored, entity_ids


def _store_relations(relations: list, fallback_doc: Document, sonar_source: Source, subject_context: str = '', primary_entity_id: str | None = None, primary_entity_norm: str | None = None) -> tuple[int, set]:
    """Persist inline-extracted relations from Sonar output. Returns (stored_count, entity_ids)."""
    valid_predicates = {p.key for p in PredicateDef.objects.all()}
    if not valid_predicates:
        logger.warning('_store_relations: no predicates — run seed_predicates first')
        return 0, set()

    conf_map = {'high': 78, 'medium': 62, 'low': 45}
    stored = 0
    entity_ids: set = set()

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

        subject_type = rel.get('subject_type', 'company')
        if subject_type not in _VALID_ENTITY_TYPES:
            subject_type = 'company'
        object_type = rel.get('object_type', 'company')
        if object_type not in _VALID_ENTITY_TYPES:
            object_type = 'company'

        confidence = conf_map.get(rel.get('confidence', 'medium'), 62)
        qualifiers = rel.get('qualifiers') or {}
        description = (rel.get('description') or '').strip()[:500]

        source_url = rel.get('source_url')
        rel_doc = _get_or_create_url_doc(source_url, sonar_source) if source_url else fallback_doc

        try:
            with transaction.atomic():
                subject_id = resolve_mention(
                    subject_mention, document_id=str(fallback_doc.id), entity_type=subject_type, subject_context=subject_context, primary_entity_id=primary_entity_id, primary_entity_norm=primary_entity_norm
                )
                # has_office_in objects are always geographic — never create company stubs for cities
                _obj_type = 'geography' if predicate == 'has_office_in' else object_type
                object_id = resolve_mention(
                    object_mention, document_id=str(fallback_doc.id), entity_type=_obj_type, subject_context=subject_context, primary_entity_id=primary_entity_id, primary_entity_norm=primary_entity_norm
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
            entity_ids.add(str(subject_id))
            entity_ids.add(str(object_id))
            stored += 1
        except Exception as e:
            logger.debug('_store_relations: %s →%s→ %s: %s', subject_mention, predicate, object_mention, e)

    return stored, entity_ids


def _parse_address_structured(address: str) -> dict | None:
    """
    Parse a free-form address into Nominatim structured fields.

    Strategy: postal code is the universal pivot point in addresses worldwide.
    Split on commas, find the chunk containing a 4-6 digit postal code, take
    the first 1-2 chunks as street+number (dropping anything in between like
    floor, unit, apartment — regardless of language), city from the postal
    code chunk, country from the last chunk.

    Returns dict with keys: street, postalcode, city, country (any may be None).
    """
    parts = [p.strip() for p in address.split(',') if p.strip()]
    if not parts:
        return None

    postal_idx = None
    postal_code = None
    city_in_chunk = None

    for i, part in enumerate(parts):
        m = re.search(r'\b(\d{4,6})\b', part)
        if m:
            postal_idx = i
            postal_code = m.group(1)
            # city is the rest of this chunk after the postal code
            city_in_chunk = part[m.end():].strip() or None
            break

    country = parts[-1] if len(parts) > 1 else None

    if postal_idx is not None and postal_idx >= 1:
        # Street = first chunk; number = second chunk if it's short (≤6 chars, likely "95" or "20A")
        street_parts = [parts[0]]
        if postal_idx >= 2 and len(parts[1]) <= 6:
            street_parts.append(parts[1])
        street = ' '.join(street_parts)
    elif postal_idx == 0:
        street = None
    else:
        # No postal code found — can't parse structurally
        return None

    return {
        'street': street,
        'postalcode': postal_code,
        'city': city_in_chunk,
        'country': country,
    }


def _llm_parse_address(address: str) -> dict | None:
    """
    Use AI_MODEL_FAST to parse any address string into structured fields.
    Handles floor/unit qualifiers, non-English formats, and unusual layouts.
    Returns dict with keys: street, postalcode, city, country (any may be None).
    """
    import json as _json
    try:
        resp = get_client().chat.completions.create(
            model=settings.AI_MODEL_FAST,
            messages=[{
                'role': 'user',
                'content': (
                    'Parse this address into JSON with exactly these keys: '
                    'street (street name and number only, no floor/unit/apartment), '
                    'postalcode, city, country. Use null for missing fields. '
                    'Return only valid JSON, no explanation.\n\n'
                    f'Address: {address}'
                ),
            }],
            response_format={'type': 'json_object'},
            max_tokens=80,
            temperature=0,
        )
        content = resp.choices[0].message.content
        if content:
            data = _json.loads(content)
            return {
                'street':     data.get('street') or None,
                'postalcode': str(data.get('postalcode') or '').strip() or None,
                'city':       data.get('city') or None,
                'country':    data.get('country') or None,
            }
    except Exception as exc:
        logger.debug('_llm_parse_address "%s": %s', address, exc)
    return None


def _geocode_with_fallback(address: str) -> tuple[float, float] | tuple[None, None]:
    """
    1. Try regex structured parse (postal code pivot, no tokens)
    2. If that fails, try LLM parse (handles any language/format, ~80 tokens)
    3. Send structured fields to Nominatim
    4. Fall back to free-form Nominatim if still no result
    """
    import time as _time
    import requests as _req

    # Step 1: regex parse
    parsed = _parse_address_structured(address)

    # Step 2: LLM parse if regex couldn't find a postal code
    if not (parsed and parsed.get('postalcode')):
        parsed = _llm_parse_address(address)

    if parsed and (parsed.get('postalcode') or parsed.get('city')):
        params = {k: v for k, v in parsed.items() if v}
        params.update({'format': 'json', 'limit': 1})
        try:
            r = _req.get(
                'https://nominatim.openstreetmap.org/search',
                params=params,
                headers={'User-Agent': 'ForeSpace/1.0 (space-industry knowledge graph)'},
                timeout=5,
            )
            results = r.json()
            if results:
                return float(results[0]['lat']), float(results[0]['lon'])
        except Exception as exc:
            logger.debug('_geocode_with_fallback structured "%s": %s', address, exc)
        _time.sleep(1.1)

    # Step 4: free-form fallback
    return _nominatim_geocode(address)


@shared_task(queue='extract')
def geocode_all_offices():
    """
    Two passes:
    1. Geocode has_office_in relations that have an address qualifier but no lat/lon.
    2. Geocode entities that have headquarters assertions but no lat/lon on the entity itself
       (covers entities researched before has_office_in predicate existed).
    Stores coords on both the relation qualifier AND directly on the entity.
    """
    import time as _time
    from django.db import close_old_connections as _close_old_connections
    from core.models import Relation as _Rel, Entity as _Entity, Assertion as _Assertion

    # Pass 1 — relations with address but no coords
    rels = list(
        _Rel.objects
        .filter(predicate_id='has_office_in', superseded_at__isnull=True)
        .values('id', 'subject_id', 'qualifiers')
    )
    done = skipped = 0
    for row in rels:
        q = row['qualifiers'] or {}
        if q.get('lat') or not q.get('address'):
            skipped += 1
            continue
        lat, lon = _geocode_with_fallback(q['address'])
        if lat is None:
            skipped += 1
            continue
        q['lat'], q['lon'] = lat, lon
        _close_old_connections()
        _Rel.objects.filter(id=row['id']).update(qualifiers=q)
        if q.get('office_type') == 'hq':
            _Entity.objects.filter(id=row['subject_id'], latitude__isnull=True).update(
                latitude=lat, longitude=lon, has_street_address=True,
            )
        done += 1
    logger.info('geocode_all_offices pass1: %d geocoded, %d skipped', done, skipped)

    # Pass 2 — entities with headquarters assertions but no coords yet
    addr_map = {
        row['entity_id']: row['value_text']
        for row in _Assertion.objects.filter(
            attribute_id='headquarters_address', superseded_at__isnull=True,
        ).exclude(value_text='').values('entity_id', 'value_text')
    }
    city_map = {
        row['entity_id']: row['value_text']
        for row in _Assertion.objects.filter(
            attribute_id='headquarters_city', superseded_at__isnull=True,
            status__in=('accepted', 'candidate'),
        ).exclude(value_text='').values('entity_id', 'value_text')
    }
    country_map = {
        row['entity_id']: row['value_text']
        for row in _Assertion.objects.filter(
            attribute_id='headquarters_country', superseded_at__isnull=True,
            status__in=('accepted', 'candidate'),
        ).exclude(value_text='').values('entity_id', 'value_text')
    }

    entities_needing_coords = list(
        _Entity.objects.filter(
            latitude__isnull=True,
            id__in=set(addr_map) | set(city_map) | set(country_map),
        ).values_list('id', flat=True)
    )

    # For city-only fallback: use existing geography entity coords — no Nominatim needed, instant
    city_names = set(city_map.values())
    geo_city_coords = {
        row['canonical_name']: (float(row['latitude']), float(row['longitude']))
        for row in _Entity.objects.filter(
            entity_type='geography', canonical_name__in=city_names, latitude__isnull=False,
        ).values('canonical_name', 'latitude', 'longitude')
    }

    done2 = skipped2 = 0
    for eid in entities_needing_coords:
        address = addr_map.get(eid)
        if address:
            lat, lon = _geocode_with_fallback(address)
            is_precise = True
            _time.sleep(1.1)
        else:
            city = city_map.get(eid)
            if city and city in geo_city_coords:
                lat, lon = geo_city_coords[city]
                is_precise = False
            else:
                # Last resort: Nominatim with city+country
                parts = [v for v in [city_map.get(eid), country_map.get(eid)] if v]
                if not parts:
                    skipped2 += 1
                    continue
                lat, lon = _nominatim_geocode(', '.join(parts))
                is_precise = False
                _time.sleep(1.1)
        if lat is None:
            skipped2 += 1
            continue
        _close_old_connections()
        _Entity.objects.filter(id=eid).update(
            latitude=lat, longitude=lon, has_street_address=is_precise,
        )
        done2 += 1
    logger.info('geocode_all_offices pass2: %d geocoded, %d skipped', done2, skipped2)


def _nominatim_geocode(address: str) -> tuple[float, float] | tuple[None, None]:
    import requests as _req
    try:
        r = _req.get(
            'https://nominatim.openstreetmap.org/search',
            params={'q': address, 'format': 'json', 'limit': 1},
            headers={'User-Agent': 'ForeSpace/1.0 (space-industry knowledge graph)'},
            timeout=5,
        )
        results = r.json()
        if results:
            return float(results[0]['lat']), float(results[0]['lon'])
    except Exception as exc:
        logger.debug('_nominatim_geocode "%s": %s', address, exc)
    return None, None


def _geocode_office_relations(topic: str, assertion_ids: list) -> None:
    """
    After a location research run:
    - Geocode headquarters_address claim → store lat/lon on has_office_in(hq) qualifier
    - Geocode address qualifier on every has_office_in relation that has one but lacks lat/lon
    """
    import time as _time
    from django.db.models import Q
    from core.models import Entity as _Entity
    from core.normalize import normalize_name as _norm

    entity_norm = _norm(topic)
    entity = (
        _Entity.objects
        .filter(Q(canonical_name__iexact=topic) | Q(aliases__alias_norm=entity_norm))
        .filter(status__in=('active', 'stub'))
        .first()
    )
    if not entity:
        return

    # 1. Geocode HQ → try street address first, fall back to city + country
    hq_rel = (
        Relation.objects
        .filter(subject=entity, predicate_id='has_office_in', superseded_at__isnull=True)
        .filter(qualifiers__office_type='hq')
        .first()
    )
    if hq_rel and not (hq_rel.qualifiers or {}).get('lat'):
        addr_assertion = (
            Assertion.objects
            .filter(pk__in=assertion_ids, attribute_id='headquarters_address')
            .exclude(value_text='')
            .order_by('-confidence')
            .first()
        )
        city_assertion = (
            Assertion.objects
            .filter(pk__in=assertion_ids, attribute_id='headquarters_city')
            .exclude(value_text='')
            .order_by('-confidence')
            .first()
        )
        country_assertion = (
            Assertion.objects
            .filter(pk__in=assertion_ids, attribute_id='headquarters_country')
            .exclude(value_text='')
            .order_by('-confidence')
            .first()
        )
        geocode_query = None
        if addr_assertion:
            geocode_query = addr_assertion.value_text
        elif city_assertion or country_assertion:
            parts = [a.value_text for a in [city_assertion, country_assertion] if a]
            geocode_query = ', '.join(parts)

        if geocode_query:
            lat, lon = _geocode_with_fallback(geocode_query) if addr_assertion else _nominatim_geocode(geocode_query)
            if lat is not None:
                # Store on the relation qualifier
                q = dict(hq_rel.qualifiers or {})
                q['lat'], q['lon'] = lat, lon
                hq_rel.qualifiers = q
                hq_rel.save(update_fields=['qualifiers'])
                # Also store directly on the entity so map_data doesn't need the relation
                entity.latitude = lat
                entity.longitude = lon
                entity.has_street_address = bool(addr_assertion)
                entity.save(update_fields=['latitude', 'longitude', 'has_street_address'])
                logger.info('geocoded HQ "%s" via "%s" → (%.5f, %.5f)', topic, geocode_query, lat, lon)
                _time.sleep(1.1)

    # 2. Geocode address qualifier on all other has_office_in relations
    office_rels = (
        Relation.objects
        .filter(subject=entity, predicate_id='has_office_in', superseded_at__isnull=True)
        .exclude(qualifiers__office_type='hq')
    )
    for rel in office_rels:
        q = dict(rel.qualifiers or {})
        if q.get('lat') or not q.get('address'):
            continue  # already geocoded or no address to use
        lat, lon = _geocode_with_fallback(q['address'])
        if lat is not None:
            q['lat'], q['lon'] = lat, lon
            rel.qualifiers = q
            rel.save(update_fields=['qualifiers'])
            logger.info('geocoded office "%s" → (%.5f, %.5f)', q['address'], lat, lon)
            _time.sleep(1.1)


PAUSE_FLAG = 'forespace:tasks:paused'


def is_paused() -> bool:
    import redis as _redis
    from django.conf import settings as _settings
    try:
        return bool(_redis.from_url(_settings.CELERY_BROKER_URL).get(PAUSE_FLAG))
    except Exception:
        return False


@shared_task(bind=True, queue='extract', max_retries=2, default_retry_delay=30)
def research_topic(self, topic: str, topic_type: str = 'company', cascade_depth: int = 0, search_angle: str = None):
    """
    Use Perplexity Sonar to research a topic and write structured knowledge to the DB.
    topic_type: 'company' | 'question' | 'news'
    """
    if is_paused():
        logger.info('research_topic: paused — dropping task for "%s"', topic)
        return

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
        angle_str = f'\nSearch focus for this run: {search_angle}' if search_angle else ''
        user_msg = (
            f'Research the space-industry company or organisation "{topic}". '
            f'Find current facts, events, intelligence, and relationships from recent web sources. '
            f'Extract as much as possible: funding history, launches, contracts, challenges, '
            f'technology choices, competitive position, key people, strategic pivots, '
            f'and all relationships with other companies, agencies, and assets.'
            f'{angle_str}\n\n'
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
        angle_str = f'\nSearch focus for this run: {search_angle}' if search_angle else ''
        user_msg = (
            f'Research "{topic}" specifically for its involvement in the space industry. '
            f'What satellite services does it operate or use? What space investments, '
            f'partnerships, or contracts does it have? What launch customers, ground '
            f'infrastructure, or spectrum assets are relevant? '
            f'Extract all relationships to space companies, agencies, and assets. '
            f'Ignore all non-space activities entirely.'
            f'{angle_str}\n\n'
            f'{_vocab_block()}'
            f'{ctx}'
            f'{_seen_block(seen)}'
        )
    elif topic_type == 'research':
        seen = _seen_urls_for_company(topic)
        ctx = _entity_context_block(topic)
        user_msg = (
            f'Find ALL published research outputs, papers, and technical work by "{topic}" '
            f'related to the space industry. Go deep — search conference proceedings, '
            f'preprint servers, journal archives, and institutional repositories.\n\n'
            f'Search these venues specifically:\n'
            f'- IAC (International Astronautical Congress) proceedings\n'
            f'- AIAA SciTech, AIAA Aviation, AIAA Propulsion & Energy\n'
            f'- ION GNSS+, ION ITM, ION Pacific PNT\n'
            f'- ESA symposia: EDHPC, ESTEC workshops, ESA/CNES/DLR/TU Delft events\n'
            f'- IEEE Aerospace Conference, SmallSat Conference, Reinventing Space\n'
            f'- arXiv (astro-ph, eess.SP, physics.space-ph), TechRxiv, NASA Technical Reports\n'
            f'- Acta Astronautica, Journal of Spacecraft and Rockets, Advances in Space Research, JGCD\n'
            f'- ESA ESTEC study contracts, SBIR/STTR, EU Horizon deliverables, PhD/MSc theses\n\n'
            f'For EACH paper, presentation, technical report, or thesis found:\n'
            f'  Use event_type=publication. Include:\n'
            f'  - Exact title (not a paraphrase)\n'
            f'  - In description: conference/journal name, year, specific findings with numbers if available\n'
            f'  - participants: every co-author and their organisation\n'
            f'  - source_url: direct link to paper, abstract, or proceedings page\n\n'
            f'For each research grant or study contract: use event_type=research_grant.\n\n'
            f'For the overall research programme: use category=research fragments with:\n'
            f'  - Specific technical focus and methodology\n'
            f'  - Key results or performance metrics achieved\n'
            f'  - Institutional collaborators and partners\n\n'
            f'{_vocab_block()}'
            f'{ctx}'
            f'{_seen_block(seen)}'
        )
    elif topic_type == 'funding':
        seen = _seen_urls_recent(days=30, limit=100)
        user_msg = (
            f'Research space industry funding activity for: "{topic}"\n\n'
            f'Search for ALL of the following:\n'
            f'1. ACTIVE GRANT CALLS — open calls from EU (Horizon Europe, EIC), ESA programmes '
            f'(ARTES, GSTP, ScyLight, NAVISP, FAST, Open Space Innovation Platform), '
            f'national agencies (UKSA, CNES, DLR, ASI, JAXA, NASA SBIR/STTR), '
            f'and innovation bodies (Innovate UK, Bpifrance, CDTI, FFG). '
            f'For each: call ID, deadline, budget envelope, eligible entity types, topic description.\n'
            f'Use event_type=grant_call with date=deadline.\n\n'
            f'2. RECENT GRANT AWARDS — which space companies or universities received grants, '
            f'from which programme, how much, and for what purpose.\n'
            f'Use event_type=grant_award.\n\n'
            f'3. EQUITY ROUNDS — seed, Series A/B/C/D, growth rounds, strategic investments, '
            f'IPOs, and SPAC mergers in the space industry. Include lead investors and co-investors.\n'
            f'Use event_type=funding_round. Classify using funding_round_series: '
            f'pre_seed | seed | series_a | series_b | series_c | series_d_plus | growth | '
            f'strategic | ipo | spac.\n\n'
            f'4. DEBT & CONVERTIBLE — venture debt, EIB loans, convertible notes and SAFEs '
            f'raised by space companies.\n'
            f'Use event_type=debt_financing or event_type=convertible.\n\n'
            f'5. INVESTOR ACTIVITY — which VCs, corporate VCs, government funds, and angels '
            f'are most active in space. Extract relations: invested_in, co_invested_with, received_grant_from.\n\n'
            f'CRITICAL — always distinguish the INSTITUTION from its FUNDING TOOL:\n'
            f'  Institution (entity_type=entity|investor): European Commission, ESA, Generalitat de Catalunya, EIB, Innovate UK\n'
            f'  Funding programme (entity_type=program): Horizon Europe, ESA ARTES, Préstecs ICF, EIC Accelerator, Smart Grant\n'
            f'  Always use the full institutional name — never shorten to a geographic area.\n'
            f'  Extract the chain as TWO relations:\n'
            f'    1. institution → administers → programme\n'
            f'    2. company → received_grant_from → programme (NOT the institution directly)\n'
            f'  This lets users navigate "what programmes does ESA offer?" and '
            f'"who received Horizon Europe funding?" independently.\n\n'
            f'Be exhaustive — extract every funding event, grant, and investor relation you find.\n\n'
            f'{_vocab_block()}'
            f'{_seen_block(seen)}'
        )
    elif topic_type == 'funding_program':
        seen = _seen_urls_for_company(topic)
        ctx = _entity_context_block(topic)
        user_msg = (
            f'Research the space industry funding instrument: "{topic}"\n\n'
            f'Find ALL of the following — be exhaustive:\n\n'
            f'1. OPEN & UPCOMING CALLS — for each call:\n'
            f'   - call ID or reference number\n'
            f'   - call_status: "open" (accepting proposals now), "upcoming" (announced but not yet open), or "closed"\n'
            f'   - deadline date (use as the event date)\n'
            f'   - budget envelope (total available for this call)\n'
            f'   - topic or scope description\n'
            f'   - call_url: the direct URL to the call page or application portal\n'
            f'   Use event_type=grant_call. Set call_status and call_url on each event.\n'
            f'   Extract ALL known calls — open, upcoming, and recently closed.\n\n'
            f'2. ELIGIBILITY — who can apply: company size (SME, startup, large enterprise, '
            f'university, research institute), geographic restriction, sector focus, '
            f'Technology Readiness Level (TRL) requirements, nationality constraints.\n'
            f'Also extract the application_url (programme-level portal where you start the application).\n\n'
            f'3. AWARD SIZE — minimum and maximum grant per application. Typical ticket size. '
            f'Co-funding rate (what % the programme covers).\n\n'
            f'4. RECENT AWARDS — who received funding from this instrument in the last 3 years, '
            f'how much, and for what project or purpose.\n'
            f'Use event_type=grant_award for each award. Include the recipient as subject_mention.\n\n'
            f'5. ADMINISTRATOR — which institution runs or administers this instrument.\n'
            f'Extract relation: institution → administers → "{topic}"\n\n'
            f'6. TRACK RECORD — total capital deployed, number of companies funded, success stories.\n\n'
            f'{_vocab_block()}'
            f'{ctx}'
            f'{_seen_block(seen)}'
        )
    elif topic_type == 'person':
        seen = _seen_urls_for_company(topic)
        ctx = _entity_context_block(topic)
        user_msg = (
            f'Research the following person in the space industry: "{topic}"\n\n'
            f'Extract a compact, focused profile — do NOT produce a full CV. Focus on:\n\n'
            f'1. CURRENT ROLE — title and organisation right now.\n'
            f'2. SPACE CONTRIBUTION — the 2-3 most significant things this person has done '
            f'in the space industry: programmes led, technologies developed, companies founded, '
            f'key decisions made. Be specific — include names and numbers where available.\n'
            f'3. CAREER THREAD — the career arc in 1-2 sentences: where they came from and '
            f'where they are going. Only include prior roles that are relevant to understanding '
            f'their space expertise.\n'
            f'4. RESEARCH — any published papers, patents, or conference presentations '
            f'directly related to space (IAC, AIAA, IEEE Aerospace, etc.).\n'
            f'5. RELATIONS — which organisations they have worked for or founded, '
            f'and which key people they collaborate with.\n\n'
            f'Do NOT extract: education history, non-space career details, personal information, '
            f'awards unrelated to space, or generic biographical facts.\n\n'
            f'{_vocab_block()}'
            f'{ctx}'
            f'{_seen_block(seen)}'
        )
    elif topic_type == 'end_user':
        seen = _seen_urls_for_company(topic)
        ctx = _entity_context_block(topic)
        user_msg = (
            f'Research "{topic}" specifically for its connection to the space industry.\n\n'
            f'This entity is an end user — its primary business is NOT space, but it consumes '
            f'space services or satellite-derived data. Focus exclusively on:\n\n'
            f'1. SPACE SERVICE USED — what specific space service, satellite data, or '
            f'space-derived capability does this entity use? '
            f'(e.g. Earth observation, GNSS/PNT, satellite communications, AIS tracking, '
            f'weather data, satellite imagery for agriculture, etc.)\n'
            f'2. SERVICE PROVIDER — which company, agency, or constellation provides '
            f'the space service to this entity? Extract the relation: '
            f'"{topic}" → customer_of → [space company or programme].\n'
            f'3. PURPOSE — for what operational purpose does this entity use the space service? '
            f'(e.g. fleet tracking, crop monitoring, disaster response, navigation, '
            f'broadband connectivity). Keep it to 1-2 sentences.\n'
            f'4. SCALE — if findable: approximate volume, coverage area, or contract value '
            f'associated with their space service use.\n\n'
            f'Do NOT extract: general company facts, financials, employee count, headquarters, '
            f'non-space products or services, competitive position in their own industry, '
            f'or any information unrelated to their use of space services.\n\n'
            f'{_vocab_block()}'
            f'{ctx}'
            f'{_seen_block(seen)}'
        )
    elif topic_type == 'location':
        ctx = _entity_context_block(topic)
        user_msg = (
            f'Find the complete location profile for "{topic}" in the space industry.\n\n'
            f'Extract ALL of the following:\n'
            f'1. HEADQUARTERS — the primary registered or operational headquarters.\n'
            f'   Use claims: headquarters_city, headquarters_country, and headquarters_address.\n'
            f'   headquarters_address: full street address including street, city, postal code, '
            f'   and country (e.g. "350 Fifth Avenue, New York, NY 10118, US").\n'
            f'2. ALL OFFICES, FACILITIES, AND SITES — every additional office, factory, R&D center, '
            f'   launch site, ground station, or test facility beyond the HQ.\n'
            f'   For EACH location use a has_office_in relation → the city name.\n'
            f'   Qualifiers MUST include:\n'
            f'     office_type: "hq" | "office" | "facility" | "rd_center"\n'
            f'     address: full street address if findable (same format as headquarters_address)\n'
            f'   Example qualifiers: {{"office_type": "office", "address": "1 Angus Robertson Drive, Inverness IV2 7WG, UK"}}\n'
            f'3. If only a country is known (not a specific city), still extract it.\n\n'
            f'Focus ONLY on location data — do not extract funding, events, or other facts.\n\n'
            f'{_vocab_block()}'
            f'{ctx}'
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
                {'role': 'system', 'content': get_prompt('research_main', _SYSTEM)},
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
        # 402 = out of credits — set pause flag and do NOT retry
        if '402' in str(exc):
            import redis as _redis
            from django.conf import settings as _settings
            _redis.from_url(_settings.CELERY_BROKER_URL).set(PAUSE_FLAG, '1')
            logger.warning('research_topic: 402 out of credits — pause flag set, task dropped')
            return
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
        entity_type_hint = claim.get('subject_type', 'company')
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
                    subject_context=f'Researching: {topic} ({topic_type})',
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

    _subject_ctx = f'Researching: {topic} ({topic_type})'

    # Primary entity hint: collapse verbose mentions like "NASA's SpaceX Crew-15 mission"
    # back to the topic entity (e.g. "Crew-15") in resolve_mention.
    from core.normalize import normalize_name as _norm
    _primary_id = name_to_id.get(topic)
    _primary_norm = _norm(topic) if _primary_id else None

    # ── Store events ──────────────────────────────────────────────────────
    events_stored, event_entity_ids = _store_events(events, name_to_id, doc, source, subject_context=_subject_ctx, primary_entity_id=_primary_id, primary_entity_norm=_primary_norm)

    # ── Store fragments ───────────────────────────────────────────────────
    fragments_stored, fragment_entity_ids = _store_fragments(fragments, name_to_id, doc, source, subject_context=_subject_ctx, primary_entity_id=_primary_id, primary_entity_norm=_primary_norm)

    # ── Store relations (inline — Sonar had full web context) ─────────────
    relations_stored, relation_entity_ids = _store_relations(relations, doc, source, subject_context=_subject_ctx, primary_entity_id=_primary_id, primary_entity_norm=_primary_norm)

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

    # ── Geocode addresses from location runs ─────────────────────────────
    if topic_type == 'location':
        _geocode_office_relations(topic, new_ids)

    # ── Downstream tasks ─────────────────────────────────────────────────
    if new_ids:
        from ingest.tasks.adjudicate import adjudicate_assertions
        from ingest.tasks.project import refresh_entity_current
        adjudicate_assertions.delay(new_ids)
        refresh_entity_current.apply_async(countdown=5)

    # Collect ALL touched entities: claims + event subjects/participants + fragments + relation subjects/objects
    all_entity_ids = set(entity_ids) | event_entity_ids | fragment_entity_ids | relation_entity_ids
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

        # Targeted dedup for all entities touched in this run (5 min delay so
        # classify/summarise have time to populate alias data first).
        from ingest.tasks.dedup import dedup_entities
        dedup_entities.apply_async(args=[entity_id_strs], countdown=300)

        # Cascade: queue every discovered entity not recently researched,
        # sorted by accepted assertion count ascending — entities with the least
        # known information go first, naturally balancing coverage across the graph.
        # end_user and location entities are leaf nodes — do not cascade from them.
        if topic_type in ('end_user', 'location'):
            return {'accepted': accepted, 'rejected': rejected, 'events': events_stored, 'fragments': fragments_stored, 'relations': relations_stored}
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
            .exclude(entity_type='geography')  # cities/countries are relation targets, never researched
            .exclude(space_relevance__lt=50, space_relevance__isnull=False)  # Sonar for 50+, null, skip 0/20
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

    # ── Search angle + research follow-ups (primary company runs only) ──────
    # Generate targeted search angles and a dedicated research intelligence call.
    # Only on user-triggered company runs (not cascades, not angle/research runs).
    if topic_type in ('company', 'space_angle') and cascade_depth == 0 and search_angle is None:
        # Dedicated deep-research call: hunts papers, conferences, grants
        research_topic.apply_async(
            args=[topic, 'research'],
            kwargs={'cascade_depth': 0},
            countdown=240,  # 4 min after primary
        )
        logger.info('research_topic: research call queued "%s"', topic)
    if topic_type in ('company', 'space_angle') and cascade_depth == 0 and search_angle is None:
        angles = _generate_search_angles(topic, topic_type)
        for i, angle in enumerate(angles):
            research_topic.apply_async(
                args=[topic, topic_type],
                kwargs={'cascade_depth': 0, 'search_angle': angle},
                countdown=180 + i * 90,  # stagger: 3min, 4.5min, 6min after primary
            )
            logger.info('research_topic: angle run queued "%s" → %s', topic, angle)

    return {'accepted': accepted, 'rejected': rejected, 'events': events_stored, 'fragments': fragments_stored, 'relations': relations_stored}
