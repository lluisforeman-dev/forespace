"""Enrich subsidiary_of relations from Wikidata corporate hierarchies."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ('Link ownership structures: for Wikidata-anchored entities, materialise '
            'P127/P355 claims as subsidiary_of relations when both endpoints exist locally.')

    def add_arguments(self, parser):
        parser.add_argument('--batch', type=int, default=100,
                            help='Max wikidata-anchored entities to process')
        parser.add_argument('--sleep', type=float, default=1.0,
                            help='Seconds between Wikidata API chunks')

    def handle(self, *args, **options):
        from ingest.tasks.ownership import run_ownership_enrichment
        stats = run_ownership_enrichment(
            batch_size=options['batch'], sleep_s=options['sleep'],
        )
        self.stdout.write(self.style.SUCCESS(f'done: {stats}'))
