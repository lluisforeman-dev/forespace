"""Tests for the L5 verification gate — cross-domain name-collision defence.

L5 maps a mention to a canonical NAME via world knowledge, then merges into
whichever entity carries that name. When one name is shared across domains
(SpaceX's "Starship" launch vehicle vs Starship Technologies' delivery
robots), the merge must be verified against the entity's actual context.
"""
import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

from core.models import Entity, KnowledgeFragment
from ingest.tasks.resolve import _entity_has_evidence, _llm_verify_entity


def _make_entity(with_evidence=True, name='Starship', etype='asset'):
    entity = Entity.objects.create(
        entity_type=etype,
        canonical_name=name,
        slug=f'{name.lower().replace(" ", "-")}-{uuid.uuid4().hex[:8]}',
        status='active',
    )
    if with_evidence:
        KnowledgeFragment.objects.create(
            entity=entity,
            category='technical',
            confidence=80,
            text='Fully reusable two-stage launch vehicle powered by Raptor engines.',
        )
    return entity


def _client_replying(match, confidence):
    client = MagicMock()
    resp = MagicMock()
    resp.choices[0].message.content = json.dumps({'match': match, 'confidence': confidence})
    resp.usage.prompt_tokens = 120
    resp.usage.completion_tokens = 12
    client.chat.completions.create.return_value = resp
    return client


@pytest.mark.django_db
def test_husk_entity_skips_verification_and_merges():
    """No evidence → nothing to clash with → merge without an LLM call."""
    entity = _make_entity(with_evidence=False)
    with patch('ingest.ai.get_client') as get_client:
        assert _llm_verify_entity('Starship', 'asset', '', str(entity.id)) is True
        get_client.assert_not_called()


@pytest.mark.django_db
def test_evidence_detected_via_fragment():
    husk = _make_entity(with_evidence=False)
    fed = _make_entity()
    assert _entity_has_evidence(str(husk.id)) is False
    assert _entity_has_evidence(str(fed.id)) is True


@pytest.mark.django_db
def test_verified_match_merges():
    entity = _make_entity()
    with patch('ingest.ai.get_client', return_value=_client_replying(True, 'high')):
        assert _llm_verify_entity('Starship', 'asset', '', str(entity.id)) is True


@pytest.mark.django_db
def test_cross_domain_collision_rejected():
    """Mention is the delivery robot; the graph entity is SpaceX's vehicle → reject."""
    entity = _make_entity()
    with patch('ingest.ai.get_client', return_value=_client_replying(False, 'high')):
        assert _llm_verify_entity('Starship e-model delivery robot', 'asset', '', str(entity.id)) is False


@pytest.mark.django_db
def test_low_confidence_fails_closed():
    entity = _make_entity()
    with patch('ingest.ai.get_client', return_value=_client_replying(True, 'low')):
        assert _llm_verify_entity('Starship', 'asset', '', str(entity.id)) is False


@pytest.mark.django_db
def test_empty_reply_fails_closed():
    entity = _make_entity()
    client = MagicMock()
    resp = MagicMock()
    resp.choices[0].message.content = ''
    client.chat.completions.create.return_value = resp
    with patch('ingest.ai.get_client', return_value=client):
        assert _llm_verify_entity('Starship', 'asset', '', str(entity.id)) is False


@pytest.mark.django_db
def test_llm_error_fails_closed():
    entity = _make_entity()
    with patch('ingest.ai.get_client', side_effect=RuntimeError('LLM down')):
        assert _llm_verify_entity('Starship', 'asset', '', str(entity.id)) is False
