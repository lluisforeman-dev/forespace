"""Retrospective entity deduplication sweep.

Finds candidate duplicate pairs via pg_trgm, confirms with LLM using both
entities' full knowledge context, and merges confirmed duplicates.

Key scaling properties:
- EntityNonMerge table persists "decided different" pairs permanently — LLM cost
  per sweep approaches zero as the graph stabilises (only new pairs ever get called).
- dedup_entities() does targeted dedup for a specific set of entity IDs, used
  automatically after each research run — much cheaper than a full sweep.
- Type filtering: persons only compare against persons; organisations against organisations.
- LLM only called for trigram sim >= 0.35 (below that: almost certainly different entities).

Safe to run repeatedly — already-merged and already-decided pairs are always skipped.
Use dry_run=True to preview without writing.
"""
import logging

import json

from celery import shared_task
from django.conf import settings
from django.db import connection, transaction

from core.normalize import normalize_name

logger = logging.getLogger(__name__)

_DEDUP_TRGM_MIN = 0.25        # minimum similarity to consider a pair
_DEDUP_LLM_MIN = 0.35         # below this: skip LLM, treat as different (saves tokens)
_DEDUP_AUTO_MERGE = 0.92      # above this: auto-merge without LLM (virtually identical)

# Entity types that are comparable for deduplication.
# Only compare within the same compatibility group.
_TYPE_GROUPS = {
    'org': {'company', 'entity', 'investor', 'university', 'end_user', 'facility'},
    'person': {'person'},
    'asset': {'asset'},
    'program': {'program', 'funding_program'},
}
_TYPE_TO_GROUP = {t: g for g, types in _TYPE_GROUPS.items() for t in types}

_DEDUP_SYSTEM = (
    'You are an entity deduplication expert for a space-industry knowledge graph. '
    'Answer only with valid JSON.'
)

_DEDUP_USER = """\
Are these two entries in our database the same real-world organisation?

Entity A: "{name_a}"
{context_a}

Entity B: "{name_b}"
{context_b}

Rules:
- SAME if one is a short form, acronym, or language variant of the other.
- SAME if the only difference is a legal suffix (SL, Ltd, GmbH, Agency, Foundation).
- DIFFERENT if a meaningful qualifier distinguishes them ("6G StarLab" ≠ "StarLab").
- DIFFERENT if related but legally separate organisations.
- Use all context above — matching facts, locations, or relations are strong evidence.
- When uncertain reply false — a missed merge is safer than a wrong merge.

Reply: {{"same": true, "confidence": "high"|"medium"|"low"}} or {{"same": false}}"""


def _entity_context_for_dedup(entity_id: str) -> str:
    """Rich context block for dedup LLM — uses summary + facts + relations."""
    from core.models import Assertion, Entity, EntitySummary, Relation

    try:
        entity = Entity.objects.get(pk=entity_id)
    except Entity.DoesNotExist:
        return ''

    lines = [f'Type: {entity.entity_type}']

    summary = EntitySummary.objects.filter(entity=entity).first()
    if summary and summary.overview:
        lines.append(f'Overview: {summary.overview[:300]}')

    assertions = (
        Assertion.objects
        .filter(entity=entity, status='accepted')
        .order_by('-confidence')[:8]
    )
    facts = []
    for a in assertions:
        val = a.value_text or a.value_num or a.value_date or a.value_bool
        if val is not None:
            facts.append(f'{a.attribute_id}={val}')
    if facts:
        lines.append('Facts: ' + ', '.join(facts))

    rels = (
        Relation.objects
        .filter(subject=entity, superseded_at__isnull=True)
        .select_related('object')[:5]
    )
    if rels:
        lines.append('Relations: ' + ', '.join(
            f'{r.predicate_id}→{r.object.canonical_name}' for r in rels
        ))

    return '\n'.join(lines)


def _load_decided_pairs() -> set:
    """Load all already-decided pairs (both merges and non-merges) from DB.

    Returns a set of (min_id, max_id) tuples for O(1) lookup.
    """
    from core.models import Entity, EntityMerge, EntityNonMerge

    decided = set()

    for m_id, k_id in EntityMerge.objects.values_list('merged_id', 'kept_id'):
        a, b = str(m_id), str(k_id)
        decided.add((min(a, b), max(a, b)))

    for a_id, b_id in EntityNonMerge.objects.values_list('entity_a_id', 'entity_b_id'):
        a, b = str(a_id), str(b_id)
        decided.add((min(a, b), max(a, b)))

    return decided


def _persist_non_merge(id_a: str, id_b: str, method: str = 'auto:dedup_sweep') -> None:
    """Save a confirmed-different pair to EntityNonMerge (normalised order)."""
    from core.models import EntityNonMerge
    a, b = (min(id_a, id_b), max(id_a, id_b))
    EntityNonMerge.objects.get_or_create(
        entity_a_id=a,
        entity_b_id=b,
        defaults={'method': method},
    )


def _process_pairs(pairs, decided_pairs, merged_ids, entity_type_map,
                   client, dry_run, method='auto:dedup_sweep'):
    """Core dedup logic shared by full sweep and targeted dedup.

    Returns (merged_count, skipped_count) and mutates decided_pairs / merged_ids in place.
    """
    from core.models import Assertion, Entity
    from ingest.cost import log_call

    merged_count = skipped_count = 0

    for id_a, id_b, sim in pairs:
        pair_key = (min(id_a, id_b), max(id_a, id_b))

        if id_a in merged_ids or id_b in merged_ids:
            skipped_count += 1
            continue
        if pair_key in decided_pairs:
            skipped_count += 1
            continue

        # Type compatibility check — don't compare persons against companies etc.
        type_a = entity_type_map.get(id_a)
        type_b = entity_type_map.get(id_b)
        if type_a and type_b and _TYPE_TO_GROUP.get(type_a) != _TYPE_TO_GROUP.get(type_b):
            decided_pairs.add(pair_key)
            skipped_count += 1
            continue

        try:
            entity_a = Entity.objects.get(pk=id_a)
            entity_b = Entity.objects.get(pk=id_b)
        except Entity.DoesNotExist:
            skipped_count += 1
            continue

        # Auto-merge for near-identical names
        if sim >= _DEDUP_AUTO_MERGE:
            do_merge = True
            confidence = 'high'
            reason = f'trigram_auto sim={sim:.2f}'
        elif sim < _DEDUP_LLM_MIN:
            # Below LLM threshold — treat as different without calling LLM
            decided_pairs.add(pair_key)
            _persist_non_merge(id_a, id_b, method)
            skipped_count += 1
            continue
        else:
            context_a = _entity_context_for_dedup(id_a)
            context_b = _entity_context_for_dedup(id_b)
            try:
                resp = client.chat.completions.create(
                    model=settings.AI_MODEL_FAST,
                    messages=[
                        {'role': 'system', 'content': _DEDUP_SYSTEM},
                        {'role': 'user', 'content': _DEDUP_USER.format(
                            name_a=entity_a.canonical_name,
                            context_a=context_a or '(no data yet)',
                            name_b=entity_b.canonical_name,
                            context_b=context_b or '(no data yet)',
                        )},
                    ],
                    response_format={'type': 'json_object'},
                    max_tokens=60,
                    temperature=0,
                )
                log_call('dedup', settings.AI_MODEL_FAST, resp)
                raw = (resp.choices[0].message.content or '').strip()
                if not raw:
                    skipped_count += 1
                    continue
                result = json.loads(raw.replace('True', 'true').replace('False', 'false'))
                do_merge = bool(result.get('same')) and result.get('confidence') in ('high', 'medium')
                confidence = result.get('confidence', 'low')
                reason = f'llm sim={sim:.2f} conf={confidence}'
            except Exception as exc:
                logger.warning('dedup LLM error %s/%s: %s', id_a, id_b, exc)
                skipped_count += 1
                continue

        if not do_merge:
            decided_pairs.add(pair_key)
            _persist_non_merge(id_a, id_b, method)
            skipped_count += 1
            continue

        # Keep the entity with more accepted assertions; ties → older entity
        count_a = Assertion.objects.filter(entity_id=id_a, status='accepted').count()
        count_b = Assertion.objects.filter(entity_id=id_b, status='accepted').count()
        kept_id = id_a if count_a >= count_b else id_b
        merged_id = id_b if kept_id == id_a else id_a

        rationale = {
            'reason': reason,
            'similarity': round(float(sim), 3),
            'confidence': confidence,
            'kept_assertions': max(count_a, count_b),
            'merged_assertions': min(count_a, count_b),
        }

        if dry_run:
            kept_name = entity_a.canonical_name if kept_id == id_a else entity_b.canonical_name
            merged_name = entity_b.canonical_name if kept_id == id_a else entity_a.canonical_name
            logger.info('[DRY RUN] would merge "%s" → "%s" (%s)', merged_name, kept_name, reason)
        else:
            try:
                execute_merge(kept_id, merged_id, rationale, performed_by=method)
            except Exception as exc:
                logger.error('dedup merge error %s/%s: %s', kept_id, merged_id, exc)
                skipped_count += 1
                continue

        decided_pairs.add(pair_key)
        merged_ids.add(merged_id)
        merged_count += 1

    return merged_count, skipped_count


def execute_merge(kept_id: str, merged_id: str, rationale: dict, performed_by: str = 'auto:dedup_sweep') -> None:
    """Merge entity `merged_id` into `kept_id`.

    Transfers all data to the kept entity, marks merged as status='merged',
    sets redirects_to, and writes an EntityMerge audit record.
    """
    from core.models import (
        Assertion, Entity, EntityAlias, EntityMerge, EntityNonMerge, EntitySummary,
        Event, KnowledgeFragment, Relation,
    )
    from ingest.tasks.resolve import _add_alias

    with transaction.atomic():
        kept = Entity.objects.select_for_update().get(pk=kept_id)
        merged = Entity.objects.select_for_update().get(pk=merged_id)

        # Transfer aliases
        EntityAlias.objects.filter(entity=merged).update(entity=kept)

        # Register merged entity's canonical name as alias on kept
        _add_alias(kept_id, merged.canonical_name, normalize_name(merged.canonical_name), None)

        # Transfer assertions, events, fragments
        Assertion.objects.filter(entity=merged).update(entity=kept)
        Event.objects.filter(entity=merged).update(entity=kept)
        KnowledgeFragment.objects.filter(entity=merged).update(entity=kept)

        # Transfer relations (both directions)
        Relation.objects.filter(subject=merged).update(subject=kept)
        Relation.objects.filter(object=merged).update(object=kept)
        # Remove self-relations created by the transfer
        Relation.objects.filter(subject=kept, object=kept).delete()

        # Transfer event participants (M2M)
        for ev in Event.objects.filter(participants=merged):
            ev.participants.remove(merged)
            if ev.entity_id != kept.id:
                ev.participants.add(kept)

        # Delete duplicate summary (kept entity's summary takes precedence)
        EntitySummary.objects.filter(entity=merged).delete()

        # Remove any non-merge records involving either entity — they're now the same
        a, b = (min(kept_id, merged_id), max(kept_id, merged_id))
        EntityNonMerge.objects.filter(entity_a_id=a, entity_b_id=b).delete()

        # Mark merged entity
        merged.status = 'merged'
        merged.redirects_to = kept
        merged.save(update_fields=['status', 'redirects_to'])

        # Audit log
        EntityMerge.objects.create(
            kept=kept,
            merged=merged,
            performed_by=performed_by,
            rationale=rationale,
        )

    logger.info('Merged "%s" → "%s" (%s)', merged.canonical_name, kept.canonical_name, rationale.get('reason'))


@shared_task(bind=True, queue='extract', max_retries=0)
def dedup_entities(self, entity_ids: list, dry_run: bool = False):
    """Targeted dedup: check a specific set of entities against the whole DB.

    Called automatically after each research_topic run with the set of touched
    entity IDs. Much cheaper than a full sweep — only checks pairs where at
    least one entity is in the provided set.
    """
    if not entity_ids:
        return {'merged': 0, 'skipped': 0}

    from core.models import Entity
    from ingest.ai import get_client

    decided_pairs = _load_decided_pairs()
    merged_ids = set(
        str(i) for i in
        Entity.objects.filter(status='merged').values_list('id', flat=True)
    )
    entity_type_map = dict(
        Entity.objects.filter(status='active')
        .values_list('id', 'entity_type')
    )
    entity_type_map = {str(k): v for k, v in entity_type_map.items()}

    # Find pairs involving at least one of the target entities
    with connection.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT
                LEAST(a.entity_id::text, b.entity_id::text)   AS id_a,
                GREATEST(a.entity_id::text, b.entity_id::text) AS id_b,
                MAX(similarity(a.alias_norm, b.alias_norm))    AS sim
            FROM entity_alias a
            JOIN entity_alias b
              ON a.entity_id != b.entity_id
             AND similarity(a.alias_norm, b.alias_norm) > %s
            WHERE a.entity_id::text = ANY(%s)
            GROUP BY 1, 2
            ORDER BY sim DESC
            LIMIT 300
        """, [_DEDUP_TRGM_MIN, entity_ids])
        pairs = cur.fetchall()

    logger.info('dedup_entities: %d candidate pairs for %d entities', len(pairs), len(entity_ids))
    client = get_client()
    merged, skipped = _process_pairs(
        pairs, decided_pairs, merged_ids, entity_type_map, client, dry_run,
        method='auto:research_cascade',
    )
    logger.info('dedup_entities done: merged=%d skipped=%d dry_run=%s', merged, skipped, dry_run)
    return {'merged': merged, 'skipped': skipped}


@shared_task(bind=True, queue='extract', max_retries=0)
def dedup_sweep(self, min_similarity: float = _DEDUP_TRGM_MIN, dry_run: bool = False):
    """Full retrospective dedup sweep across the entire entity graph.

    Queries entity_alias for pairs with pg_trgm similarity above min_similarity.
    Skips all previously decided pairs (EntityMerge + EntityNonMerge) — LLM cost
    per sweep is bounded by the number of NEW pairs only, not the total graph size.
    """
    from core.models import Entity
    from ingest.ai import get_client

    decided_pairs = _load_decided_pairs()
    merged_ids = set(
        str(i) for i in
        Entity.objects.filter(status='merged').values_list('id', flat=True)
    )
    entity_type_map = {
        str(k): v for k, v in
        Entity.objects.filter(status='active').values_list('id', 'entity_type')
    }

    with connection.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT
                LEAST(a.entity_id::text, b.entity_id::text)   AS id_a,
                GREATEST(a.entity_id::text, b.entity_id::text) AS id_b,
                MAX(similarity(a.alias_norm, b.alias_norm))    AS sim
            FROM entity_alias a
            JOIN entity_alias b
              ON a.entity_id != b.entity_id
             AND similarity(a.alias_norm, b.alias_norm) > %s
            GROUP BY 1, 2
            ORDER BY sim DESC
            LIMIT 2000
        """, [min_similarity])
        pairs = cur.fetchall()

    logger.info('dedup_sweep: %d candidate pairs (min_sim=%.2f)', len(pairs), min_similarity)
    client = get_client()
    merged, skipped = _process_pairs(
        pairs, decided_pairs, merged_ids, entity_type_map, client, dry_run,
    )
    logger.info('dedup_sweep done: merged=%d skipped=%d dry_run=%s', merged, skipped, dry_run)
    return {'merged': merged, 'skipped': skipped}
