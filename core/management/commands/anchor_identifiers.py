"""Anchor entities to external identifiers — the root of graph correctness.

Populates EntityIdentifier from two deterministic sources:
  1. Wikidata QIDs — searched via the free Wikidata API. An entity is anchored
     only when a candidate's label/aliases match the normalised name exactly AND
     the candidate is plausibly a space-industry organisation (description
     keywords or space_relevance == 100). Ambiguous matches are skipped —
     a missing anchor is cheap, a wrong anchor poisons dedup forever.
  2. Domains — extracted from each entity's `website` assertion.

These identifiers make deduplication deterministic: same QID merges without an
LLM call, different QIDs are recorded as permanent non-merges (EntityNonMerge).

Usage:
    python manage.py anchor_identifiers            # both sources
    python manage.py anchor_identifiers --skip-wikidata
    python manage.py anchor_identifiers --limit 500 --dry-run
"""
from django.core.management.base import BaseCommand
from django.db.models import Q
import re

from core.models import Assertion, Entity, EntityIdentifier
from core.normalize import normalize_name

WIKIDATA_API = 'https://www.wikidata.org/w/api.php'
_UA = 'EigenGraph/0.1 (space-industry knowledge graph; contact@forespace.io)'

# Description keywords that make a Wikidata candidate plausibly our entity's domain.
_SPACE_HINTS = (
    'space', 'aerospace', 'satellite', 'rocket', 'launch ', 'orbital',
    'defence', 'defense', 'aerospace company', 'space agency', 'aviation',
    'telescope', 'spacecraft', 'astronautics', 'aeronautics',
)


def _search_wikidata(name: str) -> list:
    import requests
    resp = requests.get(
        WIKIDATA_API,
        params={
            'action': 'wbsearchentities', 'search': name, 'language': 'en',
            'format': 'json', 'limit': 5,
        },
        headers={'User-Agent': _UA}, timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get('search', [])


def _entity_name_variants(entity: Entity) -> set:
    """Normalised name variants for matching: canonical + aliases, each also
    with parenthetical acronyms stripped ("X (ABC)" matches "x" and "x abc").

    Canonical names often carry a parenthetical qualifier the external
    registry never uses ("Institut d'Estudis Espacials de Catalunya (IEEC)"),
    and the registry label may match an ALIAS instead ("Institute of Space
    Studies of Catalonia" is an English alias of the Catalan-named item).
    """
    from core.models import EntityAlias
    raw_names = [entity.canonical_name]
    raw_names.extend(
        EntityAlias.objects.filter(entity=entity).values_list('alias', flat=True)
    )
    variants = set()
    for raw in raw_names:
        for form in (raw, re.sub(r'\([^)]*\)', ' ', raw)):
            norm = normalize_name(form)
            if norm:
                variants.add(norm)
    return variants


def _anchor_wikidata(entity: Entity) -> str:
    """Return the QID if exactly one unambiguous, plausible match exists.

    Searches the canonical name (parenthetical stripped) plus up to two
    aliases, and requires ONE distinct QID across ALL searches — a second
    name form matching a different item means the entity's names disagree
    about what it is, and a wrong anchor poisons dedup forever.
    """
    from core.models import EntityAlias

    variants = _entity_name_variants(entity)
    if not variants:
        return ''

    search_strings = [re.sub(r'\([^)]*\)', ' ', entity.canonical_name).strip()]
    for alias in EntityAlias.objects.filter(entity=entity).values_list('alias', flat=True)[:3]:
        stripped = re.sub(r'\([^)]*\)', ' ', alias).strip()
        if stripped and stripped not in search_strings:
            search_strings.append(stripped)

    found = set()
    for query in search_strings[:3]:
        try:
            results = _search_wikidata(query)
        except Exception:
            return ''  # network errors must not abort the sweep
        for cand in results:
            labels = {cand.get('label', '')}
            labels.update(cand.get('aliases', []) or [])
            cand_norms = {normalize_name(a) for a in labels if a}
            if not (cand_norms & variants):
                continue
            description = (cand.get('description') or '').lower()
            if entity.space_relevance == 100 or any(h in description for h in _SPACE_HINTS):
                found.add(cand['id'])

    if len(found) == 1:
        return next(iter(found))
    return ''  # zero or ambiguous


def _extract_domain(url: str) -> str:
    from urllib.parse import urlparse
    try:
        host = urlparse(url if '://' in url else f'https://{url}').netloc.lower()
        return host.removeprefix('www.').strip('/')
    except Exception:
        return ''


class Command(BaseCommand):
    help = 'Anchor entities to external identifiers (Wikidata QIDs, website domains).'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=300,
                            help='Max Wikidata lookups per run (politeness).')
        parser.add_argument('--min-score', type=int, default=50,
                            help='Only anchor entities with space_relevance >= this.')
        parser.add_argument('--skip-wikidata', action='store_true')
        parser.add_argument('--skip-domains', action='store_true')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        import time

        anchored = skipped = domain_count = 0

        # ── 1. Domain identifiers from website assertions ────────────────
        if not options['skip_domains']:
            existing = set(
                EntityIdentifier.objects.filter(scheme='domain')
                .values_list('entity_id', flat=True)
            )
            sites = (
                Assertion.objects
                .filter(attribute_id='website', superseded_at__isnull=True)
                .exclude(value_text='')
                .values_list('entity_id', 'value_text')
            )
            for eid, url in sites:
                if eid in existing:
                    continue
                domain = _extract_domain(url)
                if not domain or '.' not in domain:
                    continue
                if not options['dry_run']:
                    EntityIdentifier.objects.get_or_create(
                        scheme='domain', value=domain,
                        defaults={'entity_id': eid, 'confidence': 85},
                    )
                domain_count += 1
            self.stdout.write(f'domain identifiers: {domain_count} anchored')

        # ── 2. Wikidata QIDs ─────────────────────────────────────────────
        if not options['skip_wikidata']:
            anchored_ids = set(
                EntityIdentifier.objects.filter(scheme='wikidata')
                .values_list('entity_id', flat=True)
            )
            candidates = (
                Entity.objects
                .filter(status='active',
                        space_relevance__gte=options['min_score'])
                .filter(Q(entity_type='company') | Q(entity_type='investor')
                        | Q(entity_type='entity') | Q(entity_type='university')
                        | Q(entity_type='program') | Q(entity_type='funding_program'))
                .exclude(id__in=anchored_ids)
                .order_by('-space_relevance', '-created_at')
                [:options['limit']]
            )
            for entity in candidates:
                qid = _anchor_wikidata(entity)
                if qid:
                    existing = EntityIdentifier.objects.filter(
                        scheme='wikidata', value=qid,
                    ).exclude(entity_id=entity.id).first()
                    if existing is not None and not options['dry_run']:
                        # Same QID, different entity = same real-world thing
                        # (typically a cross-language or short-form split).
                        # Route through dedup's guarded merge; arbitration
                        # auto-merges with zero LLM calls. The QID ends up on
                        # the survivor via execute_merge's identifier transfer.
                        from ingest.tasks.dedup import dedup_entities
                        dedup_entities(
                            [str(existing.entity_id), str(entity.id)],
                            forced_qids={str(entity.id): qid},
                        )
                        anchored += 1
                        self.stdout.write(
                            f'  {entity.canonical_name[:50]:<50} -> {qid} '
                            f'(QID collision with another entity -> merged)'
                        )
                        continue
                    if existing is None and not options['dry_run']:
                        EntityIdentifier.objects.get_or_create(
                            scheme='wikidata', value=qid,
                            defaults={'entity_id': entity.id, 'confidence': 90},
                        )
                    anchored += 1
                    self.stdout.write(f'  {entity.canonical_name[:50]:<50} -> {qid}')
                else:
                    skipped += 1
                time.sleep(0.6)  # Wikidata politeness

            self.stdout.write(
                f'wikidata: {anchored} anchored, {skipped} unmatched/ambiguous '
                f'({options["limit"]} max looked up)'
            )

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('DRY RUN - nothing written.'))
