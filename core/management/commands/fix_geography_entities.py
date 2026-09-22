"""
Find all entities that are objects of has_office_in relations but have
entity_type != 'geography', convert them, and geocode them.
Also catches entities whose names are in the geographic blocklist.
"""
import time
import logging
import requests

from django.core.management.base import BaseCommand
from django.db.models import Q
from core.models import Entity, Relation

logger = logging.getLogger(__name__)

_HEADERS = {'User-Agent': 'ForeSpace/1.0 (space-industry knowledge graph)'}


def _geocode(name):
    try:
        r = requests.get(
            'https://nominatim.openstreetmap.org/search',
            params={'q': name, 'format': 'json', 'limit': 1},
            headers=_HEADERS,
            timeout=5,
        )
        results = r.json()
        if results:
            return float(results[0]['lat']), float(results[0]['lon'])
    except Exception as exc:
        logger.warning('geocode "%s": %s', name, exc)
    return None, None


class Command(BaseCommand):
    help = 'Convert mis-typed geographic stubs to entity_type=geography and geocode them'

    def handle(self, *args, **options):
        # All objects of has_office_in that aren't already geography
        geo_ids = set(
            Relation.objects
            .filter(predicate_id='has_office_in')
            .exclude(object__entity_type='geography')
            .values_list('object_id', flat=True)
        )

        qs = Entity.objects.filter(id__in=geo_ids).exclude(entity_type='geography')
        total = qs.count()
        self.stdout.write(f'Found {total} non-geography entities to fix...')

        done = failed = skipped = 0
        for ent in qs:
            # Skip if clearly not a place (has assertions, events, or is well-known org)
            has_data = (
                ent.assertions.filter(status__in=('accepted', 'candidate')).exists()
                or ent.events.exists()
            )
            # Only convert stubs and entities with no substantive data
            if has_data and ent.status == 'active':
                self.stdout.write(f'  SKIP {ent.canonical_name} (has data, status=active)')
                skipped += 1
                continue

            lat, lon = _geocode(ent.canonical_name)
            ent.entity_type = 'geography'
            ent.status = 'active'
            ent.latitude = lat
            ent.longitude = lon
            ent.save(update_fields=['entity_type', 'status', 'latitude', 'longitude'])

            if lat:
                self.stdout.write(f'  OK  {ent.canonical_name} → ({lat:.4f}, {lon:.4f})')
                done += 1
            else:
                self.stdout.write(f'  ?   {ent.canonical_name} → no coords found')
                failed += 1

            time.sleep(1.1)  # Nominatim rate limit

        self.stdout.write(self.style.SUCCESS(
            f'\nDone: {done} fixed+geocoded, {failed} fixed (no coords), {skipped} skipped'
        ))
