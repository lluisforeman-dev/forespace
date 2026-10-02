"""Ownership-graph enrichment from Wikidata corporate hierarchies.

For every entity anchored by a Wikidata QID (EntityIdentifier scheme='wikidata'),
fetch P127 (owned by) and P355 (subsidiary) claims and materialise them as
`subsidiary_of` relations — but ONLY when both endpoints already exist locally.
No stubs are created: linking what the graph already knows keeps the enrichment
cheap, safe, and reviewable. This is what connects split corporate families
(the 26 orphaned Airbus subsidiaries) into navigable group structures.

Usage:
    python manage.py enrich_ownership          # manual run
    enrich_ownership.delay()                   # task (analytics queue)
"""
from __future__ import annotations

import logging
import time

from celery import shared_task
from django.db import transaction

logger = logging.getLogger(__name__)

_WIKIDATA_API = 'https://www.wikidata.org/w/api.php'
_UA = 'eigengraph-knowledge-graph/1.0 (entity resolution enrichment)'
_CHUNK = 50

# P127 "owned by": statement on the CHILD, value = PARENT.
# P355 "subsidiary": statement on the PARENT, value = CHILD.
_OWNERSHIP_PROPS = ('P127', 'P355')


def _norm_qid(value: str) -> str | None:
    """Extract a QID from 'Q123', ' q123 ', or a full entity URI."""
    if not value:
        return None
    v = value.strip().upper()
    if v.startswith('HTTP'):
        v = v.rsplit('/', 1)[-1]
    return v if v.startswith('Q') and v[1:].isdigit() else None


def _fetch_claims(qids: list[str], timeout: int = 30) -> dict:
    """Fetch ownership-relevant claims for a chunk of QIDs."""
    import requests
    resp = requests.get(
        _WIKIDATA_API,
        params={
            'action': 'wbgetentities',
            'ids': '|'.join(qids),
            'props': 'claims',
            'format': 'json',
        },
        headers={'User-Agent': _UA},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json().get('entities', {})


def _parent_qids(entity_claims: dict) -> dict[str, str]:
    """Return {parent_qid: wikidata_property} for one entity's claims."""
    parents: dict[str, str] = {}
    claims = entity_claims.get('claims', {})
    for prop in _OWNERSHIP_PROPS:
        for claim in claims.get(prop, []):
            snak = claim.get('mainsnak', {})
            if snak.get('snaktype') != 'value':
                continue
            value = snak.get('datavalue', {}).get('value', {})
            parent = _norm_qid(value.get('id', ''))
            if parent:
                parents[parent] = prop
    return parents


def _ensure_relation(child_id, parent_id) -> bool:
    """Create (child) -[subsidiary_of]-> (parent) unless it already exists."""
    from core.models import PredicateDef, Relation
    if not PredicateDef.objects.filter(pk='subsidiary_of').exists():
        logger.warning('ownership: predicate "subsidiary_of" missing from vocabulary')
        return False
    exists = Relation.objects.filter(
        subject_id=child_id, predicate_id='subsidiary_of', object_id=parent_id,
        superseded_at__isnull=True,
    ).exists()
    if exists:
        return False
    with transaction.atomic():
        Relation.objects.create(
            subject_id=child_id,
            predicate_id='subsidiary_of',
            object_id=parent_id,
            method='structured_api',
            confidence=90,
            status='accepted',
            qualifiers={'source': 'wikidata'},
        )
    return True


def run_ownership_enrichment(batch_size: int = 100, sleep_s: float = 1.0) -> dict:
    from core.models import EntityIdentifier

    id_rows = list(
        EntityIdentifier.objects
        .filter(scheme='wikidata')
        .values('entity_id', 'value')[:batch_size]
    )
    qid_to_entity: dict[str, str] = {}
    for row in id_rows:
        qid = _norm_qid(row['value'])
        if qid:
            qid_to_entity.setdefault(qid, str(row['entity_id']))
    if not qid_to_entity:
        return {'linked': 0, 'already': 0, 'missing_twins': 0, 'qids': 0}

    stats = {'linked': 0, 'already': 0, 'missing_twins': 0, 'qids': len(qid_to_entity)}
    qids = list(qid_to_entity)
    for i in range(0, len(qids), _CHUNK):
        chunk = qids[i:i + _CHUNK]
        try:
            entities = _fetch_claims(chunk)
        except Exception as exc:
            logger.warning('ownership: wikidata fetch failed for %d qids: %s', len(chunk), exc)
            continue
        for qid in chunk:
            child_local = qid_to_entity[qid]
            for parent_qid, prop in _parent_qids(entities.get(qid, {})).items():
                parent_local = qid_to_entity.get(parent_qid)
                if not parent_local or parent_local == child_local:
                    stats['missing_twins'] += 1
                    continue
                try:
                    if _ensure_relation(child_local, parent_local):
                        stats['linked'] += 1
                        logger.info('ownership: %s -[subsidiary_of]-> %s (via %s)',
                                    child_local, parent_local, prop)
                    else:
                        stats['already'] += 1
                except Exception as exc:
                    logger.warning('ownership: relation create failed %s->%s: %s',
                                   child_local, parent_local, exc)
        if i + _CHUNK < len(qids):
            time.sleep(sleep_s)
    return stats


@shared_task(queue='analytics', max_retries=0)
def enrich_ownership(batch_size: int = 100):
    return run_ownership_enrichment(batch_size=batch_size)
