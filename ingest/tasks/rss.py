"""RSS/Atom feed ingestion.

Parses a feed URL and enqueues crawl_url for each entry not already in the DB.
"""
import logging

import feedparser
from celery import shared_task
from django.utils import timezone

from core.models import Document, ScheduledSource

logger = logging.getLogger(__name__)


@shared_task(bind=True, queue='crawl', max_retries=2, default_retry_delay=120)
def ingest_rss_feed(self, scheduled_source_id: int):
    """Parse feed and enqueue crawl_url for each new entry (up to 50 per run)."""
    try:
        sched = ScheduledSource.objects.select_related('source').get(pk=scheduled_source_id)
    except ScheduledSource.DoesNotExist:
        return

    try:
        feed = feedparser.parse(sched.feed_url)
    except Exception as exc:
        logger.warning('ingest_rss_feed: parse error %s: %s', sched.feed_url, exc)
        raise self.retry(exc=exc)

    from ingest.tasks.crawl import crawl_url

    queued = skipped = 0
    for entry in (feed.entries or [])[:50]:
        url = entry.get('link')
        if not url:
            continue
        # Skip URLs we've already fetched
        if Document.objects.filter(url=url).exists():
            skipped += 1
            continue
        crawl_url.delay(
            url,
            sched.source.name,
            sched.source.kind,
            sched.source.base_trust,
        )
        queued += 1

    ScheduledSource.objects.filter(pk=scheduled_source_id).update(last_checked_at=timezone.now())
    logger.info('ingest_rss_feed: %s → %d queued, %d skipped', sched.feed_url, queued, skipped)
    return {'queued': queued, 'skipped': skipped}
