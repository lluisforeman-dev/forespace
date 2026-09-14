"""Management command for the Render cron job (and manual runs).

Scores all due ScheduledSources by entity priority and dispatches
Celery ingestion tasks for the top N. Requires Redis (Celery broker) to be reachable.

Usage:
    python manage.py run_scheduled_sources
    python manage.py run_scheduled_sources --limit 50 --dry-run
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Dispatch ingestion tasks for all due ScheduledSources.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=200)
        parser.add_argument('--dry-run', action='store_true',
                            help='Print what would be dispatched without actually doing it.')

    def handle(self, *args, **options):
        from ingest.tasks.schedule import get_due_sources
        from ingest.tasks.rss import ingest_rss_feed
        from ingest.tasks.crawl import crawl_url

        due = get_due_sources(options['limit'])
        if not due:
            self.stdout.write('No sources due for checking.')
            return

        for sched in due:
            label = f'{sched.source.name} [{sched.feed_type}] {sched.feed_url[:60]}'
            if options['dry_run']:
                self.stdout.write(f'  DRY-RUN: {label}')
                continue
            if sched.feed_type == 'rss':
                ingest_rss_feed.delay(sched.id)
            else:
                crawl_url.delay(
                    sched.feed_url,
                    sched.source.name,
                    sched.source.kind,
                    sched.source.base_trust,
                )
            self.stdout.write(f'  Dispatched: {label}')

        self.stdout.write(self.style.SUCCESS(
            f'Done — {len(due)} source(s) dispatched.'
        ))
