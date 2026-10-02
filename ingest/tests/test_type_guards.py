"""Tests for the type-consistency layer.

Covers:
  - L2a/L2b type guards in resolve_mention (type-mismatched exact matches
    must be verified before routing; rejection must fall through)
  - classify._validate_entity_type (high-confidence correction, fail-closed,
    geography blocklist protection)

Patch targets: resolve.py imports get_client inside its functions, so
'ingest.ai.get_client' is the right seam there; classify.py imports it at
module level, so 'ingest.tasks.classify.get_client' must be patched instead.
"""
import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

from core.models import Entity, EntityAlias, KnowledgeFragment
from ingest.tasks.classify import _validate_entity_type
from ingest.tasks.resolve import resolve_mention


def _make_entity(name, etype, with_evidence=True, status='active'):
    return Entity.objects.create(
        entity_type=etype,
        canonical_name=name,
        slug=f'{name.lower().replace(" ", "-")}-{uuid.uuid4().hex[:8]}',
        status=status,
    )


def _frag(entity, text):
    KnowledgeFragment.objects.create(
        entity=entity, category='technical', confidence=80, text=text,
    )


def _reply(payload):
    resp = MagicMock()
    resp.choices[0].message.content = json.dumps(payload)
    resp.usage.prompt_tokens = 100
    resp.usage.completion_tokens = 10
    return resp


def _client_replying(*replies):
    client = MagicMock()
    if len(replies) == 1:
        client.chat.completions.create.return_value = replies[0]
    else:
        client.chat.completions.create.side_effect = list(replies)
    return client


# ── resolve_mention L2a type guard ───────────────────────────────────────────

@pytest.mark.django_db
def test_l2a_same_type_routes_without_llm():
    entity = _make_entity('Falcon 9', 'asset')
    _frag(entity, 'Reusable two-stage launch vehicle.')
    with patch('ingest.ai.get_client') as get_client:
        resolved = resolve_mention('Falcon 9', entity_type='asset')
        assert resolved == str(entity.id)
        get_client.assert_not_called()


@pytest.mark.django_db
def test_l2a_type_mismatch_verified_match_routes():
    entity = _make_entity('Falcon 9', 'asset')
    _frag(entity, 'Reusable two-stage launch vehicle.')
    with patch('ingest.ai.get_client', return_value=_client_replying(_reply(
            {'match': True, 'confidence': 'high'}))):
        # Document calls it a company (extraction default) — verify says same thing
        resolved = resolve_mention('Falcon 9', entity_type='company')
        assert resolved == str(entity.id)


@pytest.mark.django_db
def test_l2a_type_mismatch_rejected_falls_through():
    entity = _make_entity('Falcon 9', 'asset')
    _frag(entity, 'Reusable two-stage launch vehicle.')
    client = _client_replying(
        _reply({'match': False, 'confidence': 'high'}),   # L2a verify → different
        _reply({'canonical': None, 'confidence': 'low'}),  # L5 → unknown
    )
    with patch('ingest.ai.get_client', return_value=client), \
         patch('ingest.tasks.resolve._create_stub', return_value='stub-id-123') as stub:
        resolved = resolve_mention('Falcon 9', entity_type='company')
        assert resolved == 'stub-id-123'
        stub.assert_called_once()
        assert resolved != str(entity.id)


@pytest.mark.django_db
def test_l2b_type_mismatch_rejected_alias_does_not_route():
    entity = _make_entity('Starship', 'asset')
    _frag(entity, 'Fully reusable launch vehicle with Raptor engines.')
    EntityAlias.objects.create(
        entity=entity, alias='Starship e-model delivery robot',
        alias_norm='starship e model delivery robot', alias_kind='abbrev',
    )
    client = _client_replying(
        _reply({'match': False, 'confidence': 'high'}),   # L2b verify
        _reply({'canonical': None, 'confidence': 'low'}),  # L5
    )
    with patch('ingest.ai.get_client', return_value=client), \
         patch('ingest.tasks.resolve._create_stub', return_value='stub-id-456'):
        resolved = resolve_mention(
            'Starship e-model delivery robot', entity_type='company')
        assert resolved == 'stub-id-456'
        assert resolved != str(entity.id)


# ── classify._validate_entity_type ───────────────────────────────────────────

@pytest.mark.django_db
def test_type_correction_applied_on_high_confidence():
    entity = _make_entity('Falcon 9', 'company')
    _frag(entity, 'Falcon 9 is a reusable two-stage launch vehicle designed and '
                  'manufactured by SpaceX. It flies from Kennedy Space Center.')
    client = _client_replying(_reply({'entity_type': 'asset', 'confidence': 'high'}))
    with patch('ingest.tasks.classify.get_client', return_value=client):
        assert _validate_entity_type(entity) == 'asset'
    entity.refresh_from_db()
    assert entity.entity_type == 'asset'


@pytest.mark.django_db
def test_type_correction_ignored_on_low_confidence():
    entity = _make_entity('Falcon 9', 'company')
    _frag(entity, 'Falcon 9 is a reusable two-stage launch vehicle.')
    client = _client_replying(_reply({'entity_type': 'asset', 'confidence': 'medium'}))
    with patch('ingest.tasks.classify.get_client', return_value=client):
        assert _validate_entity_type(entity) is None
    entity.refresh_from_db()
    assert entity.entity_type == 'company'


@pytest.mark.django_db
def test_geography_blocklist_never_corrected_away():
    entity = _make_entity('Madrid', 'geography')
    _frag(entity, 'Madrid hosts a major aerospace cluster with Airbus and GMV sites.')
    with patch('ingest.tasks.classify.get_client') as get_client:
        assert _validate_entity_type(entity) is None
        get_client.assert_not_called()


@pytest.mark.django_db
def test_thin_evidence_skips_audit():
    entity = _make_entity('Mystery Orbital', 'company')
    _frag(entity, 'Short.')  # < 80 chars total
    with patch('ingest.tasks.classify.get_client') as get_client:
        assert _validate_entity_type(entity) is None
        get_client.assert_not_called()


# ── collision guard: two entities sharing one exact name ────────────────────

@pytest.mark.django_db
def test_ambiguous_exact_name_adjudicates_with_context():
    """Two same-name evidence-bearing entities → LLM adjudication, not arbitrary first()."""
    a = _make_entity('Stellar', 'company')
    _frag(a, 'Space startup building smallsat buses in Turin with electric propulsion.')
    b = _make_entity('Stellar', 'company')
    _frag(b, 'Fintech firm operating a blockchain payments network for banks.')
    client = _client_replying(_reply({'match': 2, 'confidence': 'high'}))
    with patch('ingest.ai.get_client', return_value=client):
        resolved = resolve_mention('Stellar', entity_type='company',
                                   subject_context='Researching: Stellar (payments network)')
    # the LLM was asked, and BOTH distinctive contexts were in the prompt
    client.chat.completions.create.assert_called_once()
    prompt = client.chat.completions.create.call_args.kwargs['messages'][1]['content']
    assert 'smallsat' in prompt and 'blockchain' in prompt
    assert resolved in {str(a.id), str(b.id)}


@pytest.mark.django_db
def test_ambiguous_exact_name_unresolved_falls_through_to_stub():
    a = _make_entity('Stellar', 'company')
    _frag(a, 'Space startup building smallsat buses. ' * 3)
    b = _make_entity('Stellar', 'company')
    _frag(b, 'Fintech firm operating a payments network. ' * 3)
    client = _client_replying(
        _reply({'match': None, 'confidence': 'low'}),    # collision adjudication → undecided
        _reply({'canonical': None, 'confidence': 'low'}),  # L5 → unknown
    )
    with patch('ingest.ai.get_client', return_value=client), \
         patch('ingest.tasks.resolve._create_stub', return_value='stub-id-789') as stub:
        resolved = resolve_mention('Stellar', entity_type='company')
    assert resolved == 'stub-id-789'
    stub.assert_called_once()
    assert resolved not in {str(a.id), str(b.id)}


@pytest.mark.django_db
def test_ambiguous_alias_adjudicated_never_arbitrary():
    a = _make_entity('Stellar Space', 'company')
    _frag(a, 'Smallsat manufacturer based in Italy.')
    b = _make_entity('Stellar Pay', 'company')
    _frag(b, 'Cross-border payments platform.')
    EntityAlias.objects.create(entity=a, alias='Stellar', alias_norm='stellar', alias_kind='trading')
    EntityAlias.objects.create(entity=b, alias='Stellar', alias_norm='stellar', alias_kind='trading')
    client = _client_replying(_reply({'match': 1, 'confidence': 'high'}))
    with patch('ingest.ai.get_client', return_value=client):
        resolved = resolve_mention('Stellar', entity_type='company')
    client.chat.completions.create.assert_called_once()
    assert resolved in {str(a.id), str(b.id)}


@pytest.mark.django_db
def test_multi_stub_alias_hint_suppressed():
    """Two stubs owning one alias = collision debris — never a routing hint.

    The suppressed hint falls through to L5 (world knowledge, returns unknown
    here) and then stub creation — never to an arbitrary same-name stub.
    """
    s1 = _make_entity('Stellar', 'company', with_evidence=False, status='stub')
    s2 = _make_entity('Stellar', 'company', with_evidence=False, status='stub')
    EntityAlias.objects.create(entity=s1, alias='Stellar', alias_norm='stellar', alias_kind='trading')
    EntityAlias.objects.create(entity=s2, alias='Stellar', alias_norm='stellar', alias_kind='trading')
    client = _client_replying(_reply({'canonical': None, 'confidence': 'low'}))
    with patch('ingest.ai.get_client', return_value=client), \
         patch('ingest.tasks.resolve._create_stub', return_value='stub-id-999') as stub:
        resolved = resolve_mention('Stellar', entity_type='company')
    assert resolved == 'stub-id-999'
    stub.assert_called_once()
    # exactly one LLM call (L5) — no adjudication of the stub collision, no
    # arbitrary routing to s1/s2
    client.chat.completions.create.assert_called_once()
    assert resolved not in {str(s1.id), str(s2.id)}


@pytest.mark.django_db
def test_single_name_match_still_routes_without_llm():
    """Collision guard must not disturb the unambiguous hot path."""
    entity = _make_entity('Stellar', 'company')
    _frag(entity, 'Smallsat bus manufacturer.')
    with patch('ingest.ai.get_client') as get_client:
        resolved = resolve_mention('Stellar', entity_type='company')
    assert resolved == str(entity.id)
    get_client.assert_not_called()
