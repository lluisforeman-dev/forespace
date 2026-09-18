"""Seed PredicateDef rows — the controlled vocabulary for graph edges."""
from django.core.management.base import BaseCommand
from core.models import PredicateDef

PREDICATES = [
    # ── Financial ────────────────────────────────────────────────────────
    {
        'key': 'invested_in',
        'label': 'Invested In',
        'description': (
            'Subject (investor/fund/corp) made an equity investment in Object (company). '
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
        'key': 'contracted_by',
        'label': 'Contracted By',
        'description': (
            'Subject (company) was awarded a contract by Object (agency, government, or prime contractor). '
            'Qualifiers: contract_value_usd, date, scope.'
        ),
    },
    # ── Corporate structure ───────────────────────────────────────────────
    {
        'key': 'subsidiary_of',
        'label': 'Subsidiary Of',
        'description': 'Subject is a legal subsidiary or division of Object.',
    },
    {
        'key': 'member_of',
        'label': 'Member Of',
        'description': (
            'Subject is a member of Object (consortium, standards body, joint venture, '
            'or intergovernmental organisation such as ESA). Qualifiers: role, since.'
        ),
    },
    # ── Supply chain ─────────────────────────────────────────────────────
    {
        'key': 'supplies',
        'label': 'Supplies',
        'description': (
            'Subject supplies a product, component, or subsystem to Object. '
            'Qualifiers: product, contract_value_usd, contract_date.'
        ),
    },
    {
        'key': 'manufactures',
        'label': 'Manufactures',
        'description': (
            'Subject (organisation) designs and builds Object (asset: rocket, satellite, engine, etc.). '
            'Qualifiers: model, first_flight_date.'
        ),
    },
    {
        'key': 'customer_of',
        'label': 'Customer Of',
        'description': (
            'Subject purchases launch services, satellite capacity, data products, or other space services from Object. '
            'Qualifiers: service_type, contract_value_usd, date.'
        ),
    },
    # ── Operations ────────────────────────────────────────────────────────
    {
        'key': 'launches_for',
        'label': 'Launches For',
        'description': (
            'Subject (launch provider) launches payloads for Object (satellite operator or customer). '
            'Qualifiers: vehicle, date, payload_kg, orbit.'
        ),
    },
    {
        'key': 'launched_payload',
        'label': 'Launched Payload',
        'description': (
            'Subject (launch vehicle or mission) placed Object (satellite, spacecraft, or payload) in orbit or on trajectory. '
            'Qualifiers: orbit, date, mission_name.'
        ),
    },
    {
        'key': 'operates',
        'label': 'Operates',
        'description': (
            'Subject (organisation) operates Object (satellite constellation, space station, ground station, or asset). '
            'Qualifiers: since, orbit, count.'
        ),
    },
    {
        'key': 'operates_ground_station_for',
        'label': 'Operates Ground Station For',
        'description': (
            'Subject operates ground infrastructure (ground stations, TT&C, data downlink) for Object. '
            'Qualifiers: location, bands.'
        ),
    },
    # ── Collaboration ─────────────────────────────────────────────────────
    {
        'key': 'partnered_with',
        'label': 'Partnered With',
        'description': (
            'Subject and Object have a formal commercial or technical partnership. '
            'Symmetric. Qualifiers: type, date, description.'
        ),
    },
    {
        'key': 'co_develops',
        'label': 'Co-Develops',
        'description': (
            'Subject and Object jointly develop a technology, product, or mission. '
            'Symmetric. Qualifiers: program, since.'
        ),
    },
    {
        'key': 'uses_technology_from',
        'label': 'Uses Technology From',
        'description': (
            'Subject licences, integrates, or is built on technology from Object. '
            'Qualifiers: technology, licence_date.'
        ),
    },
    # ── People ────────────────────────────────────────────────────────────
    {
        'key': 'founded_by',
        'label': 'Founded By',
        'description': 'Subject (organisation) was co-founded by Object (person).',
    },
    {
        'key': 'employs',
        'label': 'Employs',
        'description': 'Subject (organisation) employs Object (person) in a named role. Qualifiers: role, since.',
    },
    # ── Competitive / regulatory ──────────────────────────────────────────
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
        'key': 'regulated_by',
        'label': 'Regulated By',
        'description': 'Subject (company or mission) is regulated or overseen by Object (government body or agency).',
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
