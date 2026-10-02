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
from django.db.models import Q
from django.utils.text import slugify

from core.models import Entity, EntityAlias
from core.normalize import normalize_name
from ingest.prompts import get_prompt

logger = logging.getLogger(__name__)

_TRGM_LLM_MIN = 0.10    # below this: too dissimilar to bother — create new entity

_LLM_RESOLVE_SYSTEM = (
    'You are an entity resolver for a space-industry knowledge graph. '
    'Answer only with valid JSON.'
)

# L5 prompt — world-knowledge canonical name lookup (organisations)
_LLM_CANONICAL_USER = """\
What is the single most widely-used canonical name for the following ORGANISATION in the space industry?
This must be an organisation (company, agency, institute, consortium) — not a person, country, or product.

Mention : "{mention}"  (type: {entity_type})

Rules:
- Reply with the name people and press most commonly use (e.g. "SpaceX" not "Space Exploration Technologies Corp.").
- If the mention IS already the canonical name, still return it.
- Only reply with high confidence if you are certain this is a real, known ORGANISATION.
- If the mention is a person's name, product name, or geographic area, reply {{"canonical": null}}.
- Do not invent organisations.

Reply: {{"canonical": "<name>", "confidence": "high"|"medium"|"low"}} or {{"canonical": null}} if unknown."""

# L5 prompt — world-knowledge canonical name lookup (persons)
_LLM_CANONICAL_PERSON = """\
What is the full, canonical name for the following person in the space industry?

Mention : "{mention}"

Rules:
- Reply with the most complete, commonly used form of their name \
(e.g. "Roger Jové-Casulleras" not "Roger Jove", "José María" not "Jose Maria").
- Include compound or hyphenated surnames where applicable.
- If the mention is already the canonical form, return it as-is.
- Only reply with high confidence if you are certain this is a real, known person.
- Do not invent people.

Reply: {{"canonical": "<full name>", "confidence": "high"|"medium"|"low"}} or {{"canonical": null}} if unknown."""

# L5-verify prompt — confirm a world-knowledge name match against the actual
# graph entity. Names collide across domains: a mention may canonically be
# called "Starship" (delivery robot) while the graph's "Starship" is SpaceX's
# launch vehicle. The canonical-name lookup alone cannot see this.
_LLM_VERIFY_USER = """\
A document mentions "{mention}" (type: {entity_type}).
Document context: {subject_context}

Our knowledge graph already contains this entity:
Name   : "{candidate_name}" (type: {candidate_type})
{candidate_context}

Is the mention referring to THIS SAME real-world entity?
A shared or similar name alone is NOT enough — names collide across domains.
Judge by what each entity IS and DOES (sector, domain, described purpose).
NOTE: type labels are noisy — if the name matches and the document context
does not clearly point to a different real-world thing, it is the same entity.

Reply: {{"match": true|false, "confidence": "high"|"medium"|"low"}}"""

# Normalized geographic terms that should never become stub entities.
# Countries, regions, and continents appear as claim *values*, not subjects.
_GEOGRAPHIC_BLOCKLIST = {
    # Continents
    'europe', 'north america', 'south america', 'asia', 'africa', 'oceania', 'antarctica',
    # Common countries (normalized — no suffixes)
    'united', 'united kingdom', 'united states', 'united arab', 'united arab emirates', 'uae',
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
    'new york', 'los angeles', 'san francisco', 'seattle', 'boston', 'denver',
    'austin', 'chicago', 'dallas', 'miami', 'toronto', 'montreal', 'vancouver',
    'dubai', 'abu dhabi', 'tel aviv', 'singapore', 'sydney', 'melbourne',
    'bangalore', 'mumbai', 'new delhi', 'shanghai', 'seoul', 'osaka',
    'vienna', 'zurich', 'warsaw', 'prague', 'lisbon', 'copenhagen', 'helsinki',
    'kyiv', 'istanbul', 'athens', 'bucharest', 'budapest', 'sofia',
}

_VALID_ENTITY_TYPES = {
    'company', 'investor', 'entity', 'university',
    'facility', 'asset', 'person',
    'document_node', 'event', 'program',
    'funding_program',  # deployable funding instruments — grants, VC funds, loan programmes
    'end_user',         # downstream consumers of space services (not space companies themselves)
    'geography',        # countries, regions, cities — relation targets only, never researched
}

# Legal company suffixes that indicate a specific registered entity the LLM is unlikely to know.
# Subset of core.normalize._SUFFIXES — org-type words excluded intentionally
# (we DO want L5 for "European Space Agency" even though "agency" is a suffix).
_L5_LEGAL_SUFFIXES = {
    'inc', 'llc', 'ltd', 'corp', 'gmbh', 'sa', 'sas', 'bv', 'ag', 'plc',
    'sl', 'slu', 'spa', 'nv', 'oy', 'ab', 'as', 'aps',
}


def _should_call_l5(mention: str) -> bool:
    """Return True if L5 (world-knowledge LLM lookup) is worth attempting.

    L5 is skipped when the mention is clearly a specific registered legal entity
    that the LLM is unlikely to know — identified by a legal company suffix.
    L5 is always attempted for acronyms (short) and non-ASCII names (language variants)
    because those are exactly the cases L5 is designed for.
    """
    # Short mention → likely acronym (ESA, IEEC, GMV, JPL) — L5 excels here
    stripped = mention.replace(' ', '').replace('.', '')
    if len(stripped) <= 6:
        return True
    # Non-ASCII → language variant ("Institut d'Estudis…") — L5 provides English canonical
    if not stripped.isascii():
        return True
    # Ends with a legal company suffix → specific registered entity, LLM unlikely to know it
    last_token = mention.strip().split()[-1].lower().rstrip('.')
    if last_token in _L5_LEGAL_SUFFIXES:
        return False
    return True


def resolve_mention(
    mention: str,
    document_id: str | None = None,
    entity_type: str = 'company',
    subject_context: str = '',
    primary_entity_id: str | None = None,
    primary_entity_norm: str | None = None,
) -> str:
    """Resolve a surface-form mention to an entity UUID. Creates a stub if needed.

    subject_context: optional hint about the document's primary subject, e.g.
        "Researching: SpaceX (company)" — passed to L4 LLM prompts to help
        disambiguate mentions that share an acronym or name across domains.
    primary_entity_id / primary_entity_norm: when set, a mention whose normalised form
        contains the primary entity's normalised name as a contiguous substring is
        resolved directly to the primary entity (e.g. "NASA's SpaceX Crew-15 mission"
        → "Crew-15" when researching Crew-15).  Only applied when the primary name is
        at least 4 chars and the mention is at most 6 words longer than the primary name.
    """
    if entity_type not in _VALID_ENTITY_TYPES:
        entity_type = 'company'
    norm = normalize_name(mention)

    # Explicit geography type — always route to geography handler, no stub creation.
    if entity_type == 'geography':
        return _find_or_create_geography(mention, norm)

    # Geographic blocklist — countries, regions, and cities get entity_type='geography'
    # so they can be relation targets (has_office_in → London) but are filtered from
    # company-focused views. They are never researched as companies.
    if norm in _GEOGRAPHIC_BLOCKLIST:
        logger.debug('resolve: geographic mention "%s" — finding/creating geography entity', mention)
        return _find_or_create_geography(mention, norm)

    # Primary-entity shortcut: "NASA's SpaceX Crew-15 mission" → "Crew-15"
    # If the mention is a longer description of the entity being researched, collapse it.
    if (
        primary_entity_id
        and primary_entity_norm
        and len(primary_entity_norm) >= 4
        and primary_entity_norm in norm
        and len(norm.split()) <= len(primary_entity_norm.split()) + 6
    ):
        _add_alias(primary_entity_id, mention, norm, document_id)
        logger.info('resolve: primary-entity shortcut "%s" → %s', mention, primary_entity_id)
        return primary_entity_id

    # Level 2a — exact canonical name (case-insensitive), EVIDENCE-GATED.
    # An entity with actual data always outranks a data-less namesake: routing
    # a mention to a husk because it got the name first is how permanent false
    # splits are born. Data-less matches are demoted to weak hints below.
    # TYPE GUARD: an evidence-bearing namesake of a different kind ("Falcon 9"
    # the launch vehicle vs a company-typed mention) is not auto-routed — the
    # LLM confirms against the entity's actual context first.
    # COLLISION GUARD: when TWO OR MORE evidence-bearing entities share the
    # exact name (two companies legitimately called "Stellar"), first() would
    # be an arbitrary pick that silently absorbs documents into the wrong
    # entity — so the choice is promoted to comparative adjudication with each
    # candidate's distinctive context, exactly like Level 4.
    evidence_q = (
        Q(assertions__status='accepted', assertions__superseded_at__isnull=True)
        | Q(events__isnull=False)
        | Q(fragments__isnull=False)
    )
    ent_matches = list(
        Entity.objects
        .filter(canonical_name__iexact=mention, status='active')
        .filter(evidence_q)
        .distinct()
    )
    type_rejected = None
    fallback_id = None
    ambig_ids: frozenset = frozenset()
    if len(ent_matches) > 1:
        ambig_ids = frozenset(str(e.id) for e in ent_matches)
        cand = [(str(e.id), e.canonical_name, 1.0) for e in ent_matches]
        resolved, confidence = _llm_disambiguate(mention, entity_type, cand, subject_context)
        if resolved:
            logger.info('L2a ambiguous-exact adjudicated: "%s" → %s (%s)',
                        mention, resolved, confidence)
            _add_alias(resolved, mention, norm, document_id)
            return resolved
        logger.info(
            'L2a ambiguous-exact unresolved for "%s" (%d same-name entities) — falling through',
            mention, len(ent_matches))
        # Unresolved collision: routing to one of them is 50/50, but minting a
        # THIRD same-name entity is always wrong — fall back to the best-known.
        try:
            fallback_id = str(max(ent_matches, key=lambda e: e.assertions.count()).id)
        except Exception:
            fallback_id = str(ent_matches[0].id)
    elif ent_matches:
        ent = ent_matches[0]
        if ent.entity_type == entity_type or _llm_verify_entity(
                mention, entity_type, subject_context, str(ent.id)):
            _add_alias(str(ent.id), mention, norm, document_id)
            return str(ent.id)
        type_rejected = str(ent.id)
        fallback_id = type_rejected
        logger.info(
            'L2a type-guard: "%s" (%s) rejected namesake %s (%s)',
            mention, entity_type, type_rejected, ent.entity_type,
        )
    # Weak exact-name shortcut: only when the name is unambiguous (a single
    # data-less match). In an unresolved collision even the husk is an
    # arbitrary pick — let L4/L5/stub decide instead.
    weak_ent = None
    if not ambig_ids:
        weak_matches = list(
            Entity.objects
            .filter(canonical_name__iexact=mention, status='active')
        )
        weak_ent = weak_matches[0] if len(weak_matches) == 1 else None

    # Level 2b — exact alias_norm match. An alias owned by an ESTABLISHED
    # entity routes immediately; an alias owned only by a STUB is a weak hint
    # (stubs exist because resolution once failed) — it is only used if the
    # candidate adjudication below finds nothing better.
    # TYPE GUARD: same check as L2a — an established owner of a different kind
    # must be confirmed before the alias routes blindly.
    # COLLISION GUARD: same as L2a — an alias owned by several established
    # entities is adjudicated, never routed arbitrarily.
    alias_rows = list(
        EntityAlias.objects
        .select_related('entity')
        .filter(alias_norm=norm, entity__status='active')
    )
    owner_ids: list[str] = []
    for r in alias_rows:
        if str(r.entity_id) not in owner_ids:
            owner_ids.append(str(r.entity_id))
    if len(owner_ids) == 1:
        alias = alias_rows[0]
        if alias.entity.entity_type == entity_type or _llm_verify_entity(
                mention, entity_type, subject_context, str(alias.entity_id)):
            return str(alias.entity_id)
        logger.info(
            'L2b type-guard: alias "%s" (%s) rejected owner %s (%s)',
            mention, entity_type, alias.entity_id, alias.entity.entity_type,
        )
        if not fallback_id:
            fallback_id = str(alias.entity_id)
    elif len(owner_ids) > 1 and frozenset(owner_ids) != ambig_ids:
        cand = []
        seen = set()
        for r in alias_rows:
            eid = str(r.entity_id)
            if eid not in seen:
                seen.add(eid)
                cand.append((eid, r.entity.canonical_name, 1.0))
        resolved, confidence = _llm_disambiguate(mention, entity_type, cand, subject_context)
        if resolved:
            logger.info('L2b ambiguous-alias adjudicated: "%s" → %s (%s)',
                        mention, resolved, confidence)
            return resolved
        logger.info('L2b ambiguous-alias unresolved for "%s" (%d owners)', mention, len(owner_ids))
    # (owner set already adjudicated at L2a → same candidates at temperature 0 —
    #  asking twice would return the same answer, so we fall through instead.)

    # Level 3+4 — candidate collection + ONE batched LLM adjudication.
    # Candidates come from trigram similarity AND word-boundary containment
    # ("spire" must surface "Spire Global" even when higher-similarity names
    # crowd it out — the single most common false-split shape).
    candidates = _collect_candidates(norm)
    if candidates:
        resolved, confidence = _llm_pick_candidate(mention, entity_type, candidates, subject_context)
        if resolved:
            logger.info(
                'L4 batch match: "%s" → %s (%s)', mention, resolved, confidence,
            )
            _add_alias(resolved, mention, norm, document_id)
            return resolved

    # A data-less exact-name match — weak evidence only, used when candidate
    # adjudication finds nothing better.
    if weak_ent and str(weak_ent.id) == type_rejected:
        weak_ent = None  # the type guard already rejected this namesake
    if weak_ent:
        _add_alias(str(weak_ent.id), mention, norm, document_id)
        return str(weak_ent.id)

    # Level 5 — LLM world-knowledge canonical lookup
    # Handles acronyms, legal name variants, and compound/diacritic person names.
    if entity_type == 'person' or _should_call_l5(mention):
        canonical, found_id = _llm_known_entity(mention, entity_type, subject_context)
    else:
        canonical, found_id = None, None
    if found_id:
        _add_alias(found_id, mention, norm, document_id)
        logger.info('L5 match: "%s" → %s (canonical: %s)', mention, found_id, canonical)
        return found_id

    # Use LLM-suggested canonical as the stub name when provided (cleaner than raw mention)
    # SAFETY FLOOR: a same-name duplicate is ALWAYS wrong. If the exact-name
    # match was rejected by the type guard or the collision adjudication came
    # back undecided, routing to the existing evidence-bearing namesake — even
    # with a mismatched type label — beats minting a twin entity.
    if fallback_id:
        logger.info(
            'resolve: "%s" → same-name fallback %s (guard rejected/undecided; '
            'a duplicate stub is worse than a type-label mismatch)',
            mention, fallback_id,
        )
        _add_alias(fallback_id, mention, norm, document_id)
        return fallback_id

    stub_mention = canonical if canonical else mention
    stub_norm = normalize_name(stub_mention) if canonical else norm
    entity_id = _create_entity(stub_mention, stub_norm, document_id, entity_type)
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


def _collect_candidates(norm: str, limit: int = 8) -> list:
    """Candidate entities for adjudication: word-boundary containment FIRST
    (a short-form mention must see its long-form owner — 'spire' → 'Spire
    Global' — even when higher-similarity names crowd it out), then trigram
    similarity. Deduplicated per entity. Returns [(entity_id_str, name, sim)].
    """
    candidates: dict[str, tuple] = {}

    # 1. Containment — the false-split killer. 'spire' matches 'spire global'
    #    (prefix) and 'spire global canada' matches 'spire global' (suffix-ish
    #    word boundary), regardless of trigram score.
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (a.entity_id::text)
                a.entity_id::text, a.alias_norm
            FROM entity_alias a
            JOIN entity e ON e.id = a.entity_id
            WHERE e.status IN ('active')
              AND (a.alias_norm LIKE %s || ' %%'
                   OR a.alias_norm LIKE '%% ' || %s
                   OR a.alias_norm LIKE '%% ' || %s || ' %%'
                   OR %s LIKE a.alias_norm || ' %%')
            LIMIT %s
            """,
            [norm, norm, norm, norm, limit],
        )
        for entity_id, alias_norm in cur.fetchall():
            if alias_norm != norm:  # exact match was already handled upstream
                candidates[entity_id] = (entity_id, alias_norm, 1.0)

    # 2. Trigram similarity
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT entity_id::text, alias_norm, similarity(alias_norm, %s) AS sim
            FROM entity_alias
            WHERE similarity(alias_norm, %s) > %s
            ORDER BY sim DESC
            LIMIT 24
            """,
            [norm, norm, _TRGM_LLM_MIN],
        )
        for entity_id, alias_norm, sim in cur.fetchall():
            if entity_id not in candidates and alias_norm != norm:
                candidates[entity_id] = (entity_id, alias_norm, float(sim))

    if len(candidates) <= limit:
        # Deterministic order: containment hits first, then similarity desc,
        # alphabetical tiebreak — stable candidate numbering for the LLM.
        return sorted(candidates.values(), key=lambda r: (-r[2], r[1]))

    # Cap: keep containment hits (sim==1.0) plus the best trigram hits
    ranked = sorted(candidates.values(), key=lambda r: (-r[2], r[1]))
    return ranked[:limit]


_PICK_SYSTEM = (
    'You are an entity resolver for a space-industry knowledge graph. '
    'Answer only with valid JSON.'
)

_PICK_USER = """\
A document mentions "{mention}" (type: {entity_type}). Which ONE of the known
entities below is the SAME real-world organisation — or is it none of them?

{candidates}

Rules:
- SAME if the mention is a short form, acronym, or language variant of a candidate
  ("Spire" = "Spire Global", "JPL" = "Jet Propulsion Laboratory").
- SAME if the only difference is a legal suffix or a regional arm descriptor
  ONLY when the candidate has no separate legal existence — when unsure, choose none.
- DIFFERENT if a meaningful qualifier distinguishes them ("6G StarLab" is not "StarLab",
  "NASA JPL" is not "NASA", "ICEYE US" is not "ICEYE" — separate legal entities).
- DIFFERENT if related but legally separate.
- DIFFERENT when names collide across domains — a shared word is not a shared
  identity. SpaceX's "Starship" launch vehicle is NOT Starship Technologies
  (delivery robots); a product named like a company is not that company.
  Compare what each entity IS and DOES, not how the names sound.
- Use each candidate's knowledge context (facts, relations, description).
- Reply "match": null when uncertain — a missed match is safer than a wrong merge.

Reply: {{"match": <candidate number or null>, "confidence": "high"|"medium"|"low"}}"""


def _llm_pick_candidate(mention: str, entity_type: str, candidates: list,
                        subject_context: str = '') -> tuple[str | None, str]:
    """One LLM call adjudicating the mention against ALL candidates at once.

    A comparative decision ("which of these, or none?") is both cheaper than
    per-candidate calls and more accurate — the model sees the alternatives.
    Returns (entity_id or None, confidence str).
    """
    if not candidates:
        return None, 'low'
    try:
        from ingest.ai import get_client
        from ingest.cost import log_call
        client = get_client()

        blocks = []
        for i, (entity_id, alias_norm, sim) in enumerate(candidates, start=1):
            context = _entity_context_for_resolution(entity_id)
            block = f'{i}. "{alias_norm}" (similarity {sim:.2f})'
            if context:
                block += f'\n   {context[:500].replace(chr(10), " | ")}'
            blocks.append(block)

        user_msg = _PICK_USER.format(
            mention=mention, entity_type=entity_type,
            candidates='\n'.join(blocks),
        )
        if subject_context:
            user_msg += f'\n\nDocument context: {subject_context}'

        resp = client.chat.completions.create(
            model=settings.AI_MODEL_FAST,
            messages=[
                {'role': 'system', 'content': get_prompt('resolve_entity', _PICK_SYSTEM)},
                {'role': 'user', 'content': user_msg},
            ],
            response_format={'type': 'json_object'},
            max_tokens=120,
            temperature=0,
        )
        log_call('resolve', settings.AI_MODEL_FAST, resp)
        raw = (resp.choices[0].message.content or '').strip()
        if not raw:
            return None, 'low'
        result = json.loads(raw)
        confidence = result.get('confidence', 'low')
        match = result.get('match')
        if match is None or confidence not in ('high', 'medium'):
            return None, confidence
        try:
            idx = int(match) - 1
        except (TypeError, ValueError):
            return None, confidence
        if 0 <= idx < len(candidates):
            return str(candidates[idx][0]), confidence
        return None, confidence
    except Exception as exc:
        logger.warning('L4 batch resolve failed for "%s": %s', mention, exc)
        return None, 'low'


# ── Same-name collision adjudication (small focused call) ────────────────────
# Operator rule: prefer a small LLM call that clearly distinguishes things over
# being too fast to compare — but keep input AND output tiny. One-line identity
# summaries, ~30-token JSON answer. Distinctive features still decide.

_DISAMBIG_SYSTEM = (
    'You disambiguate same-name entities for a knowledge graph. '
    'Answer only with valid JSON.'
)

_DISAMBIG_USER = """\
A document mentions "{mention}" (type: {entity_type}).
Document context: {context}

Entities with this exact name:
{blocks}

Which one is the mention about, or none?
Reply: {{"match": <number or null>, "confidence": "high"|"medium"|"low"}}"""


def _llm_disambiguate(mention: str, entity_type: str, candidates: list,
                      subject_context: str = '') -> tuple[str | None, str]:
    """Minimal-token adjudication between same-name evidence-bearing entities.

    Same contract as _llm_pick_candidate, but the prompt carries no rulebook
    and each candidate is ONE line (~200 chars): type + facts + description
    prefix — the distinctive features, nothing else. Returns (entity_id|None,
    confidence). Fails closed like every resolve call.
    """
    if not candidates:
        return None, 'low'
    try:
        from ingest.ai import get_client
        from ingest.cost import log_call
        client = get_client()

        blocks = []
        for i, (entity_id, name, _sim) in enumerate(candidates, start=1):
            ctx = _entity_context_for_resolution(entity_id)
            summary = ctx[:200].replace('\n', ' | ') if ctx else '(no data)'
            blocks.append(f'{i}. "{name}" — {summary}')

        user_msg = _DISAMBIG_USER.format(
            mention=mention, entity_type=entity_type,
            context=(subject_context or '(not available)')[:200],
            blocks='\n'.join(blocks),
        )
        resp = client.chat.completions.create(
            model=settings.AI_MODEL_FAST,
            messages=[
                {'role': 'system', 'content': _DISAMBIG_SYSTEM},
                {'role': 'user', 'content': user_msg},
            ],
            response_format={'type': 'json_object'},
            max_tokens=30,
            temperature=0,
        )
        log_call('resolve', settings.AI_MODEL_FAST, resp)
        raw = (resp.choices[0].message.content or '').strip()
        if not raw:
            return None, 'low'
        result = json.loads(raw)
        confidence = result.get('confidence', 'low')
        match = result.get('match')
        if match is None or confidence not in ('high', 'medium'):
            return None, confidence
        try:
            idx = int(match) - 1
        except (TypeError, ValueError):
            return None, confidence
        if 0 <= idx < len(candidates):
            return str(candidates[idx][0]), confidence
        return None, confidence
    except Exception as exc:
        logger.warning('disambiguation failed for "%s": %s', mention, exc)
        return None, 'low'


def _find_or_create_geography(mention: str, norm: str) -> str:
    """Find or create a geography entity (city, country) as a relation target for has_office_in.

    No geocoding — coordinates come from the address qualifier on the relation itself.
    """
    alias = (
        EntityAlias.objects
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
    """Register surface_form as an alias for entity_id, with ownership arbitration.

    An alias_norm routes every future mention of that name, so WHO owns it is a
    resolution decision, not bookkeeping:
      - free → create
      - owned by the same entity → nothing to do
      - owned by a STUB → rebind to the established entity (the stub was a
        failed resolution; it must not keep routing the name)
      - owned by another ACTIVE entity → do not steal; queue the pair for
        targeted dedup so the collision is adjudicated with full context.
    """
    existing = (
        EntityAlias.objects
        .select_related('entity')
        .filter(alias_norm=norm)
        .first()
    )
    if existing is None:
        EntityAlias.objects.create(
            entity_id=entity_id,
            alias=surface_form,
            alias_norm=norm,
            alias_kind='abbrev',
            document_id=document_id,
        )
        return

    if str(existing.entity_id) == str(entity_id):
        return

    # Two established entities claim the same name — genuine collision.
    # Adjudicate the pair with full context (cheap, targeted dedup).
    owner = existing.entity
    logger.warning(
        'Alias collision: "%s" owned by %s (%s); %s also resolved here — queueing dedup',
        norm, owner.canonical_name, owner.id, entity_id,
    )
    try:
        from ingest.tasks.dedup import dedup_entities
        dedup_entities.apply_async(
            args=[[str(entity_id), str(owner.id)]], countdown=120,
        )
    except Exception as exc:
        # Best-effort: alias routing must never break resolution (e.g. broker down)
        logger.warning('dedup enqueue for alias collision failed: %s', exc)


def _llm_known_entity(mention: str, entity_type: str, subject_context: str = '') -> tuple[str | None, str | None]:
    """L5 — ask the LLM for the canonical name using world knowledge.

    Returns (canonical_name, entity_id):
    - entity_id set → canonical found in DB, merge into it
    - canonical_name set, entity_id None → use canonical as stub name
    - both None → LLM doesn't recognise the mention
    subject_context provides the primary subject being researched in the source document.
    """
    try:
        from ingest.ai import get_client
        from ingest.cost import log_call
        client = get_client()

        if entity_type == 'person':
            user_content = _LLM_CANONICAL_PERSON.format(mention=mention)
        else:
            user_content = _LLM_CANONICAL_USER.format(mention=mention, entity_type=entity_type)
        if subject_context:
            user_content += f'\n\nDocument context: {subject_context}'

        resp = client.chat.completions.create(
            model=settings.AI_MODEL_FAST,
            messages=[
                {'role': 'system', 'content': _LLM_RESOLVE_SYSTEM},
                {'role': 'user', 'content': user_content},
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
        if not canonical or result.get('confidence') not in ('high', 'medium'):
            return None, None

        # If canonical is the same as the mention, no new information
        if normalize_name(canonical) == normalize_name(mention):
            return None, None

        # Try to find it in the DB by canonical name
        ent = Entity.objects.filter(
            canonical_name__iexact=canonical,
            status__in=('active',),
        ).first()
        if ent:
            if not _llm_verify_entity(mention, entity_type, subject_context, str(ent.id)):
                logger.info(
                    'L5 verification rejected "%s" → canonical "%s" (entity %s is a different thing)',
                    mention, canonical, ent.id,
                )
                return None, None
            return canonical, str(ent.id)

        # Try alias_norm
        alias = (
            EntityAlias.objects
            .select_related('entity')
            .filter(alias_norm=normalize_name(canonical), entity__status='active')
            .first()
        )
        if alias:
            if not _llm_verify_entity(mention, entity_type, subject_context, str(alias.entity_id)):
                logger.info(
                    'L5 verification rejected "%s" → canonical "%s" (entity %s is a different thing)',
                    mention, canonical, alias.entity_id,
                )
                return None, None
            return canonical, str(alias.entity_id)

        # Canonical not in DB yet — return name only so stub uses the better form
        logger.info('L5 canonical "%s" not in DB for mention "%s" — will use as stub name', canonical, mention)
        return canonical, None

    except Exception as exc:
        logger.warning('L5 world-knowledge lookup failed for "%s": %s', mention, exc)
        return None, None


def _entity_has_evidence(entity_id: str) -> bool:
    """True when the entity carries real data (fragments, events, accepted assertions).

    Evidence-less husks have no identity to clash with — merging into them is
    safe and verification would be a wasted LLM call.
    """
    from core.models import Assertion, Event, KnowledgeFragment
    return (
        KnowledgeFragment.objects.filter(entity_id=entity_id).exists()
        or Event.objects.filter(entity_id=entity_id).exists()
        or Assertion.objects.filter(
            entity_id=entity_id, status='accepted', superseded_at__isnull=True,
        ).exists()
    )


def _llm_verify_entity(
    mention: str, entity_type: str, subject_context: str, entity_id: str,
) -> bool:
    """L5-verify — confirm a world-knowledge name match against the graph entity.

    L5 maps a mention to a canonical NAME via world knowledge, then looks that
    name up in the graph. But names collide across domains (SpaceX's Starship
    launch vehicle vs Starship Technologies' delivery robots — both genuinely
    called "Starship"), so a name hit must be confirmed against what the
    existing entity actually IS before merging. Fails closed: an unverifiable
    match is treated as no match.
    """
    if not _entity_has_evidence(entity_id):
        return True  # husk — nothing to clash with
    try:
        from ingest.ai import get_client
        from ingest.cost import log_call
        client = get_client()

        entity = Entity.objects.only('canonical_name', 'entity_type').get(pk=entity_id)
        context = _entity_context_for_resolution(entity_id)
        user_msg = _LLM_VERIFY_USER.format(
            mention=mention, entity_type=entity_type,
            subject_context=subject_context or '(not available)',
            candidate_name=entity.canonical_name,
            candidate_type=entity.entity_type,
            candidate_context=context or '(no data)',
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
            return False
        raw = raw.replace('True', 'true').replace('False', 'false').replace('None', 'null')
        result = json.loads(raw)
        return result.get('match') is True and result.get('confidence') in ('high', 'medium')
    except Exception as exc:
        logger.warning('L5 verification failed for "%s": %s', mention, exc)
        return False


def _create_entity(
    mention: str,
    norm: str,
    document_id: str | None,
    entity_type: str = 'company',
) -> str:
    """Create an entity for an unresolved mention — active from birth.

    There is no stub lifecycle: an entity created by resolution is a real
    entity immediately. The daily dedup sweep reconciles duplicates, and the
    classify pass audits types; neither needs a probation status.
    """
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

    # Initial assessment: world-knowledge description + space_relevance score.
    # Fires once per new entity; routes to further research based on score.
    if entity_type not in ('geography', 'document_node'):
        from ingest.tasks.summarise import synthesise_entity_summary
        synthesise_entity_summary.apply_async(args=[str(entity.id)], countdown=10)

    return str(entity.id)


# ── Alias enrichment ────────────────────────────────────────────────────────

_ALIAS_SYSTEM = """\
You are an alias generator for a space-industry knowledge graph.
Given an organisation's canonical name, list every alternative name it is genuinely known by.

Include ALL of the following that apply:
- Legal / registered name with corporate suffix (e.g. "Sateliot" → "Satel IoT SL")
- Brand or trading name if different from legal name
- Acronym or initialisation (e.g. "IEEC", "ESA", "GMV")
- Names in other languages (English, Spanish, Catalan, French, German…)
- Former names or name before rebranding
- Common shortened or colloquial forms

Rules:
- Only include names you are highly confident are real alternative names for this specific entity.
- Do not invent names. Return an empty list if you are uncertain.
- Do not include generic descriptions (e.g. "the space agency").
- Limit to 8 aliases maximum.

Return JSON only: {"aliases": ["name1", "name2", ...]}"""


def _fetch_aliases(canonical_name: str, entity_type: str, context: str = '') -> list[str]:
    """Ask the LLM for known alternative names for this entity.

    *context* is optional additional text (e.g. entity overview from summarise stage)
    that helps the LLM reason about obscure or ambiguous entities.
    """
    try:
        from ingest.ai import get_client
        from ingest.cost import log_call
        from ingest.prompts import get_prompt
        client = get_client()
        user_content = f'Organisation: "{canonical_name}" (type: {entity_type})'
        if context:
            user_content += f'\n\nContext:\n{context[:600]}'
        resp = client.chat.completions.create(
            model=settings.AI_MODEL_FAST,
            messages=[
                {'role': 'system', 'content': get_prompt('alias_enrichment', _ALIAS_SYSTEM)},
                {'role': 'user', 'content': user_content},
            ],
            response_format={'type': 'json_object'},
            max_tokens=300,
            temperature=0,
        )
        log_call('alias_enrichment', settings.AI_MODEL_FAST, resp)
        raw = (resp.choices[0].message.content or '').strip()
        if not raw:
            return []
        data = json.loads(raw)
        return [str(a).strip() for a in data.get('aliases', []) if a and str(a).strip()]
    except Exception as exc:
        logger.warning('_fetch_aliases "%s": %s', canonical_name, exc)
        return []


from celery import shared_task as _shared_task


@_shared_task(bind=True, queue='extract', max_retries=1, default_retry_delay=60)
def enrich_entity_aliases(self, entity_id: str, canonical_name: str, entity_type: str, context: str = ''):
    """Generate and register alternative names for an entity using its synthesised summary as context."""
    aliases = _fetch_aliases(canonical_name, entity_type, context)
    if not aliases:
        return

    registered = 0
    for alias in aliases:
        norm = normalize_name(alias)
        if not norm or norm == normalize_name(canonical_name):
            continue
        # Skip if already registered for this or any other entity (avoid cross-entity alias collision)
        if EntityAlias.objects.filter(alias_norm=norm).exists():
            continue
        try:
            EntityAlias.objects.create(
                entity_id=entity_id,
                alias=alias,
                alias_norm=norm,
                alias_kind='abbrev',
            )
            registered += 1
        except Exception:
            pass  # unique constraint race — harmless

    logger.info('enrich_entity_aliases %s ("%s"): %d aliases registered', entity_id, canonical_name, registered)
