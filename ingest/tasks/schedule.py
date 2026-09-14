"""Priority scheduler — §10.

Scores all active ScheduledSources and dispatches ingestion jobs for those that are due.
Runs as a Render cron job (see render.yaml) via `manage.py run_scheduled_sources`,
OR can be called as a Celery task from the REPL.

Priority function (§10):
    p *= days_stale / mean_volatility      # older data = higher urgency
    p *= 2.0 if watchlist                  # hand-curated priority boost
    p *= 0.3 if dormant                    # quiet entities de-prioritised
    p *= log1p(ref_count)                  # well-connected = more valuable
"""
from __future__ import annotations

import logging
from datetime import timedelta
from math import log1p

from celery import shared_task
from django.db.models import Max
from django.utils import timezone

from core.models import Assertion, Entity, ScheduledSource

logger = logging.getLogger(__name__)

_CADENCE_DAYS = {'daily': 1, 'weekly': 7, 'monthly': 30, 'event_driven': 0}
_DEFAULT_VOLATILITY = 180  # days


def entity_priority(entity: Entity) -> float:
    """Higher score = more urgent to check today."""
    last_obs = entity.assertions.aggregate(m=Max('observed_at'))['m']
    if last_obs:
        days_stale = (timezone.now() - last_obs).days
    else:
        days_stale = 365

    p = days_stale / _DEFAULT_VOLATILITY
    p *= 2.0 if entity.watchlist else 1.0
    p *= 0.3 if entity.status == 'dormant' else 1.0
    p *= log1p(entity.assertions.count())
    return max(p, 0.01)


def get_due_sources(limit: int = 200) -> list[ScheduledSource]:
    """Return ScheduledSources that are due for a check, sorted by entity priority."""
    now = timezone.now()
    due = []
    for sched in ScheduledSource.objects.filter(is_active=True).select_related('source', 'entity_hint'):
        cadence = _CADENCE_DAYS.get(sched.cadence, 7)
        if sched.last_checked_at and (now - sched.last_checked_at).days < cadence:
            continue
        prio = entity_priority(sched.entity_hint) if sched.entity_hint else 1.0
        due.append((prio, sched))

    due.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in due[:limit]]


@shared_task(bind=True, queue='crawl')
def dispatch_scheduled_sources(self, limit: int = 200):
    """Celery-callable version of the scheduler. Can also be called from management command."""
    from ingest.tasks.rss import ingest_rss_feed
    from ingest.tasks.crawl import crawl_url

    sources = get_due_sources(limit)
    dispatched = 0
    for sched in sources:
        if sched.feed_type == 'rss':
            ingest_rss_feed.delay(sched.id)
        else:
            crawl_url.delay(
                sched.feed_url,
                sched.source.name,
                sched.source.kind,
                sched.source.base_trust,
            )
        dispatched += 1

    logger.info('schedule: dispatched %d source checks', dispatched)
    return {'dispatched': dispatched}
