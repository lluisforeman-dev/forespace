"""Entity resolution — levels 1–4.

Level 1: external identifier match (skipped for free-text mentions in press releases)
Level 2: exact canonical name or alias_norm match
Level 3: pg_trgm similarity on alias_norm (threshold 0.35)
Level 4: LLM disambiguation for borderline trigram candidates (0.2–0.35 similarity)
Fallback: create a stub entity for human review

Always returns an entity_id string — never silently drops an unresolved mention.
"""
import json
import logging

from django.conf import settings
from django.db import connection, transaction
from django.utils.text import slugify

from core.models import Entity, EntityAlias
from core.normalize import normalize_name

logger = logging.getLogger(__name__)

_TRGM_THRESHOLD = 0.82  # above this: auto-merge (virtually identical names)
_TRGM_LLM_MIN = 0.10    # below this: too dissimilar to bother — create new entity

_LLM_RESOLVE_SYSTEM = (
    'You are an entity resolver for a space-industry knowledge graph. '
    'Answer only with valid JSON.'
)

# Fallback prompt used when the candidate entity has no knowledge data yet
_LLM_RESOLVE_USER_STRINGS_ONLY = """\
Is the mention "{mention}" (type: {mention_type}) the same real-world organisation as "{candidate}"?

Rules:
- SAME if one is an abbreviation or short form of the other.
- SAME if the only difference is a legal/org suffix (Foundation, Institute, Corp, Centre).
- SAME if one is a local-language form (Fundació = Foundation in Catalan).
- DIFFERENT if a meaningful prefix/qualifier is present in one but not the other \
("6G StarLab" ≠ "StarLab", "NASA JPL" ≠ "NASA").
- DIFFERENT if related but legally distinct.
- When uncertain reply false — a missed merge is safer than a wrong merge.

Reply: {{"same": true, "confidence": "high"|"medium"|"low"}} or {{"same": false}}"""

# Rich prompt used when we have knowledge data for the candidate
_LLM_RESOLVE_USER_WITH_CONTEXT = """\
Decide whether the new mention refers to the same real-world entity as the candidate in our database.

New mention : "{mention}"  (type: {mention_type})

Candidate   : "{candidate_name}"
{context}

Rules:
- SAME if the mention is an abbreviation, short form, or local-language name for the candidate \
(e.g. "JPL" = "Jet Propulsion Laboratory", "ESA" = "European Space Agency").
- SAME if the only difference is a legal/org suffix (Foundation, Ltd, Centre, Agency).
- DIFFERENT if a meaningful qualifier distinguishes them \
("6G StarLab" ≠ "StarLab", "NASA JPL" ≠ "NASA", "Airbus DS" ≠ "Airbus").
- DIFFERENT if related but legally separate organisations.
- Use the knowledge context above — matching headquarters, relations, or descriptions \
are strong evidence of being the same entity.
- When uncertain reply false.

Reply: {{"same": true, "confidence": "high"|"medium"|"low"}} or {{"same": false}}"""


_VALID_ENTITY_TYPES = {
    'company', 'investor', 'entity', 'university',
    'facility', 'asset', 'person',
    'document_node', 'event', 'program',
}


def resolve_mention(
    mention: str,
    document_id: str | None = None,
    entity_type: str = 'company',
) -> str:
    """Resolve a surface-form mention to an entity UUID. Creates a stub if needed."""
    if entity_type not in _VALID_ENTITY_TYPES:
        entity_type = 'company'
    norm = normalize_name(mention)

    # Level 2a — exact canonical name (case-insensitive)
    ent = (
        Entity.objects
        .filter(canonical_name__iexact=mention, status__in=('active', 'stub'))
        .first()
    )
    if ent:
        return str(ent.id)

    # Level 2b — exact alias_norm match
    alias = (
        EntityAlias.objects
        .select_related('entity')
        .filter(alias_norm=norm, entity__status__in=('active', 'stub'))
        .first()
    )
    if alias:
        return str(alias.entity_id)

    # Level 3 — trigram similarity via pg_trgm
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT entity_id, alias_norm, similarity(alias_norm, %s) AS sim
            FROM entity_alias
            WHERE similarity(alias_norm, %s) > %s
            ORDER BY sim DESC
            LIMIT 3
            """,
            [norm, norm, _TRGM_LLM_MIN],
        )
        rows = cur.fetchall()

    if rows:
        best_entity_id, best_alias_norm, best_sim = rows[0]
        if best_sim >= _TRGM_THRESHOLD:
            logger.info('L3 match: "%s" → %s (sim=%.2f)', mention, best_entity_id, best_sim)
            return str(best_entity_id)

        # Level 4 — LLM disambiguation for borderline candidates
        resolved = _llm_disambiguate(mention, entity_type, rows)
        if resolved:
            logger.info('L4 LLM match: "%s" → %s', mention, resolved)
            return resolved

    # Fallback — stub entity queued for human review
    return _create_stub(mention, norm, document_id, entity_type)


def _entity_context_for_resolution(entity_id: str) -> str:
    """Pull a compact knowledge block for a candidate entity to pass to the LLM."""
    from core.models import Assertion, KnowledgeFragment, Relation
    lines = []
    try:
        entity = Entity.objects.only('entity_type', 'canonical_name').get(pk=entity_id)
        lines.append(f'Type: {entity.entity_type}')
    except Entity.DoesNotExist:
        return ''
    assertions = (
        Assertion.objects
        .filter(entity_id=entity_id, status='accepted', superseded_at__isnull=True)
        .order_by('-confidence')[:6]
    )
    if assertions:
        facts = []
        for a in assertions:
            val = a.value_text or a.value_num or a.value_date or a.value_bool
            unit = f' {a.unit}' if getattr(a, 'unit', None) else ''
            if val is not None:
                facts.append(f'{a.attribute_key}={val}{unit}')
        if facts:
            lines.append('Facts: ' + ', '.join(facts))
    frag = (
        KnowledgeFragment.objects
        .filter(entity_id=entity_id)
        .order_by('-confidence')
        .first()
    )
    if frag:
        lines.append(f'Description: {frag.text[:300]}')
    rels = (
        Relation.objects
        .filter(subject_id=entity_id, superseded_at__isnull=True)
        .select_related('object')[:3]
    )
    if rels:
        lines.append('Relations: ' + ', '.join(
            f'{r.predicate_id} → {r.object.canonical_name}' for r in rels
        ))
    return '\n'.join(lines)


def _llm_disambiguate(mention: str, mention_type: str, candidates: list) -> str | None:
    """Ask the LLM whether any borderline trigram candidate matches the mention.

    For each candidate, pulls knowledge context from the DB and passes it to the
    LLM so it can compare facts, not just strings.
    """
    try:
        from ingest.ai import get_client
        from ingest.cost import log_call
        client = get_client()

        for entity_id, alias_norm, sim in candidates:
            candidate_entity = Entity.objects.filter(pk=entity_id).only('canonical_name').first()
            candidate_name = candidate_entity.canonical_name if candidate_entity else alias_norm

            context = _entity_context_for_resolution(str(entity_id))
            if context:
                user_msg = _LLM_RESOLVE_USER_WITH_CONTEXT.format(
                    mention=mention,
                    mention_type=mention_type,
                    candidate_name=candidate_name,
                    context=context,
                )
            else:
                user_msg = _LLM_RESOLVE_USER_STRINGS_ONLY.format(
                    mention=mention,
                    mention_type=mention_type,
                    candidate=candidate_name,
                )

            resp = client.chat.completions.create(
                model=settings.AI_MODEL_FAST,
                messages=[
                    {'role': 'system', 'content': _LLM_RESOLVE_SYSTEM},
                    {'role': 'user', 'content': user_msg},
                ],
                response_format={'type': 'json_object'},
                max_tokens=60,
                temperature=0,
            )
            log_call('resolve', settings.AI_MODEL_FAST, resp)
            raw = (resp.choices[0].message.content or '').strip()
            if not raw:
                continue
            raw = raw.replace('True', 'true').replace('False', 'false').replace('None', 'null')
            result = json.loads(raw)
            if result.get('same') and result.get('confidence') in ('high', 'medium'):
                logger.info(
                    'L4 LLM matched "%s" → %s (%s, sim=%.2f, ctx=%s)',
                    mention, entity_id, candidate_name, sim, bool(context),
                )
                return str(entity_id)
    except Exception as exc:
        logger.warning('L4 LLM resolve failed for "%s": %s', mention, exc)
    return None


def _create_stub(
    mention: str,
    norm: str,
    document_id: str | None,
    entity_type: str = 'company',
) -> str:
    """Create a stub entity for an unresolved mention."""
    slug_base = slugify(mention)[:200] or 'entity'
    slug = slug_base
    n = 1
    while Entity.objects.filter(slug=slug).exists():
        slug = f'{slug_base}-{n}'
        n += 1

    with transaction.atomic():
        entity = Entity.objects.create(
            entity_type=entity_type,
            canonical_name=mention,
            slug=slug,
            status='active',
        )
        EntityAlias.objects.create(
            entity=entity,
            alias=mention,
            alias_norm=norm,
            alias_kind='trading',
            document_id=document_id,
        )

    logger.info('Entity created: "%s" → %s', mention, entity.id)
    return str(entity.id)
