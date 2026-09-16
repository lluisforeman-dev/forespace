"""Taxonomy classification task — §6.

Builds an entity profile from accepted assertions, then asks the LLM to assign
nodes across all 6 taxonomy facets with weighted membership.

Anti-self-confirmation (§9): classification sees the entity's own accepted facts
(not the graph state of *other* entities), which is acceptable — the LLM is
classifying based on the entity's known attributes, not anchoring on prior classifications.
"""
from __future__ import annotations

import json
import logging
from typing import Literal

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from pydantic import BaseModel, AliasChoices, Field

from core.models import Assertion, Classification, Entity, Taxonomy, TaxonomyNode
from ingest.ai import get_client
from ingest.cost import log_call

logger = logging.getLogger(__name__)


class FacetClassification(BaseModel):
    # LLM sometimes sends 'facet' instead of 'facet_key'
    facet_key: str = Field(validation_alias=AliasChoices('facet_key', 'facet'), default='')
    node_path: str | None = None               # e.g. 'upstream.launch.small_lift'
    weight: float = 0.5                        # 0.0–1.0, weighted membership
    is_primary: bool = False
    confidence: Literal["high", "medium", "low"] = "medium"


class ClassificationResult(BaseModel):
    classifications: list[FacetClassification]


_SYSTEM = """\
You are a taxonomy classifier for a space-industry knowledge graph.
Given a company profile, assign it to nodes in each of the provided taxonomy facets.
A company can have weighted membership across multiple nodes (weights sum to ~1.0 per facet).
Only use node paths from the allowed list. Return JSON: {"classifications": [...]}"""


def _build_profile(entity: Entity) -> str:
    """Build a short text profile from accepted assertions."""
    assertions = (
        Assertion.objects
        .filter(entity=entity, status='accepted', superseded_at__isnull=True)
        .select_related('attribute')
        .order_by('attribute_id')
    )
    lines = [f'Company: {entity.canonical_name}']
    for a in assertions:
        val = a.value_text or a.value_num or a.value_date or a.value_bool
        if val is not None:
            lines.append(f'  {a.attribute_id}: {val}{" " + a.unit if a.unit else ""}')
    return '\n'.join(lines)


def _build_taxonomy_vocab() -> str:
    """List all active taxonomy nodes for the LLM prompt."""
    lines = []
    for taxonomy in Taxonomy.objects.filter(status='active').prefetch_related('nodes'):
        lines.append(f'\nFacet: {taxonomy.key} ({taxonomy.key})')
        for node in taxonomy.nodes.all().order_by('path'):
            lines.append(f'  {node.path}: {node.label} — {node.definition[:120]}')
    return '\n'.join(lines) or '(run seed_taxonomy first)'


@shared_task(bind=True, queue='extract', max_retries=2)
def classify_entity(self, entity_id: str, run_id: str | None = None):
    """Classify an entity across all taxonomy facets."""
    try:
        entity = Entity.objects.get(pk=entity_id)
    except Entity.DoesNotExist:
        return

    profile = _build_profile(entity)
    vocab = _build_taxonomy_vocab()
    if '(run seed_taxonomy' in vocab:
        logger.warning('classify_entity: no taxonomy nodes found — run seed_taxonomy first')
        return

    model = settings.AI_MODEL
    user_msg = f'{profile}\n\nAllowed taxonomy nodes:\n{vocab}'

    try:
        resp = get_client().chat.completions.create(
            model=model,
            messages=[
                {'role': 'system', 'content': _SYSTEM},
                {'role': 'user', 'content': user_msg},
            ],
            response_format={'type': 'json_object'},
            max_tokens=800,
            temperature=0,
        )
        log_call('extract', model, resp, entity=entity)
        result = ClassificationResult.model_validate_json(resp.choices[0].message.content)
    except Exception as exc:
        logger.error('classify_entity %s error: %s', entity_id, exc)
        raise self.retry(exc=exc)

    # Build lookup of valid nodes
    valid_nodes: dict[tuple[str, str], TaxonomyNode] = {}
    for node in TaxonomyNode.objects.select_related('taxonomy'):
        valid_nodes[(node.taxonomy.key, node.path)] = node

    from core.models import ExtractionRun
    run = ExtractionRun.objects.filter(pk=run_id).first() if run_id else None

    created = skipped = 0
    with transaction.atomic():
        for fc in result.classifications:
            if not fc.node_path:
                skipped += 1
                continue
            node = valid_nodes.get((fc.facet_key, fc.node_path))
            if not node:
                logger.warning('classify: unknown node %s/%s', fc.facet_key, fc.node_path)
                skipped += 1
                continue
            conf = {'high': 85, 'medium': 65, 'low': 45}[fc.confidence]
            Classification.objects.update_or_create(
                entity=entity,
                node=node,
                defaults={
                    'weight': min(1.0, max(0.0, fc.weight)),
                    'is_primary': fc.is_primary,
                    'confidence': conf,
                    'method': 'extracted',
                    'run': run,
                },
            )
            created += 1

    logger.info('classify_entity %s: %d classifications, %d skipped', entity_id, created, skipped)
