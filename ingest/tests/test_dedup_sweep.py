"""Dedup sweep pairing — word-boundary containment catches short-form/long-form
false splits ('Starliner' vs 'CST-100 Starliner') that trigram similarity misses.
Containment pairs carry LLM-band similarity (0.75): adjudicated, never auto-merged."""
import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

from core.models import Entity, EntityAlias, EntityIdentifier


def _make_named(name, etype, norm):
    e = Entity.objects.create(
        entity_type=etype, canonical_name=name,
        slug=f'{name.lower().replace(" ", "-")}-{uuid.uuid4().hex[:8]}',
        status='active',
    )
    EntityAlias.objects.create(entity=e, alias=name, alias_norm=norm, alias_kind='trading')
    return e


def _client_replying(payload):
    client = MagicMock()
    resp = MagicMock()
    resp.choices[0].message.content = json.dumps(payload)
    resp.usage.prompt_tokens = 100
    resp.usage.completion_tokens = 10
    client.chat.completions.create.return_value = resp
    return client


@pytest.mark.django_db
@pytest.mark.django_db
def test_forced_qid_merges_without_llm():
    """An anchor colliding with a QID already held by another entity means the
    two are the same real-world thing — typically a cross-language split
    (English vs Catalan institute name). dedup_entities merges them
    deterministically with zero LLM calls, however dissimilar the names."""
    a = _make_named('Institute of Space Studies of Catalonia', 'company',
                    'institute of space studies of catalonia')
    b = _make_named("Institut d'Estudis Espacials de Catalunya", 'company',
                    'institut d estudis espacials de catalunya')
    EntityIdentifier.objects.create(scheme='wikidata', value='Q123456', entity=a, confidence=90)

    client = MagicMock()
    with patch('ingest.ai.get_client', return_value=client):
        from ingest.tasks.dedup import dedup_entities
        result = dedup_entities([str(a.id), str(b.id)],
                                forced_qids={str(b.id): 'Q123456'})

    assert result['merged'] == 1
    client.chat.completions.create.assert_not_called()
    b.refresh_from_db()
    assert b.status == 'merged'


@pytest.mark.django_db
@pytest.mark.django_db
def test_kept_selection_weighs_total_evidence_not_fresh_assertions():
    """A few fresh accepted assertions must not let a young entity absorb an
    established one (the IEEC failure: 9 fresh assertions outweighed 23
    fragments + 17 events). Total evidence mass decides. The pair arrives via
    a QID collision, as it did in production."""
    from core.models import Assertion, EntityIdentifier, Event, KnowledgeFragment

    young = _make_named('Institute of Space Studies of Catalonia', 'company',
                        'institute of space studies of catalonia')
    est = _make_named("Institut d'Estudis Espacials de Catalunya (IEEC)", 'company',
                      'institut d estudis espacials de catalunya ieec')
    EntityIdentifier.objects.create(scheme='wikidata', value='Q20105088', entity=est, confidence=90)
    for attr in ('website', 'headquarters_country', 'founding_year'):
        Assertion.objects.create(entity=young, attribute_id=attr,
                                 value_text='ES', status='accepted', confidence=90)
    for _ in range(10):
        KnowledgeFragment.objects.create(entity=est, category='general', text='x', confidence=80)
    for _ in range(8):
        Event.objects.create(entity=est, title='e')

    client = MagicMock()
    with patch('ingest.ai.get_client', return_value=client):
        from ingest.tasks.dedup import dedup_entities
        result = dedup_entities([str(young.id), str(est.id)],
                                forced_qids={str(young.id): 'Q20105088'})

    assert result['merged'] == 1
    client.chat.completions.create.assert_not_called()
    young.refresh_from_db()
    est.refresh_from_db()
    assert est.status == 'active' and young.status == 'merged'


@pytest.mark.django_db
def test_sweep_pairs_short_form_with_long_form():
    a = _make_named('CST-100 Starliner', 'asset', 'cst 100 starliner')
    b = _make_named('Starliner', 'asset', 'starliner')

    with patch('ingest.ai.get_client', return_value=_client_replying(
            {'same': True, 'confidence': 'high'})):
        from ingest.tasks.dedup import dedup_sweep
        result = dedup_sweep()

    assert result['merged'] == 1
    b.refresh_from_db()
    assert b.status == 'merged'


@pytest.mark.django_db
def test_sweep_containment_pairs_go_to_llm_not_auto_merge():
    """'Apollo' vs 'Apollo 11' are different things — the LLM verdict records
    a non-merge instead of fusing them, and the pair is never re-litigated
    without new evidence."""
    a = _make_named('Apollo 11', 'program', 'apollo 11')
    b = _make_named('Apollo', 'program', 'apollo')

    with patch('ingest.ai.get_client', return_value=_client_replying(
            {'same': False, 'confidence': 'high'})):
        from ingest.tasks.dedup import dedup_sweep
        result = dedup_sweep()

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.status == 'active' and b.status == 'active'
    from core.models import EntityNonMerge
    assert EntityNonMerge.objects.exists()
