"""Taxonomy evolution — triggered automatically after research runs.

Analyses newly ingested entities against the current taxonomy and makes
additive changes directly (no human approval):
  - Extends existing branches when a concept fits under a known parent
  - Creates new root nodes only when nothing in the tree fits
  - Skips nodes too similar to existing ones (SequenceMatcher >= 0.72)
  - Never modifies or removes what is already there
"""
from __future__ import annotations

import json
import logging
from difflib import SequenceMatcher

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from core.models import Assertion, Entity, Taxonomy, TaxonomyNode
from ingest.ai import get_client
from ingest.cost import log_call

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are an expert space-industry ontologist maintaining a living taxonomy.

Given the current taxonomy tree and a batch of recently ingested entities,
identify (a) genuine gaps where new nodes are needed, and (b) existing nodes
whose label or definition no longer accurately reflects the entities using them.

OPERATIONS — each proposal must have an "op" field:
  "add"   — add a new node (path must not already exist)
  "alter" — update the label and/or definition of an existing node
             (path must already exist; you cannot change the path itself)

RULES FOR "add":
1. NEVER propose if the concept is already covered by an existing node.
2. PREFER extending existing branches (child paths) over new root branches.
3. Only propose if at least 2 of the listed entities clearly need it.
4. Paths use lowercase letters, digits, and underscores only, dot-separated.

RULES FOR "alter":
1. Only propose when the current label or definition is genuinely misleading,
   too narrow, or outdated given the real entities now classified there.
2. The change must meaningfully improve accuracy — not just rephrase.
3. Keep the spirit of the original node; do not repurpose it entirely.

GENERAL:
- Limit to 6 proposals total (adds + alters combined).
- If the taxonomy is fine, return {"proposals": []}.

Return ONLY valid JSON (no markdown fences):
{
  "proposals": [
    {
      "op": "add",
      "facet_key": "value_chain",
      "path": "upstream.launch.reusable_heavy",
      "label": "Reusable Heavy Lift",
      "definition": "Heavy-lift launch vehicles designed for booster recovery and re-flight.",
      "examples": ["SpaceX Falcon Heavy", "Starship"],
      "reasoning": "Reusability is a distinct differentiator not captured by upstream.launch.heavy_lift."
    },
    {
      "op": "alter",
      "facet_key": "value_chain",
      "path": "upstream.propulsion",
      "label": "Propulsion & Power Systems",
      "definition": "Manufacturers of rocket engines, spacecraft propulsion units, and in-space power systems.",
      "reasoning": "Power systems companies (solar arrays, batteries) are classified here but the definition excluded them."
    }
  ]
}"""


def _taxonomy_tree() -> str:
    """Full active taxonomy as indented text for the LLM prompt."""
    lines = []
    for taxonomy in Taxonomy.objects.filter(status='active').prefetch_related('nodes'):
        lines.append(f'\nFacet: {taxonomy.key} — {taxonomy.key}')
        for node in taxonomy.nodes.filter(status='active').order_by('path'):
            depth = node.path.count('.')
            indent = '  ' * (depth + 1)
            lines.append(f'{indent}{node.path}: {node.label} — {node.definition[:120]}')
    return '\n'.join(lines)


def _entity_sample(entity_ids: list[str] | None = None, limit: int = 50) -> str:
    """Build a compact entity profile list for the LLM."""
    qs = Entity.objects.filter(status__in=('active', 'stub'))
    if entity_ids:
        qs = qs.filter(id__in=entity_ids)
    else:
        from datetime import timedelta
        qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=14))
    qs = qs.order_by('-created_at')[:limit]

    lines = []
    for entity in qs:
        assertions = (
            Assertion.objects
            .filter(entity=entity, status='accepted')
            .select_related('attribute')
            .order_by('-confidence')[:5]
        )
        facts = ', '.join(
            f"{a.attribute_id}={a.value_text or a.value_num or a.value_date}"
            for a in assertions
            if a.value_text or a.value_num or a.value_date
        )
        lines.append(
            f'- {entity.canonical_name} ({entity.entity_type})'
            + (f': {facts}' if facts else '')
        )
    return '\n'.join(lines)


def _is_too_similar(label: str, definition: str, taxonomy_key: str, threshold: float = 0.72) -> TaxonomyNode | None:
    """Return an existing node if its label+definition is too similar to the proposal."""
    candidates = (
        TaxonomyNode.objects
        .filter(taxonomy__key=taxonomy_key, status__in=('active', 'proposed'))
        .values('id', 'label', 'definition', 'path')
    )
    proposal_text = f'{label} {definition}'.lower()
    for c in candidates:
        existing_text = f"{c['label']} {c['definition']}".lower()
        ratio = SequenceMatcher(None, proposal_text, existing_text).ratio()
        if ratio >= threshold:
            return c
    return None


@shared_task(bind=True, queue='extract', max_retries=1, default_retry_delay=120)
def evolve_taxonomy(self, entity_ids: list[str] | None = None):
    """
    Analyse recently ingested entities and propose additive taxonomy changes.
    Called automatically at the end of research_topic runs.
    entity_ids: if provided, focus on these specific entities; else use last 14 days.
    """
    taxonomy_text = _taxonomy_tree()
    entity_sample = _entity_sample(entity_ids=entity_ids)

    if not entity_sample:
        logger.info('evolve_taxonomy: no entities to analyse')
        return {'proposals': 0}

    user_msg = (
        f'Current taxonomy:\n{taxonomy_text}\n\n'
        f'Newly ingested entities:\n{entity_sample}\n\n'
        f'Identify genuine gaps. Remember: extend existing branches first, '
        f'new roots only when strictly necessary, skip anything already covered.'
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
            max_tokens=4000,
            temperature=0.2,
        )
        log_call('evolve_taxonomy', model, resp)
        data = json.loads(resp.choices[0].message.content)
        proposals = data.get('proposals', [])
    except Exception as exc:
        logger.error('evolve_taxonomy error: %s', exc)
        raise self.retry(exc=exc)

    created = altered = duplicate = skipped = 0
    for p in proposals:
        op = p.get('op', 'add').strip().lower()
        facet_key = p.get('facet_key', '').strip()
        path = p.get('path', '').strip().lower().replace(' ', '_')
        label = p.get('label', '').strip()
        definition = p.get('definition', '').strip()

        if not facet_key or not path or not label:
            skipped += 1
            continue

        taxonomy = Taxonomy.objects.filter(key=facet_key, status='active').first()
        if not taxonomy:
            logger.warning('evolve_taxonomy: unknown facet "%s"', facet_key)
            skipped += 1
            continue

        if op == 'alter':
            updated = TaxonomyNode.objects.filter(
                taxonomy=taxonomy, path=path, status='active',
            ).update(label=label, definition=definition)
            if updated:
                logger.info('evolve_taxonomy: altered %s/%s — "%s"', facet_key, path, label)
                altered += 1
            else:
                logger.warning('evolve_taxonomy: alter target not found %s/%s', facet_key, path)
                skipped += 1
            continue

        # op == 'add'
        if TaxonomyNode.objects.filter(taxonomy=taxonomy, path=path).exists():
            logger.debug('evolve_taxonomy: path %s/%s already exists', facet_key, path)
            duplicate += 1
            continue

        similar = _is_too_similar(label, definition, facet_key)
        if similar:
            logger.info(
                'evolve_taxonomy: proposal "%s" too similar to existing "%s" (%s) — skipping',
                label, similar['label'], similar['path'],
            )
            duplicate += 1
            continue

        _, node_created = TaxonomyNode.objects.get_or_create(
            taxonomy=taxonomy,
            path=path,
            defaults={
                'label': label,
                'definition': definition,
                'examples': p.get('examples', []),
                'status': 'active',
            },
        )
        if node_created:
            logger.info('evolve_taxonomy: added %s/%s — "%s"', facet_key, path, label)
            created += 1
        else:
            duplicate += 1

    logger.info(
        'evolve_taxonomy: %d added, %d altered, %d duplicates skipped, %d invalid',
        created, altered, duplicate, skipped,
    )
    return {'added': created, 'altered': altered, 'duplicates': duplicate, 'skipped': skipped}
