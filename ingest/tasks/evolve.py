"""Taxonomy evolution task.

Periodically reviews recently ingested entities against the current taxonomy,
then asks the LLM to propose new nodes that better capture what the industry
is doing. Proposals land with status='proposed' for human review before going live.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from core.models import Assertion, Entity, Taxonomy, TaxonomyNode
from ingest.ai import get_client
from ingest.cost import log_call

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are an expert in the space industry and knowledge taxonomy design.
You will receive the current taxonomy structure and a sample of recently
discovered entities. Your job is to identify gaps — activities, technologies,
or market segments that real entities represent but that have no fitting node.

Return ONLY valid JSON (no markdown):
{
  "proposals": [
    {
      "facet_key": "value_chain",
      "path": "upstream.launch.reusable_heavy",
      "label": "Reusable Heavy Lift",
      "definition": "Heavy-lift launch vehicles designed for booster recovery and re-flight.",
      "examples": ["SpaceX Falcon Heavy", "Starship"],
      "reasoning": "Reusability is now a distinct market differentiator not captured by existing nodes."
    }
  ]
}

Rules:
- Only propose nodes that are clearly missing — do not duplicate existing ones.
- path must be a dotted ltree-style string using only lowercase letters, digits, and underscores.
- Limit to the most important gaps (max 8 proposals per run).
- If the taxonomy is adequate, return {"proposals": []}."""


def _current_taxonomy_text() -> str:
    lines = []
    for taxonomy in Taxonomy.objects.filter(status='active').prefetch_related('nodes'):
        lines.append(f'\nFacet: {taxonomy.key}')
        for node in taxonomy.nodes.filter(status='active').order_by('path'):
            lines.append(f'  {node.path}: {node.label} — {node.definition[:100]}')
    return '\n'.join(lines)


def _recent_entity_sample(days: int = 30, limit: int = 40) -> str:
    cutoff = timezone.now() - timedelta(days=days)
    entities = (
        Entity.objects
        .filter(status__in=('active', 'stub'), created_at__gte=cutoff)
        .order_by('-created_at')[:limit]
    )
    lines = []
    for entity in entities:
        assertions = (
            Assertion.objects
            .filter(entity=entity, status='accepted')
            .select_related('attribute')
            .order_by('-confidence')[:4]
        )
        facts = ', '.join(
            f"{a.attribute_id}={a.value_text or a.value_num or a.value_date}"
            for a in assertions
            if (a.value_text or a.value_num or a.value_date)
        )
        lines.append(f'- {entity.canonical_name} ({entity.entity_type})'
                     + (f': {facts}' if facts else ''))
    return '\n'.join(lines)


@shared_task(bind=True, queue='extract', max_retries=1, default_retry_delay=60)
def evolve_taxonomy(self):
    """Propose new taxonomy nodes based on recently ingested entities."""
    taxonomy_text = _current_taxonomy_text()
    entity_sample = _recent_entity_sample()

    if not entity_sample:
        logger.info('evolve_taxonomy: no recent entities, skipping')
        return {'proposals': 0}

    user_msg = (
        f'Current taxonomy:\n{taxonomy_text}\n\n'
        f'Recently discovered entities (last 30 days):\n{entity_sample}\n\n'
        f'Identify gaps and propose new taxonomy nodes.'
    )

    model = settings.AI_MODEL
    try:
        resp = get_client().chat.completions.create(
            model=model,
            messages=[
                {'role': 'system', 'content': _SYSTEM},
                {'role': 'user', 'content': user_msg},
            ],
            response_format={'type': 'json_object'},
            max_tokens=3000,
            temperature=0.3,
        )
        log_call('evolve_taxonomy', model, resp)
        data = json.loads(resp.choices[0].message.content)
        proposals = data.get('proposals', [])
    except Exception as exc:
        logger.error('evolve_taxonomy error: %s', exc)
        raise self.retry(exc=exc)

    created = skipped = 0
    for p in proposals:
        facet_key = p.get('facet_key', '')
        path = p.get('path', '')
        if not facet_key or not path:
            skipped += 1
            continue

        taxonomy = Taxonomy.objects.filter(key=facet_key, status='active').first()
        if not taxonomy:
            logger.warning('evolve_taxonomy: unknown facet %s', facet_key)
            skipped += 1
            continue

        _, node_created = TaxonomyNode.objects.get_or_create(
            taxonomy=taxonomy,
            path=path,
            defaults={
                'label': p.get('label', path),
                'definition': p.get('definition', ''),
                'examples': p.get('examples', []),
                'status': 'proposed',
            },
        )
        if node_created:
            logger.info('evolve_taxonomy: proposed %s/%s (%s)', facet_key, path, p.get('label'))
            created += 1
        else:
            skipped += 1

    logger.info('evolve_taxonomy: %d proposed, %d skipped', created, skipped)
    return {'proposals': created, 'skipped': skipped}
