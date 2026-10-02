"""Backfill: normalize existing headquarters_country values to ISO 3166-1 alpha-2.

New extractions normalize at write time (see core.normalize.normalize_country
and the write paths in extract/research). This command brings existing rows in
line. Preview by default; --apply writes.

Usage:
    python manage.py normalize_countries            # show what would change
    python manage.py normalize_countries --apply
"""
from django.core.management.base import BaseCommand

from core.models import Assertion
from core.normalize import normalize_country


class Command(BaseCommand):
    help = 'Normalize headquarters_country assertion values to ISO 3166-1 alpha-2.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write changes (default: preview only).')

    def handle(self, *args, **options):
        rows = (
            Assertion.objects
            .filter(attribute_id='headquarters_country', superseded_at__isnull=True)
            .exclude(value_text='')
            .select_related('entity')
            .order_by('entity_id', '-confidence')
        )

        changed = unchanged = 0
        seen_entities: set = set()
        for a in rows:
            norm = normalize_country(a.value_text)
            if norm is None or norm == a.value_text:
                unchanged += 1
                continue
            # Only the best assertion per entity gets rewritten in preview output,
            # but apply rewrites every row (candidates included).
            if not options['apply']:
                if a.entity_id in seen_entities:
                    continue
                seen_entities.add(a.entity_id)
                self.stdout.write(f'  {a.entity.canonical_name[:40]:<40} '
                                  f'"{a.value_text}" → "{norm}"')
            else:
                a.value_text = norm
                a.save(update_fields=['value_text'])
            changed += 1

        verb = 'Normalized' if options['apply'] else 'Would normalize'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {changed} assertion(s); {unchanged} already ISO or unrecognised '
            '(left untouched — never guess).'
        ))
        if not options['apply']:
            self.stdout.write(self.style.WARNING('Dry run — pass --apply to write.'))
