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

from core.models import (
    Assertion, AttributeDef, Entity, EntityAlias, ExtractionRun, KnowledgeFragment,
)
from ingest.tasks.classify import _validate_entity_type, classify_entity
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
def test_l2a_type_mismatch_rejected_falls_back_to_namesake():
    """Guard rejection tries L4/L5 for something better, then falls back to the
    evidence-bearing namesake — never mints a same-name duplicate."""
    entity = _make_entity('Falcon 9', 'asset')
    _frag(entity, 'Reusable two-stage launch vehicle.')
    client = _client_replying(
        _reply({'match': False, 'confidence': 'high'}),   # L2a verify → different
        _reply({'canonical': None, 'confidence': 'low'}),  # L5 → unknown
    )
    with patch('ingest.ai.get_client', return_value=client), \
         patch('ingest.tasks.resolve._create_entity') as create_entity:
        resolved = resolve_mention('Falcon 9', entity_type='company')
        assert resolved == str(entity.id)
        create_entity.assert_not_called()


@pytest.mark.django_db
def test_l2b_type_mismatch_rejected_falls_back_to_owner():
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
         patch('ingest.tasks.resolve._create_entity') as create_entity:
        resolved = resolve_mention(
            'Starship e-model delivery robot', entity_type='company')
        assert resolved == str(entity.id)
        create_entity.assert_not_called()


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
def test_ambiguous_exact_name_unresolved_falls_back_not_stub():
    """Undecided collision → best-known namesake, never a third same-name entity."""
    a = _make_entity('Stellar', 'company')
    _frag(a, 'Space startup building smallsat buses. ' * 3)
    b = _make_entity('Stellar', 'company')
    _frag(b, 'Fintech firm operating a payments network. ' * 3)
    client = _client_replying(
        _reply({'match': None, 'confidence': 'low'}),    # collision adjudication → undecided
        _reply({'canonical': None, 'confidence': 'low'}),  # L5 → unknown
    )
    with patch('ingest.ai.get_client', return_value=client), \
         patch('ingest.tasks.resolve._create_entity') as create_entity:
        resolved = resolve_mention('Stellar', entity_type='company')
    assert resolved in {str(a.id), str(b.id)}
    create_entity.assert_not_called()


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
def test_same_name_data_less_entities_never_route_arbitrarily():
    """Two data-less entities sharing one name: no arbitrary routing — a new
    entity is created instead (active from birth; dedup reconciles later)."""
    s1 = _make_entity('Stellar', 'company', with_evidence=False)
    s2 = _make_entity('Stellar', 'company', with_evidence=False)
    EntityAlias.objects.create(entity=s1, alias='Stellar', alias_norm='stellar', alias_kind='trading')
    EntityAlias.objects.create(entity=s2, alias='Stellar', alias_norm='stellar', alias_kind='trading')
    client = _client_replying(
        _reply({'match': None, 'confidence': 'low'}),     # alias-collision adjudication → undecided
        _reply({'canonical': None, 'confidence': 'low'}),  # L5 → unknown
    )
    with patch('ingest.ai.get_client', return_value=client), \
         patch('ingest.tasks.resolve._create_entity', return_value='new-entity-999') as create_entity:
        resolved = resolve_mention('Stellar', entity_type='company')
    assert resolved == 'new-entity-999'
    create_entity.assert_called_once()
    # one disambiguation call + one L5 call — no arbitrary routing to s1/s2
    assert client.chat.completions.create.call_count == 2
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


# ── classify merge conditioning ──────────────────────────────────────────────

def _make_run():
    return ExtractionRun.objects.create(
        task='research_company',
        prompt_sha256='a' * 64,
        model='test-model',
        code_version='test',
        status='completed',
    )


def _make_attr():
    attr, _ = AttributeDef.objects.get_or_create(
        key='headquarters_address',
        defaults=dict(entity_type='company', label='HQ address', datatype='text'),
    )
    return attr


def _make_doc():
    """Extracted assertions require a source document (extracted_needs_source)."""
    from core.models import Document, Source
    source, _ = Source.objects.get_or_create(
        name='test-source', defaults=dict(kind='trade_press', domain='example.com'),
    )
    return Document.objects.create(
        source=source, content_sha256=uuid.uuid4().hex,
        storage_key=f'test/{uuid.uuid4().hex}', pipeline_status='extracted',
    )


def _make_assertion(entity, run, status):
    # method='extracted' has a DB check: document AND quote must be present.
    return Assertion.objects.create(
        entity=entity, attribute_id=_make_attr().key, run=run,
        document=_make_doc(), method='extracted', confidence=60,
        status=status, value_text='Calle Falsa 1, Madrid', quote='Calle Falsa 1, Madrid',
    )


@pytest.mark.django_db
def test_classify_defers_while_run_claims_pending():
    """Un-adjudicated claims from the triggering run → defer, judge nothing yet."""
    entity = _make_entity('Acme Space', 'company')
    run = _make_run()
    _make_assertion(entity, run, status='candidate')
    with patch('ingest.tasks.classify.classify_entity') as task_mock, \
         patch('ingest.tasks.classify.get_client') as get_client:
        classify_entity(str(entity.id), str(run.pk))
        task_mock.apply_async.assert_called_once()
        call_kwargs = task_mock.apply_async.call_args.kwargs
        assert call_kwargs['args'] == [str(entity.id), str(run.pk), 1]
        assert call_kwargs['countdown'] == 15
        get_client.assert_not_called()


@pytest.mark.django_db
def test_classify_proceeds_when_run_adjudicated():
    """All run claims adjudicated → no deferral, classification proceeds."""
    entity = _make_entity('Acme Space', 'company')
    run = _make_run()
    _make_assertion(entity, run, status='accepted')
    client = _client_replying(_reply({'classifications': [], 'space_relevance': 50}))
    with patch('ingest.tasks.classify.classify_entity') as task_mock, \
         patch('ingest.tasks.classify.get_client', return_value=client):
        classify_entity(str(entity.id), str(run.pk))
        task_mock.apply_async.assert_not_called()


@pytest.mark.django_db
def test_verify_fuzzy_match_saves_snippet_quotes():
    """Full-page fetches differ from search snippets in punctuation/structure —
    high token overlap must verify, not punish."""
    from core.models import Document, Source
    from ingest.tasks.verify import verify_assertion_quote
    entity = _make_entity('Rocket Lab', 'company')
    source = Source.objects.create(name='rl-test', kind='primary', domain='rocketlabcorp.com')
    doc = Document.objects.create(
        source=source,
        url='https://rocketlabcorp.com/investor-faqs',
        content_sha256=uuid.uuid4().hex,
        storage_key='test/rl',
        pipeline_status='done',
        raw_content='FAQ: Where are Rocket Lab headquarters located? The company operates '
                    'from 3881 McGowen Street, Long Beach, California 90808, USA.',
    )
    # Quote as the LLM would write it from a snippet — different phrasing order
    a = Assertion.objects.create(
        entity=entity, attribute_id=_make_attr().key, document=doc,
        method='extracted', confidence=75, status='accepted',
        value_text='Long Beach', quote="Where are Rocket Lab's headquarters? 3881 McGowen Street, Long Beach, California 90808.",
    )
    assert verify_assertion_quote(a) == 'verified'
    a.refresh_from_db()
    assert a.quote_verified is True


@pytest.mark.django_db
def test_verify_still_fails_on_absent_content():
    """Genuinely absent content must still fail — fuzzy matching is not a pass."""
    from core.models import Document, Source
    from ingest.tasks.verify import verify_assertion_quote
    entity = _make_entity('Acme', 'company')
    source = Source.objects.create(name='acme-test', kind='primary', domain='acme.test')
    doc = Document.objects.create(
        source=source, url='https://acme.test/news', content_sha256=uuid.uuid4().hex,
        storage_key='test/acme',
        pipeline_status='done',
        raw_content='Acme Corp announces a new office in Madrid, Spain for launch operations.',
    )
    a = Assertion.objects.create(
        entity=entity, attribute_id=_make_attr().key, document=doc,
        method='extracted', confidence=75, status='accepted',
        value_text='50,000 employees', quote='Acme employs 50,000 people worldwide across offices.',
    )
    assert verify_assertion_quote(a) == 'failed'


@pytest.mark.django_db
def test_classify_without_run_id_never_defers():
    """Manual/other triggers (run_id=None) keep immediate behaviour."""
    entity = _make_entity('Acme Space', 'company')
    client = _client_replying(_reply({'classifications': [], 'space_relevance': 50}))
    with patch('ingest.tasks.classify.classify_entity') as task_mock, \
         patch('ingest.tasks.classify.get_client', return_value=client):
        classify_entity(str(entity.id))
        task_mock.apply_async.assert_not_called()
