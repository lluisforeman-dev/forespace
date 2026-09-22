"""
Backfill lat/lon for geography entities that were created before geocoding was added.
Usage: python manage.py geocode_geography [--limit N]
Nominatim rate limit: 1 req/sec.
"""
import time
import logging
import requests

from django.core.management.base import BaseCommand
from core.models import Entity

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Geocode geography entities missing lat/lon via Nominatim'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=500)

    def handle(self, *args, **options):
        qs = Entity.objects.filter(entity_type='geography', latitude__isnull=True)[:options['limit']]
        total = len(qs)
        self.stdout.write(f'Geocoding {total} geography entities...')
        done = failed = 0
        for ent in qs:
            try:
                r = requests.get(
                    'https://nominatim.openstreetmap.org/search',
                    params={'q': ent.canonical_name, 'format': 'json', 'limit': 1},
                    headers={'User-Agent': 'ForeSpace/1.0 (space-industry knowledge graph)'},
                    timeout=5,
                )
                results = r.json()
                if results:
                    ent.latitude  = float(results[0]['lat'])
                    ent.longitude = float(results[0]['lon'])
                    ent.save(update_fields=['latitude', 'longitude'])
                    done += 1
                else:
                    failed += 1
            except Exception as exc:
                logger.warning('geocode_geography: "%s": %s', ent.canonical_name, exc)
                failed += 1
            time.sleep(1.1)  # Nominatim 1 req/sec policy

        self.stdout.write(self.style.SUCCESS(f'Done: {done} geocoded, {failed} not found'))
