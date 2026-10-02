"""Tests for the knowledge-construction primitives — independence rules."""
from types import SimpleNamespace

from ingest.confidence import document_domain, documents_independent


def _doc(pk=1, url='https://spacenews.com/article-1', sha=None, domain='spacenews.com'):
    if sha is None:
        sha = f'sha-{pk}'  # unique per document by default
    return SimpleNamespace(pk=pk, url=url, content_sha256=sha,
                           source=SimpleNamespace(domain=domain))


# ── document_domain ──────────────────────────────────────────────────────────

def test_domain_from_url():
    assert document_domain(_doc(url='https://www.reuters.com/x')) == 'reuters.com'

def test_domain_falls_back_to_source():
    assert document_domain(_doc(url=None, domain='nasa.gov')) == 'nasa.gov'

def test_domain_empty_when_nothing_known():
    assert document_domain(_doc(url=None, domain='')) == ''


# ── documents_independent ────────────────────────────────────────────────────

def test_same_document_is_not_independent():
    d = _doc(pk=1)
    assert not documents_independent(d, d)

def test_same_content_hash_is_not_independent():
    # Same wire story republished at a different URL
    assert not documents_independent(
        _doc(pk=1, sha='same-wire-story'),
        _doc(pk=2, sha='same-wire-story'),
    )

def test_same_domain_is_not_independent():
    # SpaceNews printing the story twice is one source
    assert not documents_independent(
        _doc(pk=1, url='https://spacenews.com/a'),
        _doc(pk=2, url='https://spacenews.com/b'),
    )

def test_www_stripped_for_domain_comparison():
    assert not documents_independent(
        _doc(pk=1, url='https://spacenews.com/a'),
        _doc(pk=2, url='https://www.spacenews.com/b'),
    )

def test_different_domains_are_independent():
    assert documents_independent(
        _doc(pk=1, url='https://spacenews.com/a'),
        _doc(pk=2, url='https://reuters.com/b'),
    )

def test_unknown_provenance_grants_no_credit():
    # A document with no domain on either side cannot corroborate
    assert not documents_independent(
        _doc(pk=1, url='https://spacenews.com/a'),
        _doc(pk=2, url=None, domain=''),
    )

def test_none_documents():
    assert not documents_independent(None, _doc())
    assert not documents_independent(_doc(), None)


# ── non-merge reopen rule ────────────────────────────────────────────────────

from ingest.tasks.dedup import _should_reopen  # noqa: E402


def test_reopen_when_decided_in_ignorance():
    # Decided when one side had no evidence, now it has plenty → reopen
    assert _should_reopen(rec_a=0, rec_b=5, cur_a=4, cur_b=5)
    assert _should_reopen(rec_a=2, rec_b=2, cur_a=2, cur_b=7)


def test_reopen_on_major_new_evidence():
    assert _should_reopen(rec_a=5, rec_b=5, cur_a=9, cur_b=5)   # +4 on A
    assert _should_reopen(rec_a=1, rec_b=1, cur_a=1, cur_b=10)  # B went 1→10


def test_no_reopen_when_evidence_stable():
    # Same evidence base, or minor drift — the verdict stands
    assert not _should_reopen(rec_a=5, rec_b=5, cur_a=5, cur_b=5)
    assert not _should_reopen(rec_a=5, rec_b=5, cur_a=7, cur_b=5)   # +2 < 3
    assert not _should_reopen(rec_a=6, rec_b=6, cur_a=5, cur_b=6)   # shrunk


def test_reopen_threshold_is_three():
    # 'Thin at decision' means <3; current <3 never reopens on its own
    assert not _should_reopen(rec_a=0, rec_b=0, cur_a=2, cur_b=0)
    assert _should_reopen(rec_a=0, rec_b=0, cur_a=3, cur_b=0)
