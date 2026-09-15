"""Warm the analytics snapshot cache — run by cron every 6 hours."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Recompute and cache the analytics dashboard snapshot.'

    def handle(self, *args, **options):
        from ingest.tasks.analytics import build_analytics_snapshot
        snapshot = build_analytics_snapshot()
        self.stdout.write(self.style.SUCCESS(
            f'warm_analytics_cache: snapshot computed. '
            f'{sum(sum(v.values()) for v in snapshot.get("entity_counts", {}).values())} entities.'
        ))
