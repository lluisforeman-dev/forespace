"""Taxonomy classification task — §6.

Builds an entity profile from accepted assertions, then asks the LLM to assign
nodes from the appropriate taxonomy facets for this entity type.

Facet routing per entity type:
  company / investor / entity / university
      → value_chain, orbit_regime, customer_type, maturity,
        research_area, adjacent_sector
  funding_program
      → funding_type only  (what KIND of instrument is this?)
  program
      → value_chain, orbit_regime  (what kind of space programme?)
  person
      → research_area only  (what field do they work in?)
  asset
      → value_chain, orbit_regime
  facility / geography / document_node / event
      → skipped (no meaningful taxonomy classification)

Anti-self-confirmation (§9): classification sees the entity's own accepted facts
(not the graph state of *other* entities), which is acceptable — the LLM is
classifying based on the entity's known attributes, not anchoring on prior classifications.
"""
from __future__ import annotations

import json
import logging

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import Assertion, Classification, Entity, Taxonomy, TaxonomyNode
from ingest.ai import get_client
from ingest.cost import log_call
from ingest.prompts import get_prompt

logger = logging.getLogger(__name__)

# Taxonomy facets applicable to each entity type.
# Entity types not listed here are skipped entirely.
_FACETS_FOR_TYPE: dict[str, set[str]] = {
    'company':          {'value_chain', 'orbit_regime', 'customer_type', 'maturity', 'research_area', 'adjacent_sector'},
    'investor':         {'value_chain', 'customer_type', 'maturity', 'adjacent_sector'},
    'entity':           {'value_chain', 'orbit_regime', 'customer_type', 'maturity', 'research_area', 'adjacent_sector'},
    'university':       {'research_area', 'adjacent_sector'},
    'funding_program':  {'funding_type'},
    'program':          {'value_chain', 'orbit_regime'},
    'person':           {'research_area'},
    'asset':            {'value_chain', 'orbit_regime'},
    # End users: what industry sector are they in, who do they sell to, and what downstream
    # space service do they consume? Never classify in orbit/maturity — irrelevant.
    'end_user':         {'adjacent_sector', 'customer_type', 'value_chain'},
}

_SYSTEM_COMPANY = """\
You are a taxonomy classifier for a space-industry knowledge graph.
Given a company profile, assign it to nodes in each of the provided taxonomy facets.
A company can have weighted membership across multiple nodes (weights sum to ~1.0 per facet).
Only use node paths from the allowed list.

Also score how space-relevant this entity is (0–100):
  100 — core space industry (launch, satellites, propulsion, ground systems, EO, etc.)
   75 — primarily space but with significant adjacent activity (defence primes, dual-use tech)
   50 — adjacent / partial (supplies components not exclusive to space, space investor, space data end-user)
   25 — very tangential (e.g. a general VC that made one space investment, a bank that financed a launch)
    0 — no meaningful space connection (crypto, retail, pharma, oil & gas, consumer brand, etc.)

Return JSON: {"classifications": [...], "space_relevance": <0-100>}"""

_SYSTEM_FUNDING_PROGRAM = """\
You are a taxonomy classifier for a space-industry knowledge graph.
Given a funding instrument profile, classify it under the funding_type taxonomy facet.
Assign the single most specific node that describes what TYPE of instrument this is
(e.g. grant.horizon_europe, equity.seed, debt.eib_eif, non_equity.in_kind).
Return JSON: {"classifications": [...]}"""

_SYSTEM_PERSON = """\
You are a taxonomy classifier for a space-industry knowledge graph.
Given a person's profile, classify their primary research or professional area
under the research_area taxonomy facet.
Only use node paths from the allowed list.

Also score how space-relevant this person is (0–100):
  100 — works primarily in the space industry
   50 — adjacent (dual-use research, defence, adjacent tech)
    0 — no meaningful space connection
Return JSON: {"classifications": [...], "space_relevance": <0-100>}"""

_SYSTEM_PROGRAM = """\
You are a taxonomy classifier for a space-industry knowledge graph.
Given a space programme profile, classify it under the value_chain and orbit_regime
taxonomy facets as applicable.
Only use node paths from the allowed list. Return JSON: {"classifications": [...]}"""

_SYSTEM_END_USER = """\
You are a taxonomy classifier for a space-industry knowledge graph.
Given an end-user profile — a company whose primary business is NOT space but which
consumes space services or data — classify it across three facets:
  adjacent_sector : what industry sector is this company in?
  customer_type   : does it sell to governments, commercial clients, or consumers?
  value_chain     : which downstream space service does it consume?
                    (only assign downstream.* nodes — e.g. downstream.earth_observation,
                     downstream.navigation, downstream.satcom. Do NOT assign upstream or
                     midstream nodes.)
Only use node paths from the allowed list. Return JSON: {"classifications": [...]}"""


def _system_prompt_for_type(entity_type: str) -> str:
    if entity_type == 'funding_program':
        return get_prompt('classify_funding_program', _SYSTEM_FUNDING_PROGRAM)
    if entity_type == 'person':
        return get_prompt('classify_person', _SYSTEM_PERSON)
    if entity_type == 'program':
        return get_prompt('classify_program', _SYSTEM_PROGRAM)
    if entity_type == 'end_user':
        return get_prompt('classify_end_user', _SYSTEM_END_USER)
    return get_prompt('classify_company', _SYSTEM_COMPANY)


def _profile_label(entity_type: str) -> str:
    return {
        'funding_program': 'Funding instrument',
        'end_user': 'End user (space services consumer)',
        'person': 'Person',
        'program': 'Space programme',
        'university': 'University',
        'investor': 'Investor',
        'facility': 'Facility',
        'asset': 'Asset',
    }.get(entity_type, 'Company')


def _build_profile(entity: Entity) -> str:
    """Build a short text profile from accepted assertions."""
    assertions = (
        Assertion.objects
        .filter(entity=entity, status='accepted', superseded_at__isnull=True)
        .select_related('attribute')
        .order_by('attribute_id')
    )
    label = _profile_label(entity.entity_type)
    lines = [f'{label}: {entity.canonical_name}']
    for a in assertions:
        val = a.value_text or a.value_num or a.value_date or a.value_bool
        if val is not None:
            lines.append(f'  {a.attribute_id}: {val}{" " + a.unit if a.unit else ""}')
    return '\n'.join(lines)


def _build_taxonomy_vocab(allowed_facets: set[str]) -> str:
    """List active taxonomy nodes for the applicable facets only."""
    lines = []
    for taxonomy in Taxonomy.objects.filter(status='active', key__in=allowed_facets).prefetch_related('nodes'):
        lines.append(f'\nFacet: {taxonomy.key}')
        for node in taxonomy.nodes.filter(status='active').order_by('path'):
            lines.append(f'  {node.path}: {node.label} — {node.definition[:120]}')
    return '\n'.join(lines) or '(no matching taxonomy nodes)'


@shared_task(bind=True, queue='extract', max_retries=2)
def classify_entity(self, entity_id: str, run_id: str | None = None):
    """Classify an entity across the taxonomy facets appropriate for its type."""
    try:
        entity = Entity.objects.get(pk=entity_id)
    except Entity.DoesNotExist:
        return

    allowed_facets = _FACETS_FOR_TYPE.get(entity.entity_type)
    if not allowed_facets:
        logger.debug('classify_entity %s: skipping entity_type=%s', entity_id, entity.entity_type)
        return

    profile = _build_profile(entity)
    vocab = _build_taxonomy_vocab(allowed_facets)
    if '(no matching' in vocab:
        logger.warning('classify_entity %s: no active taxonomy nodes for facets %s', entity_id, allowed_facets)
        return

    system_prompt = _system_prompt_for_type(entity.entity_type)
    model = settings.AI_MODEL_FAST
    user_msg = f'{profile}\n\nAllowed taxonomy nodes:\n{vocab}'

    try:
        resp = get_client().chat.completions.create(
            model=model,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_msg},
            ],
            response_format={'type': 'json_object'},
            max_tokens=3000,
            temperature=0,
        )
        log_call('extract', model, resp, entity=entity)
        content = resp.choices[0].message.content
        if not content:
            logger.warning('classify_entity %s: empty response from model', entity_id)
            return
        text = content.strip()
        text = text.replace('True', 'true').replace('False', 'false').replace('None', 'null')
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.error('classify_entity %s bad JSON (likely truncated): %s', entity_id, exc)
            return
        classifications = raw.get('classifications', [])
    except Exception as exc:
        logger.error('classify_entity %s error: %s', entity_id, exc)
        raise self.retry(exc=exc)

    # Build lookup restricted to the allowed facets
    valid_nodes: dict[tuple[str, str], TaxonomyNode] = {}
    for node in TaxonomyNode.objects.filter(status='active', taxonomy__key__in=allowed_facets).select_related('taxonomy'):
        valid_nodes[(node.taxonomy.key, node.path)] = node

    from core.models import ExtractionRun
    run = ExtractionRun.objects.filter(pk=run_id).first() if run_id else None

    conf_map = {'high': 85, 'medium': 65, 'low': 45}
    created = skipped = 0
    with transaction.atomic():
        for item in classifications:
            if not isinstance(item, dict):
                skipped += 1
                continue
            facet_key = item.get('facet_key') or item.get('facet', '')
            node_path = item.get('node_path') or item.get('node', '')
            if not facet_key or not node_path:
                skipped += 1
                continue
            node = valid_nodes.get((facet_key, node_path))
            if not node:
                logger.warning('classify: unknown node %s/%s for entity_type=%s', facet_key, node_path, entity.entity_type)
                skipped += 1
                continue
            conf = conf_map.get(item.get('confidence', 'medium'), 65)
            Classification.objects.update_or_create(
                entity=entity,
                node=node,
                defaults={
                    'weight': min(1.0, max(0.0, float(item.get('weight', 0.5)))),
                    'is_primary': bool(item.get('is_primary', False)),
                    'confidence': conf,
                    'method': 'extracted',
                    'run': run,
                },
            )
            created += 1

    logger.info('classify_entity %s (%s): %d classifications, %d skipped', entity_id, entity.entity_type, created, skipped)

    # Set space_relevance on the entity so the pipeline can filter non-space entities.
    # Applies to types where the LLM was asked to score relevance (0–100).
    _relevance_types = {'company', 'investor', 'entity', 'university', 'person'}
    if entity.entity_type in _relevance_types:
        score = raw.get('space_relevance', None)
        try:
            new_relevance = max(0, min(100, int(score))) if score is not None else None
        except (TypeError, ValueError):
            new_relevance = None
        # If LLM gave no score but classified successfully, assume relevant
        if new_relevance is None and created > 0:
            new_relevance = 100
        if new_relevance != entity.space_relevance:
            entity.space_relevance = new_relevance
            entity.save(update_fields=['space_relevance'])
            logger.info('classify_entity %s: space_relevance=%s', entity_id, new_relevance)
