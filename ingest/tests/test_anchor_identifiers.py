"""Wikidata anchoring — the deterministic cross-language dedup bridge.

Anchors must survive real-world name variants: parenthetical acronyms in
canonical names, and registry labels that match an ALIAS rather than the
canonical form (English name vs Catalan name of the same institute).
"""
from unittest.mock import patch

import pytest

from core.models import Entity, EntityAlias


def _ieec_entity():
    e = Entity.objects.create(
        entity_type='company', status='active', space_relevance=100,
        canonical_name="Institut d'Estudis Espacials de Catalunya (IEEC)",
        slug='ieec',
    )
    EntityAlias.objects.create(
        entity=e, alias="Institut d'Estudis Espacials de Catalunya",
        alias_norm='institut d estudis espacials de catalunya', alias_kind='trading',
    )
    EntityAlias.objects.create(entity=e, alias='IEEC', alias_norm='ieec', alias_kind='acronym')
    return e


@pytest.mark.django_db
def test_anchor_matches_alias_and_strips_parenthetical():
    """Wikidata's label is the Catalan base name — an ALIAS of our entity whose
    canonical carries '(IEEC)'. The anchor must match it via the alias, with
    the parenthetical stripped, even though the description has no space hint."""
    e = _ieec_entity()
    cand = {
        'id': 'Q123456',
        'label': "Institut d'Estudis Espacials de Catalunya",
        'aliases': ['IEEC', 'Institute of Space Studies of Catalonia'],
        'description': 'research institute in Catalonia',
    }
    with patch('core.management.commands.anchor_identifiers._search_wikidata',
               return_value=[cand]):
        from core.management.commands.anchor_identifiers import _anchor_wikidata
        assert _anchor_wikidata(e) == 'Q123456'


@pytest.mark.django_db
def test_anchor_skips_ambiguous_matches():
    """Two plausible candidates matching different name forms of the same
    entity = ambiguous. A missing anchor is cheap; a wrong anchor poisons
    dedup forever."""
    e = _ieec_entity()
    cands = [
        {'id': 'Q1', 'label': "Institut d'Estudis Espacials de Catalunya",
         'aliases': [], 'description': 'research institute in Catalonia'},
        {'id': 'Q2', 'label': 'IEEC',
         'aliases': [], 'description': 'space studies centre'},
    ]
    with patch('core.management.commands.anchor_identifiers._search_wikidata',
               return_value=cands):
        from core.management.commands.anchor_identifiers import _anchor_wikidata
        assert _anchor_wikidata(e) == ''


@pytest.mark.django_db
def test_anchor_requires_plausibility():
    """A label match alone is not enough: without space hints in the
    description, mid-relevance entities are not anchored."""
    e = _ieec_entity()
    e.space_relevance = 50
    e.save()
    cand = {
        'id': 'Q999',
        'label': "Institut d'Estudis Espacials de Catalunya",
        'aliases': [],
        'description': 'research institute in Catalonia',
    }
    with patch('core.management.commands.anchor_identifiers._search_wikidata',
               return_value=[cand]):
        from core.management.commands.anchor_identifiers import _anchor_wikidata
        assert _anchor_wikidata(e) == ''
