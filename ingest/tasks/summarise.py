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
  "competitive_position": "1-2 sentences on how this entity competes or differentiates in its market"
}

Be specific and grounded in the evidence — do not invent facts not present in the input."""

_SYSTEM_WK = """\
You are a space industry analyst. Using your general knowledge, write a brief profile of the given entity.

Return JSON only (no markdown fences):
{
  "overview": "2-4 sentence prose overview — what this entity is, what it does, where it operates",
  "challenges": ["challenge 1", ...],
  "strategic_bets": ["strategic focus 1", ...],
  "competitive_position": "1-2 sentences on competitive standing or relevance"
}

Rules:
- Only include information you are confident about.
- If you have little or no reliable knowledge of this entity, return {"overview": ""} and nothing else.
- Do not invent or guess facts."""

# Entity types we do NOT attempt world-knowledge summaries for:
# - person: too many obscure individuals, low hit rate, privacy concerns
# - geography: not useful
# - document_node: internal structural type
_WK_SKIP_TYPES = {'person', 'geography', 'document_node'}


def _world_knowledge_summary(entity_id: str, entity) -> dict | None:
    """Call LLM for a world-knowledge-based summary of an entity with no collected data.

    Returns the parsed JSON dict on success, or None if the entity is unknown / skipped.
    """
    from core.models import Assertion

    # Build hint from any accepted assertions already in the DB
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

    hint = ''
    if hint_lines:
        hint = '\nKnown attributes:\n' + '\n'.join(hint_lines)

    user_msg = (
        f'Entity: {entity.canonical_name} ({entity.get_entity_type_display()}){hint}\n\n'
        f'Write a profile based on your general knowledge.'
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
            max_tokens=600,
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

    events = list(
        Event.objects
        .filter(entity=entity)
        .order_by('date', 'created_at')[:25]
    )
    fragments = list(
        KnowledgeFragment.objects
        .filter(entity=entity)
        .order_by('-date_of_information', '-created_at')[:30]
    )

    if not events and not fragments:
        # No collected evidence — try world knowledge for eligible types
        if entity.entity_type in _WK_SKIP_TYPES:
            return
        # Skip if a summary already exists (don't overwrite evidence-based with WK)
        if EntitySummary.objects.filter(entity=entity).exists():
            return
        data = _world_knowledge_summary(entity_id, entity)
        if not data:
            return
        overview = data.get('overview', '')
        if not overview:
            return
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

    model = settings.AI_MODEL
    try:
        t0 = time.monotonic()
        resp = get_client().chat.completions.create(
            model=model,
            messages=[
                {'role': 'system', 'content': get_prompt('summarise_entity', _SYSTEM)},
                {'role': 'user', 'content': user_msg},
            ],
            max_tokens=6000,
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
