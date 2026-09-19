"""
One-time cleanup: find duplicate Event rows (same entity + type + title + date),
synthesise their descriptions with AI_MODEL_FAST, keep the oldest row, merge
participants, and delete the redundant rows.

Usage:
    python manage.py deduplicate_events [--dry-run]
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count, Min


class Command(BaseCommand):
    help = 'Merge duplicate event rows and synthesise their descriptions.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Print what would be merged without writing to the DB.',
        )

    def handle(self, *args, **options):
        from core.models import Event
        from ingest.tasks.research import _synthesise_event_description

        dry_run = options['dry_run']

        # Find groups with more than one row
        dupes = (
            Event.objects
            .values('entity_id', 'event_type', 'title', 'date')
            .annotate(cnt=Count('id'), oldest=Min('id'))
            .filter(cnt__gt=1)
            .order_by('-cnt')
        )

        total_groups = dupes.count()
        self.stdout.write(f'Found {total_groups} duplicate group(s).')
        if total_groups == 0:
            return

        merged = deleted = 0

        for group in dupes:
            rows = list(
                Event.objects
                .filter(
                    entity_id=group['entity_id'],
                    event_type=group['event_type'],
                    title=group['title'],
                    date=group['date'],
                )
                .prefetch_related('participants')
                .order_by('id')  # oldest first
            )

            keeper = rows[0]
            duplicates = rows[1:]

            # Collect all descriptions and participants across the group
            all_descriptions = [keeper.description] + [r.description for r in duplicates]
            all_participants = list(keeper.participants.all())
            for r in duplicates:
                for p in r.participants.all():
                    if p not in all_participants:
                        all_participants.append(p)

            # Synthesise all descriptions into one, compounding sources
            synthesised = all_descriptions[0]
            for extra_desc in all_descriptions[1:]:
                if extra_desc and extra_desc != synthesised:
                    synthesised = _synthesise_event_description(
                        synthesised, extra_desc, keeper.title
                    )

            self.stdout.write(
                f'  [{keeper.entity_id}] {keeper.date} {keeper.event_type} '
                f'"{keeper.title[:60]}" — merging {len(duplicates) + 1} rows'
            )

            if not dry_run:
                with transaction.atomic():
                    keeper.description = synthesised
                    keeper.save(update_fields=['description'])
                    keeper.participants.set(all_participants)
                    for r in duplicates:
                        r.delete()

            merged += 1
            deleted += len(duplicates)

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'Dry run — would merge {merged} group(s), delete {deleted} row(s).'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Done — merged {merged} group(s), deleted {deleted} duplicate row(s).'
            ))
