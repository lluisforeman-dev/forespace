"""Entity resolution — levels 1–3 (Phase 2 scope).

Level 1: external identifier match (skipped for free-text mentions in press releases)
Level 2: exact canonical name or alias_norm match
Level 3: pg_trgm similarity on alias_norm (threshold 0.35)
Fallback: create a stub entity for human review

Always returns an entity_id string — never silently drops an unresolved mention.
"""
import logging

from django.db import connection, transaction
from django.utils.text import slugify

from core.models import Entity, EntityAlias
from core.normalize import normalize_name

logger = logging.getLogger(__name__)

_TRGM_THRESHOLD = 0.35


def resolve_mention(mention: str, document_id: str | None = None) -> str:
    """Resolve a surface-form mention to an entity UUID. Creates a stub if needed."""
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
            SELECT entity_id, similarity(alias_norm, %s) AS sim
            FROM entity_alias
            WHERE similarity(alias_norm, %s) > %s
            ORDER BY sim DESC
            LIMIT 1
            """,
            [norm, norm, _TRGM_THRESHOLD],
        )
        row = cur.fetchone()

    if row:
        entity_id, sim = row
        logger.info('L3 match: "%s" → %s (sim=%.2f)', mention, entity_id, sim)
        return str(entity_id)

    # Fallback — stub entity queued for human review
    return _create_stub(mention, norm, document_id)


def _create_stub(mention: str, norm: str, document_id: str | None) -> str:
    """Create a stub entity for an unresolved mention."""
    slug_base = slugify(mention)[:200] or 'entity'
    slug = slug_base
    n = 1
    while Entity.objects.filter(slug=slug).exists():
        slug = f'{slug_base}-{n}'
        n += 1

    with transaction.atomic():
        entity = Entity.objects.create(
            entity_type='organization',
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
