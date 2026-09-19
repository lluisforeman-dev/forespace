"""
Re-queue funding_program entities for research when their data is stale.

Staleness criteria (either triggers a refresh):
  1. No research in the last STALE_DAYS days (default 30).
  2. Their most recent grant_call event has a deadline that has already passed
     — meaning the next call may now be open and we need updated data.

Usage:
    python manage.py refresh_funding_programs [--dry-run] [--stale-days N]

Designed to be run weekly via Render cron:
    schedule: "0 6 * * 1"   # every Monday at 06:00 UTC
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Max
from django.utils import timezone


class Command(BaseCommand):
    help = 'Re-queue stale funding_program entities for research.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--stale-days', type=int, default=30)

    def handle(self, *args, **options):
        from core.models import Assertion, Entity, Event
        from ingest.tasks.research import research_topic

        dry_run = options['dry_run']
        stale_days = options['stale_days']
        cutoff = timezone.now() - timedelta(days=stale_days)
        today = timezone.now().date()

        programs = Entity.objects.filter(
            entity_type='funding_program',
            status__in=('active', 'stub'),
        )

        queued = 0
        for prog in programs:
            # Criterion 1: not researched recently
            last_obs = (
                Assertion.objects
                .filter(entity=prog)
                .aggregate(m=Max('observed_at'))['m']
            )
            stale = not last_obs or last_obs < cutoff

            # Criterion 2: most recent grant_call deadline has passed
            latest_call = (
                Event.objects
                .filter(entity=prog, event_type='grant_call', date__isnull=False)
                .order_by('-date')
                .values_list('date', flat=True)
                .first()
            )
            deadline_passed = latest_call and latest_call < today

            if stale or deadline_passed:
                reason = []
                if stale:
                    reason.append(f'stale (last obs: {last_obs.date() if last_obs else "never"})')
                if deadline_passed:
                    reason.append(f'deadline passed ({latest_call})')
                self.stdout.write(f'  {prog.canonical_name} — {", ".join(reason)}')
                if not dry_run:
                    research_topic.delay(prog.canonical_name, topic_type='funding_program')
                queued += 1

        verb = 'Would queue' if dry_run else 'Queued'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {queued} funding programme(s) for refresh.'
        ))
