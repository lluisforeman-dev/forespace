"""Retrospective entity deduplication sweep.

Finds candidate duplicate pairs via pg_trgm, confirms with LLM using both
entities' full knowledge context, and merges confirmed duplicates.

Safe to run repeatedly — already-merged pairs are skipped via EntityMerge log.
Use dry_run=True to preview without writing.
"""
import json
import logging

from celery import shared_task
from django.conf import settings
from django.db import connection, transaction

from core.normalize import normalize_name

logger = logging.getLogger(__name__)

_DEDUP_TRGM_MIN = 0.20    # minimum similarity to consider a pair (stricter than resolve)
_DEDUP_AUTO_MERGE = 0.92  # above this: auto-merge without LLM (virtually identical)

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


def execute_merge(kept_id: str, merged_id: str, rationale: dict, performed_by: str = 'auto:dedup_sweep') -> None:
    """Merge entity `merged_id` into `kept_id`.

    Transfers all data to the kept entity, marks merged as status='merged',
    sets redirects_to, and writes an EntityMerge audit record.
    """
    from core.models import (
        Assertion, Entity, EntityAlias, EntityMerge, EntitySummary,
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
            if ev.entity_id != kept.id:  # don't add as participant of own event
                ev.participants.add(kept)

        # Delete duplicate summary (kept entity's summary takes precedence)
        EntitySummary.objects.filter(entity=merged).delete()

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
def dedup_sweep(self, min_similarity: float = _DEDUP_TRGM_MIN, dry_run: bool = False):
    """Find and merge duplicate entities in the DB.

    Queries entity_alias for pairs with pg_trgm similarity above min_similarity,
    skips already-merged or already-decided pairs, confirms with LLM, merges.
    """
    from core.models import Assertion, Entity, EntityMerge
    from ingest.ai import get_client
    from ingest.cost import log_call

    # Pairs already processed — skip both directions
    decided_pairs = set()
    for m_id, k_id in EntityMerge.objects.values_list('merged_id', 'kept_id'):
        decided_pairs.add((str(m_id), str(k_id)))
        decided_pairs.add((str(k_id), str(m_id)))

    # IDs of already-merged entities — skip entirely
    merged_ids = set(
        str(i) for i in
        Entity.objects.filter(status='merged').values_list('id', flat=True)
    )

    # Find candidate pairs via trigram similarity across alias_norm
    with connection.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT
                LEAST(a.entity_id::text, b.entity_id::text)  AS id_a,
                GREATEST(a.entity_id::text, b.entity_id::text) AS id_b,
                MAX(similarity(a.alias_norm, b.alias_norm))   AS sim
            FROM entity_alias a
            JOIN entity_alias b
              ON a.entity_id != b.entity_id
             AND similarity(a.alias_norm, b.alias_norm) > %s
            GROUP BY 1, 2
            ORDER BY sim DESC
            LIMIT 500
        """, [min_similarity])
        pairs = cur.fetchall()

    logger.info('dedup_sweep: %d candidate pairs (min_sim=%.2f)', len(pairs), min_similarity)

    client = get_client()
    merged_count = skipped_count = 0

    for id_a, id_b, sim in pairs:
        # Skip already-merged or already-decided
        if id_a in merged_ids or id_b in merged_ids:
            skipped_count += 1
            continue
        if (id_a, id_b) in decided_pairs or (id_b, id_a) in decided_pairs:
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
                logger.warning('dedup_sweep LLM error %s/%s: %s', id_a, id_b, exc)
                skipped_count += 1
                continue

        if not do_merge:
            # Mark as decided-different so next sweep skips this pair
            decided_pairs.add((id_a, id_b))
            skipped_count += 1
            continue

        # Keep the entity with more accepted assertions; ties → older entity
        count_a = Assertion.objects.filter(entity_id=id_a, status='accepted').count()
        count_b = Assertion.objects.filter(entity_id=id_b, status='accepted').count()
        if count_a >= count_b:
            kept_id, merged_id = id_a, id_b
        else:
            kept_id, merged_id = id_b, id_a

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
                execute_merge(kept_id, merged_id, rationale)
            except Exception as exc:
                logger.error('dedup_sweep merge error %s/%s: %s', kept_id, merged_id, exc)
                skipped_count += 1
                continue

        merged_ids.add(merged_id)  # mark so subsequent pairs skip this id
        merged_count += 1

    logger.info('dedup_sweep done: merged=%d skipped=%d dry_run=%s', merged_count, skipped_count, dry_run)
    return {'merged': merged_count, 'skipped': skipped_count}
