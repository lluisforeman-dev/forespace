"""Fresh-research guard: redelivered duplicates must not re-burn tokens.

A worker restart (every deploy) redelivers queued Celery tasks; a research
run that already completed for the same topic+frame within the cooldown is
skipped instead of re-run at full price.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from core.models import ExtractionRun


def _seed_run(task, topic, age_minutes):
    """Seed a completed run; started_at is auto_now_add, so backdate after."""
    run = ExtractionRun.objects.create(
        task=task,
        prompt_sha256='x' * 64,
        status='completed',
        stats={'topic': topic, 'accepted': 12},
    )
    ExtractionRun.objects.filter(pk=run.pk).update(
        started_at=timezone.now() - timedelta(minutes=age_minutes),
    )
    return run


@pytest.mark.django_db
def test_research_skips_recently_completed_topic():
    _seed_run('research_company', 'Open Cosmos', age_minutes=30)
    from ingest.tasks.research import research_topic
    with patch('ingest.tasks.research._attr_vocab', return_value={'k': 'v'}), \
         patch('ingest.tasks.research._predicate_vocab', return_value='p'):
        research_topic('open cosmos', 'company')

    # no second run row created
    assert ExtractionRun.objects.filter(task='research_company').count() == 1


@pytest.mark.django_db
def test_research_proceeds_when_last_run_is_old():
    _seed_run('research_company', 'Open Cosmos', age_minutes=8 * 60)
    from ingest.tasks import research as rmod
    # Reaching run creation proves the guard passed; abort there.
    with patch('ingest.tasks.research._attr_vocab', return_value={'k': 'v'}), \
         patch('ingest.tasks.research._predicate_vocab', return_value='p'), \
         patch.object(rmod.ExtractionRun.objects, 'create',
                      side_effect=AssertionError('reached run creation - guard passed')):
        with pytest.raises(AssertionError, match='guard passed'):
            rmod.research_topic('Open Cosmos', 'company')


@pytest.mark.django_db
def test_research_guard_is_per_frame():
    """A company run does not block an asset-frame run on the same topic."""
    _seed_run('research_company', 'Starship', age_minutes=30)
    from ingest.tasks import research as rmod
    with patch('ingest.tasks.research._attr_vocab', return_value={'k': 'v'}), \
         patch('ingest.tasks.research._predicate_vocab', return_value='p'), \
         patch.object(rmod.ExtractionRun.objects, 'create',
                      side_effect=AssertionError('reached run creation - guard passed')):
        with pytest.raises(AssertionError, match='guard passed'):
            rmod.research_topic('Starship', 'asset')


@pytest.mark.django_db
def test_research_guard_ignores_failed_runs():
    """A failed run must not suppress the retry."""
    run = ExtractionRun.objects.create(
        task='research_company',
        prompt_sha256='x' * 64,
        status='failed',
        stats={'topic': 'Open Cosmos'},
    )
    ExtractionRun.objects.filter(pk=run.pk).update(
        started_at=timezone.now() - timedelta(minutes=5),
    )
    from ingest.tasks import research as rmod
    with patch('ingest.tasks.research._attr_vocab', return_value={'k': 'v'}), \
         patch('ingest.tasks.research._predicate_vocab', return_value='p'), \
         patch.object(rmod.ExtractionRun.objects, 'create',
                      side_effect=AssertionError('reached run creation - guard passed')):
        with pytest.raises(AssertionError, match='guard passed'):
            rmod.research_topic('Open Cosmos', 'company')
