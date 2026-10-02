"""Dedup sweep pairing — word-boundary containment catches short-form/long-form
false splits ('Starliner' vs 'CST-100 Starliner') that trigram similarity misses.
Containment pairs carry LLM-band similarity (0.75): adjudicated, never auto-merged."""
import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

from core.models import Entity, EntityAlias


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
