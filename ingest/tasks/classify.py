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

# ── Entity-type audit ────────────────────────────────────────────────────────
# entity_type is assigned write-once by extraction (which defaults to
# 'company' when unsure) and nothing used to re-check it — how "Falcon 9"
# became a company and "Denmark" a company. classify_entity re-audits the
# type against the entity's accumulated evidence on every pass.
_TYPE_AUDIT_SYSTEM = (
    'You are an entity-type auditor for a space-industry knowledge graph. '
    'Answer only with valid JSON.'
)

_TYPE_AUDIT_USER = """\
Given the evidence below, what is the correct entity_type for "{name}"?

The type describes WHAT THE ENTITY ITSELF IS — not what is located in or around
it, and not what it owns. A city or country with aerospace activity is still a
geography. A state launch base is a facility. A rocket, satellite, or other named
product is an asset. An operational undertaking (Artemis, ISS, Galileo) is a
program. A deployable instrument with calls, deadlines and budgets is a
funding_program. A commercial space business is a company. A public institution
or agency is an entity.

Allowed types: company, entity, investor, university, facility, asset, person,
program, funding_program, end_user, geography

Evidence:
{evidence}

Current label: {current_type}

Reply: {{"entity_type": "<one allowed type>", "confidence": "high"|"medium"|"low"}}"""

_CORRECTABLE_TYPES = {
    'company', 'entity', 'investor', 'university', 'facility', 'asset',
    'person', 'program', 'funding_program', 'end_user', 'geography',
}


def _typing_evidence(entity: Entity) -> str:
    """Compact evidence block for the type audit."""
    from core.models import Event, KnowledgeFragment
    parts = []
    for f in KnowledgeFragment.objects.filter(entity=entity).order_by('-confidence')[:3]:
        parts.append(f.text[:220])
    for ev in Event.objects.filter(entity=entity).order_by('-created_at')[:5]:
        parts.append(f'{ev.event_type}: {ev.title}')
    prof = _build_profile(entity)
    if prof.strip():
        parts.append(prof)
    return '\n'.join(parts)[:1200]


def _validate_entity_type(entity: Entity) -> str | None:
    """Re-audit entity.entity_type against the entity's evidence.

    Returns the new type when a high-confidence correction was applied, else
    None. Husks (thin evidence) are never judged; confirmed place names are
    never corrected away from geography. Fail-closed on any error.
    """
    if entity.entity_type not in _CORRECTABLE_TYPES:
        return None
    if entity.entity_type == 'geography':
        from core.normalize import normalize_name
        from ingest.tasks.resolve import _GEOGRAPHIC_BLOCKLIST
        if normalize_name(entity.canonical_name) in _GEOGRAPHIC_BLOCKLIST:
            return None
    evidence = _typing_evidence(entity)
    if len(evidence.strip()) < 80:
        return None  # not enough signal — do not guess
    try:
        resp = get_client().chat.completions.create(
            model=settings.AI_MODEL_FAST,
            messages=[
                {'role': 'system', 'content': _TYPE_AUDIT_SYSTEM},
                {'role': 'user', 'content': _TYPE_AUDIT_USER.format(
                    name=entity.canonical_name, evidence=evidence,
                    current_type=entity.entity_type)},
            ],
            response_format={'type': 'json_object'},
            max_tokens=40,
            temperature=0,
        )
        log_call('classify', settings.AI_MODEL_FAST, resp, entity=entity)
        raw = (resp.choices[0].message.content or '').strip()
        if not raw:
            return None
        raw = raw.replace('True', 'true').replace('False', 'false').replace('None', 'null')
        result = json.loads(raw)
        new_type = result.get('entity_type')
        if result.get('confidence') != 'high' or new_type not in _CORRECTABLE_TYPES:
            return None
        if new_type == entity.entity_type:
            return None
        old = entity.entity_type
        entity.entity_type = new_type
        entity.save(update_fields=['entity_type'])
        logger.info('TYPE CORRECTION %s "%s": %s -> %s',
                    entity.id, entity.canonical_name, old, new_type)
        return new_type
    except Exception as exc:
        logger.warning('type audit failed for %s: %s', entity.id, exc)
        return None

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

Also score how space-relevant this entity is:
  100 — core space industry (launch, satellites, propulsion, EO, ground systems, dedicated space investors, space agencies, etc.)
   50 — adjacent / somewhat related (defence primes with a space division, dual-use tech suppliers, end-users of space data, VCs with notable space portfolio)
   20 — very tangential (a general bank that financed one space deal, a consultancy with occasional space work)
    0 — no meaningful space connection (crypto/DeFi, retail, pharma, oil & gas, consumer brand, unrelated tech, etc.)

Return JSON: {"classifications": [...], "space_relevance": 0|20|50|100}"""

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

Also score how space-relevant this person is:
  100 — works primarily in the space industry
   50 — adjacent (dual-use research, defence, adjacent tech with space application)
   20 — very tangential
    0 — no meaningful space connection
Return JSON: {"classifications": [...], "space_relevance": 0|20|50|100}"""

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
    from ingest.pause import is_paused
    if is_paused('classify'):
        return
    try:
        entity = Entity.objects.get(pk=entity_id)
    except Entity.DoesNotExist:
        return

    # Type audit — re-check write-once typing before classifying. A correction
    # updates entity.entity_type in place, so the facet routing below uses the
    # corrected type.
    _validate_entity_type(entity)

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
    taxonomy_map: dict[str, object] = {}
    for node in TaxonomyNode.objects.filter(status='active', taxonomy__key__in=allowed_facets).select_related('taxonomy'):
        valid_nodes[(node.taxonomy.key, node.path)] = node
        taxonomy_map[node.taxonomy.key] = node.taxonomy
    # Also fetch taxonomies that may have no nodes yet
    for t in Taxonomy.objects.filter(status='active', key__in=allowed_facets):
        taxonomy_map.setdefault(t.key, t)

    from core.models import ExtractionRun
    run = ExtractionRun.objects.filter(pk=run_id).first() if run_id else None

    import re as _re
    _VALID_PATH = _re.compile(r'^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$')

    def _try_expand(facet_key, node_path):
        """Auto-create a taxonomy node if its parent exists and the path is valid.

        Returns the new TaxonomyNode on success, None if expansion is not safe.
        Only expands one level at a time — parent must already be in valid_nodes
        (or be a root node on a known taxonomy).
        """
        if not _VALID_PATH.match(node_path):
            return None
        taxonomy = taxonomy_map.get(facet_key)
        if not taxonomy:
            return None
        parts = node_path.split('.')
        if len(parts) > 1:
            parent_path = '.'.join(parts[:-1])
            if (facet_key, parent_path) not in valid_nodes:
                return None  # Parent missing — don't create orphan nodes
        label = parts[-1].replace('_', ' ').title()
        node, created_now = TaxonomyNode.objects.get_or_create(
            taxonomy=taxonomy,
            path=node_path,
            defaults={
                'label': label,
                'definition': f'Auto-expanded during entity classification.',
                'examples': [],
                'status': 'active',
            },
        )
        if created_now:
            logger.info('classify: auto-expanded taxonomy %s/%s — "%s"', facet_key, node_path, label)
        valid_nodes[(facet_key, node_path)] = node
        return node

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
                node = _try_expand(facet_key, node_path)
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

    # Update space_relevance — promote only, never reduce.
    # New evidence (more assertions, new taxonomy hits) can reveal higher relevance;
    # it should never reduce a score that was set from richer prior evidence.
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
        if new_relevance is not None and (entity.space_relevance is None or new_relevance > entity.space_relevance):
            entity.space_relevance = new_relevance
            entity.save(update_fields=['space_relevance'])
            logger.info('classify_entity %s: space_relevance promoted to %s', entity_id, new_relevance)

            # Person entities skip WK summary so synthesise_entity_summary returns
            # early without ever queuing research.  Trigger it here on first
            # score promotion so space-relevant persons get their own research run.
            if entity.entity_type == 'person' and new_relevance >= 50:
                from core.models import Event, KnowledgeFragment
                has_data = (
                    Event.objects.filter(entity=entity).exists()
                    or KnowledgeFragment.objects.filter(entity=entity).exists()
                )
                if not has_data:
                    from ingest.tasks.research import research_topic
                    research_topic.apply_async(
                        args=[entity.canonical_name, 'person'],
                        kwargs={'cascade_depth': 0},
                        countdown=0,
                    )
                    logger.info('classify_entity %s: queued person research (no data, score=%d)', entity_id, new_relevance)
