"""Stage 6 — Entity Summary Synthesis.

After new events and fragments arrive for an entity, synthesise them into
a living prose portrait: overview, challenges, strategic bets, competitive position.
"""
import json
import logging
import re
import time

from celery import shared_task
from django.conf import settings

from core.models import EntitySummary, Event, KnowledgeFragment
from ingest.ai import get_client
from ingest.cost import log_call
from ingest.prompts import get_prompt

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are a space industry analyst synthesising intelligence about a specific entity.
Given a timeline of events and knowledge fragments, produce a structured summary.

Return JSON only (no markdown fences):
{
  "overview": "3-5 sentence prose overview — what the entity does, where it operates, what stage it is at",
  "challenges": ["specific challenge 1", "specific challenge 2", ...],
  "strategic_bets": ["key strategic choice 1", "key strategic choice 2", ...],
  "competitive_position": "1-2 sentences on how this entity competes or differentiates in its market",
  "space_relevance": 0|20|50|100
}

Space relevance (score based on the evidence provided):
  100 — core space industry (launch, satellites, propulsion, EO, ground systems, space investors, agencies)
   50 — adjacent / somewhat related (defence primes with space division, dual-use tech, space data end-users)
   20 — very tangential (general bank that financed one space deal, occasional space work)
    0 — no meaningful space connection

Be specific and grounded in the evidence — do not invent facts not present in the input."""

_SYSTEM_WK = """\
You are a space industry analyst. Using your general knowledge, write a profile of the given entity \
and score how space-relevant it is.

Return JSON only (no markdown fences):
{
  "overview": "2-4 sentence prose overview — what this entity is, what it does, where it operates",
  "challenges": ["challenge 1", ...],
  "strategic_bets": ["strategic focus 1", ...],
  "competitive_position": "1-2 sentences on competitive standing or relevance",
  "space_relevance": 0|20|50|100
}

Space relevance:
  100 — core space industry (launch, satellites, propulsion, EO, ground systems, space investors, agencies)
   50 — adjacent / somewhat related (defence primes with space division, dual-use tech, space data end-users)
   20 — very tangential (general bank that financed one launch, occasional space work)
    0 — no meaningful space connection (crypto/DeFi, retail, pharma, oil & gas, consumer brand, etc.)

Rules:
- Only include information you are confident about.
- If you have little or no reliable knowledge of this entity, set overview to "" but still score space_relevance.
- Do not invent or guess facts."""

# Entity types we do NOT attempt world-knowledge summaries for:
# - person: too many obscure individuals, low hit rate, privacy concerns
# - geography: not useful
# - document_node: internal structural type
_WK_SKIP_TYPES = {'person', 'geography', 'document_node'}


def _world_knowledge_summary(entity_id: str, entity, brief: bool = False) -> dict | None:
    """Call LLM for a world-knowledge-based summary of an entity with no collected data.

    brief=True: overview only, 1-2 sentences (for space_relevance=20 entities).
    brief=False: full 4-field profile (for space_relevance=50 entities).
    Returns the parsed JSON dict on success, or None if the entity is unknown / skipped.
    """
    from core.models import Assertion

    assertions = (
        Assertion.objects
        .filter(entity=entity, status='accepted')
        .order_by('-confidence')[:10]
    )
    hint_lines = []
    for a in assertions:
        val = a.value_text or (str(a.value_num) if a.value_num is not None else None) \
              or (str(a.value_date) if a.value_date else None)
        if val:
            hint_lines.append(f'  {a.attribute_key}: {val[:200]}')

    hint = '\nKnown attributes:\n' + '\n'.join(hint_lines) if hint_lines else ''

    if brief:
        instruction = 'Write a 1-2 sentence overview only. Return JSON: {"overview": "..."}. If unknown return {"overview": ""}.'
        max_tokens = 120
    else:
        instruction = 'Write a profile based on your general knowledge.'
        max_tokens = 600

    user_msg = (
        f'Entity: {entity.canonical_name} ({entity.get_entity_type_display()}){hint}\n\n'
        f'{instruction}'
    )

    model = settings.AI_MODEL_FAST
    try:
        t0 = time.monotonic()
        resp = get_client().chat.completions.create(
            model=model,
            messages=[
                {'role': 'system', 'content': _SYSTEM_WK},
                {'role': 'user', 'content': user_msg},
            ],
            response_format={'type': 'json_object'},
            max_tokens=max_tokens,
            temperature=0,
        )
        log_call('synthesise_entity_summary_wk', model, resp,
                 duration_ms=int((time.monotonic() - t0) * 1000))
        raw = (resp.choices[0].message.content or '').strip()
        if not raw:
            return None
        data = json.loads(raw)
        if not data.get('overview', '').strip():
            return None
        return data
    except Exception as exc:
        logger.warning('world_knowledge_summary entity=%s: %s', entity_id, exc)
        return None


@shared_task(bind=True, queue='extract', max_retries=1, default_retry_delay=120)
def synthesise_entity_summary(self, entity_id: str):
    """Build or refresh the synthesised prose summary for an entity.

    If the entity has collected events/fragments, synthesises from that evidence.
    Otherwise falls back to a world-knowledge LLM call (skipping person/geography).
    """
    from core.models import Entity
    try:
        entity = Entity.objects.get(pk=entity_id)
    except Entity.DoesNotExist:
        return

    if entity.status == 'merged':
        return

    # Condense input for adjacent entities (space_relevance=50): fewer sources, faster model
    score = entity.space_relevance
    condensed = score is not None and score < 100

    events = list(
        Event.objects
        .filter(entity=entity)
        .order_by('date', 'created_at')[:10 if condensed else 25]
    )
    fragments = list(
        KnowledgeFragment.objects
        .filter(entity=entity)
        .order_by('-date_of_information', '-created_at')[:10 if condensed else 30]
    )

    if not events and not fragments:
        # No collected evidence — world-knowledge assessment.
        if entity.entity_type in _WK_SKIP_TYPES:
            return

        already_scored = entity.space_relevance is not None
        already_summarised = EntitySummary.objects.filter(entity=entity).exists()

        # Skip only if confirmed non-space AND already described — nothing to add.
        if already_scored and entity.space_relevance == 0 and already_summarised:
            return

        # Use brief mode for score=20 entities that already have a summary (just re-score)
        brief = already_scored and entity.space_relevance is not None and entity.space_relevance <= 20 and already_summarised
        data = _world_knowledge_summary(entity_id, entity, brief=brief)
        if not data:
            return
        overview = data.get('overview', '')

        _, created = EntitySummary.objects.update_or_create(
            entity=entity,
            defaults={
                'overview':             overview,
                'challenges':           data.get('challenges', []),
                'strategic_bets':       data.get('strategic_bets', []),
                'competitive_position': data.get('competitive_position', ''),
                'source_count':         0,
            },
        )
        logger.info('synthesise_entity_summary WK: entity=%s overview_len=%d', entity_id, len(overview))

        # Promote space_relevance — never reduce, only increase.
        # Re-assessment with new WK data can reveal higher relevance.
        score = data.get('space_relevance')
        if score is not None:
            try:
                score = max(0, min(100, int(score)))
            except (TypeError, ValueError):
                score = None
        if score is not None and (entity.space_relevance is None or score > entity.space_relevance):
            entity.space_relevance = score
            entity.save(update_fields=['space_relevance'])
            logger.info('synthesise_entity_summary WK: entity=%s space_relevance promoted to %d', entity_id, score)
            # Newly promoted to 50+: queue research pipeline
            if score >= 50 and not already_scored:
                from ingest.tasks.research import research_topic
                _TYPE_TO_TOPIC = {
                    'company': 'company', 'investor': 'company', 'entity': 'company',
                    'university': 'company', 'asset': 'company',
                    'funding_program': 'funding_program', 'end_user': 'end_user',
                    'program': 'question', 'facility': 'question',
                    'person': 'person', 'event': 'question',
                }
                topic_type = _TYPE_TO_TOPIC.get(entity.entity_type, 'company')
                research_topic.apply_async(
                    args=[entity.canonical_name, topic_type],
                    kwargs={'cascade_depth': 0},
                    countdown=120,
                )

        if created and overview:
            from ingest.tasks.resolve import enrich_entity_aliases
            enrich_entity_aliases.apply_async(
                args=[entity_id, entity.canonical_name, entity.entity_type],
                kwargs={'context': overview},
                countdown=2,
            )
        return

    parts = []
    if events:
        parts.append('TIMELINE:')
        for ev in events:
            date_str = str(ev.date) if ev.date else 'unknown date'
            amt = f' (${ev.amount_usd:,.0f})' if ev.amount_usd else ''
            parts.append(f'  {date_str} [{ev.event_type}] {ev.title}{amt}: {ev.description}')

    if fragments:
        parts.append('\nKNOWLEDGE:')
        for frag in fragments:
            parts.append(f'  [{frag.category}] {frag.text}')

    context = '\n'.join(parts)
    user_msg = (
        f'Entity: {entity.canonical_name} ({entity.get_entity_type_display()})\n\n'
        f'{context}\n\n'
        f'Synthesise a summary.'
    )

    model = settings.AI_MODEL_FAST if condensed else settings.AI_MODEL
    try:
        t0 = time.monotonic()
        resp = get_client().chat.completions.create(
            model=model,
            messages=[
                {'role': 'system', 'content': get_prompt('summarise_entity', _SYSTEM)},
                {'role': 'user', 'content': user_msg},
            ],
            max_tokens=1500 if condensed else 6000,
            temperature=0,
        )
        log_call('synthesise_entity_summary', model, resp,
                 duration_ms=int((time.monotonic() - t0) * 1000))
        content = resp.choices[0].message.content
        if not content:
            logger.warning('synthesise_entity_summary entity=%s: empty response', entity_id)
            return
        raw = content.strip()
    except Exception as exc:
        logger.error('synthesise_entity_summary entity=%s: %s', entity_id, exc)
        raise self.retry(exc=exc)

    text = raw
    m = re.search(r'```(?:json)?\s*([\s\S]+?)\s*```', text)
    if m:
        text = m.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.error('synthesise_entity_summary bad JSON entity=%s: %r', entity_id, raw[:200])
        return

    overview = data.get('overview', '')
    _, created = EntitySummary.objects.update_or_create(
        entity=entity,
        defaults={
            'overview':             overview,
            'challenges':           data.get('challenges', []),
            'strategic_bets':       data.get('strategic_bets', []),
            'competitive_position': data.get('competitive_position', ''),
            'source_count':         len(events) + len(fragments),
        },
    )
    logger.info('synthesise_entity_summary: updated entity=%s (%d sources)', entity_id, len(events) + len(fragments))

    # Promote space_relevance from evidence — never reduce, only increase.
    # Evidence-based synthesis has richer data than the initial WK assessment,
    # so it may reveal a higher score.
    ev_score = data.get('space_relevance')
    if ev_score is not None:
        try:
            ev_score = max(0, min(100, int(ev_score)))
        except (TypeError, ValueError):
            ev_score = None
    if ev_score is not None and (entity.space_relevance is None or ev_score > entity.space_relevance):
        entity.space_relevance = ev_score
        entity.save(update_fields=['space_relevance'])
        logger.info('synthesise_entity_summary: space_relevance promoted to %d for entity=%s', ev_score, entity_id)

    # On first summary creation, trigger alias enrichment with the overview as context.
    # Aliases are enriched here rather than at stub creation so the LLM has real evidence
    # about the entity (not just its name) — critical for obscure or ambiguous organisations.
    if created and entity.entity_type not in ('geography', 'document_node', 'event') and overview:
        from ingest.tasks.resolve import enrich_entity_aliases
        enrich_entity_aliases.apply_async(
            args=[entity_id, entity.canonical_name, entity.entity_type],
            kwargs={'context': overview},
            countdown=2,
        )
