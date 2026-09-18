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


@shared_task(bind=True, queue='extract', max_retries=1, default_retry_delay=120)
def synthesise_entity_summary(self, entity_id: str):
    """Build or refresh the synthesised prose summary for an entity."""
    from core.models import Entity
    try:
        entity = Entity.objects.get(pk=entity_id)
    except Entity.DoesNotExist:
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
                {'role': 'system', 'content': _SYSTEM},
                {'role': 'user', 'content': user_msg},
            ],
            max_tokens=600,
            temperature=0,
        )
        log_call('synthesise_entity_summary', model, resp,
                 duration_ms=int((time.monotonic() - t0) * 1000))
        raw = resp.choices[0].message.content.strip()
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

    EntitySummary.objects.update_or_create(
        entity=entity,
        defaults={
            'overview':             data.get('overview', ''),
            'challenges':           data.get('challenges', []),
            'strategic_bets':       data.get('strategic_bets', []),
            'competitive_position': data.get('competitive_position', ''),
            'source_count':         len(events) + len(fragments),
        },
    )
    logger.info('synthesise_entity_summary: updated entity=%s (%d sources)', entity_id, len(events) + len(fragments))
