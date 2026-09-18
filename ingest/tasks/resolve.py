"""Entity resolution — levels 1–5.

Level 1: external identifier match (skipped for free-text mentions in press releases)
Level 2: exact canonical name or alias_norm match
Level 3: pg_trgm similarity on alias_norm (threshold 0.82 auto-merge, 0.10 min for LLM)
Level 4: LLM disambiguation — compares mention against trigram candidates using knowledge context
Level 5: LLM world-knowledge canonical lookup — resolves acronyms and legal name variants
         that share no trigrams (e.g. "SpaceX" ↔ "Space Exploration Technologies Corp.")
Fallback: create a stub entity

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

# L5 prompt — world-knowledge canonical name lookup
_LLM_CANONICAL_USER = """\
What is the single most widely-used canonical name for the following organisation in the space industry?

Mention : "{mention}"  (type: {entity_type})

Rules:
- Reply with the name people and press most commonly use (e.g. "SpaceX" not "Space Exploration Technologies Corp.").
- If the mention IS already the canonical name, still return it.
- Only reply with high confidence if you are certain this is a real, known organisation.
- Do not invent organisations.

Reply: {{"canonical": "<name>", "confidence": "high"|"medium"|"low"}} or {{"canonical": null}} if unknown."""

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


# Normalized geographic terms that should never become stub entities.
# Countries, regions, and continents appear as claim *values*, not subjects.
_GEOGRAPHIC_BLOCKLIST = {
    # Continents
    'europe', 'north america', 'south america', 'asia', 'africa', 'oceania', 'antarctica',
    # Common countries (normalized — no suffixes)
    'united', 'united kingdom', 'united arab', 'united arab emirates', 'uae',
    'france', 'germany', 'spain', 'italy', 'japan', 'china', 'india', 'canada',
    'australia', 'brazil', 'russia', 'south korea', 'israel', 'norway', 'sweden',
    'netherlands', 'belgium', 'switzerland', 'austria', 'portugal', 'poland',
    'ukraine', 'turkey', 'saudi arabia', 'new zealand', 'singapore', 'luxembourg',
    # Regions / autonomous communities
    'catalonia', 'cataluna', 'scotland', 'wales', 'flanders', 'brittany',
    'bavaria', 'ile de france', 'lombardy', 'andalusia',
    # Common cities that slip through
    'london', 'paris', 'berlin', 'madrid', 'rome', 'tokyo', 'beijing', 'washington',
    'brussels', 'geneva', 'amsterdam', 'stockholm', 'oslo', 'helsinki',
    'toulouse', 'munich', 'barcelona', 'milan', 'cape canaveral', 'houston',
}

_VALID_ENTITY_TYPES = {
    'company', 'investor', 'entity', 'university',
    'facility', 'asset', 'person',
    'document_node', 'event', 'program',
    'geography',  # countries, regions, cities — relation targets only, never researched
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

    # Geographic blocklist — countries, regions, and cities get entity_type='geography'
    # so they can be relation targets (has_office_in → London) but are filtered from
    # company-focused views. They are never researched as companies.
    if norm in _GEOGRAPHIC_BLOCKLIST:
        logger.debug('resolve: geographic mention "%s" — finding/creating geography entity', mention)
        return _find_or_create_geography(mention, norm)

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

    # Level 5 — LLM world-knowledge canonical lookup
    # Handles cases where string similarity is useless (acronyms, legal name variants)
    canonical, found_id = _llm_known_entity(mention, entity_type)
    if found_id:
        _add_alias(found_id, mention, norm, document_id)
        logger.info('L5 match: "%s" → %s (canonical: %s)', mention, found_id, canonical)
        return found_id

    # Use LLM-suggested canonical as the stub name when provided (cleaner than raw mention)
    stub_mention = canonical if canonical else mention
    stub_norm = normalize_name(stub_mention) if canonical else norm
    entity_id = _create_stub(stub_mention, stub_norm, document_id, entity_type)
    if canonical and canonical != mention:
        _add_alias(entity_id, mention, norm, document_id)
    return entity_id


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
                facts.append(f'{a.attribute_id}={val}{unit}')
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


def _find_or_create_geography(mention: str, norm: str) -> str:
    """Find or create a geography entity (country, region, city).

    These are valid relation targets (has_office_in → London) but carry
    entity_type='geography' so they are excluded from company-focused views
    and never auto-researched as companies.
    """
    alias = (
        EntityAlias.objects
        .select_related('entity')
        .filter(alias_norm=norm, entity__entity_type='geography')
        .first()
    )
    if alias:
        return str(alias.entity_id)

    slug_base = slugify(mention)[:200] or 'geo'
    slug = slug_base
    n = 1
    while Entity.objects.filter(slug=slug).exists():
        slug = f'{slug_base}-{n}'
        n += 1

    with transaction.atomic():
        ent = Entity.objects.create(
            entity_type='geography',
            canonical_name=mention,
            slug=slug,
            status='active',
        )
        EntityAlias.objects.create(
            entity=ent,
            alias=mention,
            alias_norm=norm,
            alias_kind='trading',
        )
    logger.info('Geography entity created: "%s" → %s', mention, ent.id)
    return str(ent.id)


def _add_alias(entity_id: str, surface_form: str, norm: str, document_id: str | None) -> None:
    """Register surface_form as an alias for entity_id if not already present."""
    EntityAlias.objects.get_or_create(
        entity_id=entity_id,
        alias_norm=norm,
        defaults={
            'alias': surface_form,
            'alias_kind': 'abbrev',
            'document_id': document_id,
        },
    )


def _llm_known_entity(mention: str, entity_type: str) -> tuple[str | None, str | None]:
    """L5 — ask the LLM for the canonical name using world knowledge.

    Returns (canonical_name, entity_id):
    - entity_id set → canonical found in DB, merge into it
    - canonical_name set, entity_id None → use canonical as stub name
    - both None → LLM doesn't recognise the mention
    """
    try:
        from ingest.ai import get_client
        from ingest.cost import log_call
        client = get_client()

        resp = client.chat.completions.create(
            model=settings.AI_MODEL_FAST,
            messages=[
                {'role': 'system', 'content': _LLM_RESOLVE_SYSTEM},
                {'role': 'user', 'content': _LLM_CANONICAL_USER.format(
                    mention=mention, entity_type=entity_type,
                )},
            ],
            response_format={'type': 'json_object'},
            max_tokens=80,
            temperature=0,
        )
        log_call('resolve', settings.AI_MODEL_FAST, resp)
        raw = (resp.choices[0].message.content or '').strip()
        if not raw:
            return None, None
        raw = raw.replace('True', 'true').replace('False', 'false').replace('None', 'null')
        result = json.loads(raw)

        canonical = result.get('canonical')
        if not canonical or result.get('confidence') != 'high':
            return None, None

        # If canonical is the same as the mention, no new information
        if normalize_name(canonical) == normalize_name(mention):
            return None, None

        # Try to find it in the DB by canonical name
        ent = Entity.objects.filter(
            canonical_name__iexact=canonical,
            status__in=('active', 'stub'),
        ).first()
        if ent:
            return canonical, str(ent.id)

        # Try alias_norm
        alias = (
            EntityAlias.objects
            .select_related('entity')
            .filter(alias_norm=normalize_name(canonical), entity__status__in=('active', 'stub'))
            .first()
        )
        if alias:
            return canonical, str(alias.entity_id)

        # Canonical not in DB yet — return name only so stub uses the better form
        logger.info('L5 canonical "%s" not in DB for mention "%s" — will use as stub name', canonical, mention)
        return canonical, None

    except Exception as exc:
        logger.warning('L5 world-knowledge lookup failed for "%s": %s', mention, exc)
        return None, None


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
