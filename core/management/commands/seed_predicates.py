"""Seed PredicateDef rows — the controlled vocabulary for graph edges."""
from django.core.management.base import BaseCommand
from core.models import PredicateDef

PREDICATES = [
    {
        'key': 'invested_in',
        'label': 'Invested In',
        'description': (
            'Subject (investor/fund) made an equity investment in Object (company). '
            'Qualifiers: amount_usd, round_series, round_date, stake_pct.'
        ),
    },
    {
        'key': 'acquired',
        'label': 'Acquired',
        'description': (
            'Subject acquired Object (full acquisition or majority stake). '
            'Qualifiers: amount_usd, date, stake_pct.'
        ),
    },
    {
        'key': 'subsidiary_of',
        'label': 'Subsidiary Of',
        'description': 'Subject is a legal subsidiary or division of Object.',
    },
    {
        'key': 'supplies',
        'label': 'Supplies',
        'description': (
            'Subject supplies a product or component to Object. '
            'Qualifiers: product, contract_value_usd, contract_date.'
        ),
    },
    {
        'key': 'launches_for',
        'label': 'Launches For',
        'description': (
            'Subject (launch provider) launches payloads for Object (satellite operator or customer). '
            'Qualifiers: vehicle, date, payload_kg.'
        ),
    },
    {
        'key': 'partnered_with',
        'label': 'Partnered With',
        'description': (
            'Subject and Object have a formal commercial or technical partnership. '
            'Symmetric. Qualifiers: type, date, description.'
        ),
    },
    {
        'key': 'founded_by',
        'label': 'Founded By',
        'description': 'Subject (organisation) was co-founded by Object (person).',
    },
    {
        'key': 'employs',
        'label': 'Employs',
        'description': 'Subject (organisation) currently employs Object (person) in a named role. Qualifiers: role, since.',
    },
    {
        'key': 'competes_with',
        'label': 'Competes With',
        'description': 'Subject and Object are direct competitors in the same market segment. Symmetric.',
    },
    {
        'key': 'licensed_by',
        'label': 'Licensed By',
        'description': 'Subject holds a regulatory licence issued by Object (regulator). Qualifiers: licence_type, country.',
    },
    {
        'key': 'launched_payload',
        'label': 'Launched Payload',
        'description': 'Subject (launch vehicle / event) placed Object (satellite / payload) in orbit. Qualifiers: orbit, date.',
    },
]


class Command(BaseCommand):
    help = 'Seed PredicateDef rows (relation vocabulary).'

    def add_arguments(self, parser):
        parser.add_argument('--overwrite', action='store_true')

    def handle(self, *args, **options):
        created = updated = skipped = 0
        for spec in PREDICATES:
            obj, is_new = PredicateDef.objects.get_or_create(
                key=spec['key'],
                defaults={'label': spec['label'], 'description': spec['description']},
            )
            if is_new:
                created += 1
            elif options['overwrite']:
                obj.label = spec['label']
                obj.description = spec['description']
                obj.save()
                updated += 1
            else:
                skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f'seed_predicates: {created} created, {updated} updated, {skipped} skipped.'
        ))
