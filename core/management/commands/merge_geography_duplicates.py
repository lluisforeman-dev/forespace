"""
management command: merge_geography_duplicates

Finds geography entities whose geocoordinates are within 0.005° of each other
(~500m — same city, different language spellings) and merges duplicates into
the canonical entity that has more relation objects.

Usage:
    python manage.py merge_geography_duplicates [--dry-run]
"""
import logging
from django.core.management.base import BaseCommand
from django.db import transaction
from core.models import Entity, EntityAlias, RelationAssertion

logger = logging.getLogger(__name__)

THRESHOLD = 0.005  # degrees ≈ 500m


class Command(BaseCommand):
    help = 'Merge geography entities that are within 0.005° of each other'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Show what would be merged without changing the database')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        rows = list(
            Entity.objects
            .filter(entity_type='geography', latitude__isnull=False, status='active')
            .values_list('id', 'canonical_name', 'latitude', 'longitude')
            .order_by('created_at')  # older entity preferred as canonical
        )
        self.stdout.write(f'Loaded {len(rows)} active geography entities with coordinates')

        merged_ids = set()
        pairs = []

        for i, (id1, name1, lat1, lon1) in enumerate(rows):
            if id1 in merged_ids:
                continue
            for id2, name2, lat2, lon2 in rows[i + 1:]:
                if id2 in merged_ids:
                    continue
                dlat = abs(float(lat1) - float(lat2))
                dlon = abs(float(lon1) - float(lon2))
                if dlat < THRESHOLD and dlon < THRESHOLD:
                    pairs.append((id1, name1, id2, name2))
                    merged_ids.add(id2)

        if not pairs:
            self.stdout.write(self.style.SUCCESS('No duplicates found.'))
            return

        self.stdout.write(f'Found {len(pairs)} duplicate pair(s):')
        for id1, name1, id2, name2 in pairs:
            self.stdout.write(f'  KEEP "{name1}" ({id1})  <-  MERGE "{name2}" ({id2})')

        if dry_run:
            self.stdout.write(self.style.WARNING('Dry run — no changes made.'))
            return

        with transaction.atomic():
            for keep_id, keep_name, dup_id, dup_name in pairs:
                dup = Entity.objects.get(id=dup_id)

                # Redirect relation assertions that reference the duplicate as object
                ra_count = RelationAssertion.objects.filter(object_entity_id=dup_id).update(object_entity_id=keep_id)

                # Redirect aliases
                for alias in EntityAlias.objects.filter(entity_id=dup_id):
                    exists = EntityAlias.objects.filter(entity_id=keep_id, normalized=alias.normalized).exists()
                    if not exists:
                        alias.entity_id = keep_id
                        alias.save(update_fields=['entity'])
                    else:
                        alias.delete()

                # Add the duplicate's canonical name as alias on the keeper
                from ingest.tasks.resolve import _add_alias, _normalize_mention
                norm = _normalize_mention(dup_name)
                if not EntityAlias.objects.filter(entity_id=keep_id, normalized=norm).exists():
                    _add_alias(str(keep_id), dup_name, norm, None)

                # Deactivate the duplicate
                dup.status = 'merged'
                dup.save(update_fields=['status'])

                self.stdout.write(
                    self.style.SUCCESS(
                        f'  Merged "{dup_name}" into "{keep_name}" '
                        f'(redirected {ra_count} relations)'
                    )
                )

        self.stdout.write(self.style.SUCCESS(f'Done. {len(pairs)} geography entities merged.'))
