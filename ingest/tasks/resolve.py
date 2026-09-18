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

_TRGM_THRESHOLD = 0.35
_TRGM_LLM_MIN = 0.20   # below this, skip LLM — too dissimilar to be worth the cost

_LLM_RESOLVE_SYSTEM = (
    'You are an entity resolver for a space-industry knowledge graph. '
    'Answer only with valid JSON.'
)
_LLM_RESOLVE_USER = """\
Is the mention "{mention}" referring to the same organisation as "{candidate}"?
Consider abbreviations, trading names, and common misspellings.
Reply: {{"same": true, "confidence": "high"|"medium"|"low"}} or {{"same": false}}"""


_VALID_ENTITY_TYPES = {
    'company', 'investor', 'entity',
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
        resolved = _llm_disambiguate(mention, rows)
        if resolved:
            logger.info('L4 LLM match: "%s" → %s', mention, resolved)
            return resolved

    # Fallback — stub entity queued for human review
    return _create_stub(mention, norm, document_id, entity_type)


def _llm_disambiguate(mention: str, candidates: list) -> str | None:
    """Ask the LLM whether any borderline trigram candidate matches the mention."""
    try:
        from ingest.ai import get_client
        from ingest.cost import log_call
        client = get_client()

        for entity_id, alias_norm, sim in candidates:
            resp = client.chat.completions.create(
                model=settings.AI_MODEL_FAST,  # binary yes/no question
                messages=[
                    {'role': 'system', 'content': _LLM_RESOLVE_SYSTEM},
                    {'role': 'user', 'content': _LLM_RESOLVE_USER.format(
                        mention=mention, candidate=alias_norm,
                    )},
                ],
                response_format={'type': 'json_object'},
                max_tokens=60,
                temperature=0,
            )
            log_call('resolve', settings.AI_MODEL_FAST, resp)
            raw = (resp.choices[0].message.content or '').strip()
            if not raw:
                continue
            # Some models return Python literals instead of JSON
            raw = raw.replace('True', 'true').replace('False', 'false').replace('None', 'null')
            result = json.loads(raw)
            if result.get('same') and result.get('confidence') in ('high', 'medium'):
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
            status='stub',
        )
        EntityAlias.objects.create(
            entity=entity,
            alias=mention,
            alias_norm=norm,
            alias_kind='trading',
            document_id=document_id,
        )

    logger.info('Stub created: "%s" → %s', mention, entity.id)
    return str(entity.id)
