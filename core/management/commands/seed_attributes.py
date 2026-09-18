from django.core.management.base import BaseCommand
from core.models import AttributeDef

# Space-industry attribute vocabulary.
# The LLM extraction prompt reads `description` directly — write it carefully.
ATTRIBUTES = [
    # ── Organisation facts ──────────────────────────────────────────────────
    {
        'key': 'founding_year',
        'entity_type': 'company',
        'label': 'Founding Year',
        'datatype': 'int',
        'cardinality': 'single',
        'is_projected': True,
        'is_promoted': True,
        'volatility_days': None,
        'description': (
            'The calendar year in which the organization was formally founded or incorporated. '
            'Treat as immutable — changing this always indicates a data error.'
        ),
    },
    {
        'key': 'headquarters_city',
        'entity_type': 'company',
        'label': 'Headquarters City',
        'datatype': 'text',
        'cardinality': 'single',
        'is_projected': True,
        'is_promoted': True,
        'volatility_days': 730,
        'description': 'City where the organization has its primary headquarters.',
    },
    {
        'key': 'headquarters_country',
        'entity_type': 'company',
        'label': 'Headquarters Country',
        'datatype': 'text',
        'cardinality': 'single',
        'is_projected': True,
        'is_promoted': True,
        'volatility_days': 1825,
        'description': (
            'Country of headquarters. Use ISO 3166-1 alpha-2 codes where possible (e.g. "US", "GB", "FR").'
        ),
    },
    {
        'key': 'employee_count',
        'entity_type': 'company',
        'label': 'Employee Count',
        'datatype': 'int',
        'cardinality': 'single',
        'is_projected': True,
        'is_promoted': False,
        'volatility_days': 180,
        'description': (
            'Total number of full-time employees. Extract the most recent figure mentioned. '
            'If a range is given, extract the midpoint and note it as approximate.'
        ),
    },
    {
        'key': 'business_description',
        'entity_type': 'company',
        'label': 'Business Description',
        'datatype': 'text',
        'cardinality': 'single',
        'is_projected': True,
        'is_promoted': False,
        'volatility_days': 365,
        'description': (
            'One to three sentences describing what the organization does in the space industry. '
            'Prefer the company\'s own wording from press releases or official descriptions.'
        ),
    },
    {
        'key': 'website',
        'entity_type': 'company',
        'label': 'Website',
        'datatype': 'text',
        'cardinality': 'single',
        'is_projected': True,
        'is_promoted': False,
        'volatility_days': None,
        'description': 'Primary website URL of the organization (e.g. "https://example.com").',
    },
    {
        'key': 'ceo_name',
        'entity_type': 'company',
        'label': 'CEO / Managing Director',
        'datatype': 'text',
        'cardinality': 'single',
        'is_projected': True,
        'is_promoted': False,
        'volatility_days': 365,
        'description': 'Full name of the current Chief Executive Officer or equivalent top executive.',
    },
    # ── Funding ─────────────────────────────────────────────────────────────
    {
        'key': 'funding_round_amount_usd',
        'entity_type': 'company',
        'label': 'Funding Round Amount (USD)',
        'datatype': 'money',
        'unit': 'USD',
        'cardinality': 'multi',
        'is_projected': False,
        'is_promoted': False,
        'volatility_days': 30,
        'description': (
            'Amount raised in a single funding round, in USD. '
            'Always extract the round type (seed/Series A/B/C etc.) and date alongside this. '
            'Do not confuse cumulative total raised with a single round amount.'
        ),
    },
    {
        'key': 'funding_round_series',
        'entity_type': 'company',
        'label': 'Funding Round Series',
        'datatype': 'text',
        'cardinality': 'multi',
        'is_projected': False,
        'is_promoted': False,
        'volatility_days': 30,
        'description': (
            'Type of funding round: seed, pre-seed, series_a, series_b, series_c, series_d, '
            'convertible_note, grant, ipo, spac, debt, or other.'
        ),
    },
    {
        'key': 'total_funding_usd',
        'entity_type': 'company',
        'label': 'Total Funding Raised (USD)',
        'datatype': 'money',
        'unit': 'USD',
        'cardinality': 'single',
        'is_projected': True,
        'is_promoted': False,
        'volatility_days': 30,
        'description': (
            'Cumulative total funding raised by the organization across all rounds, in USD. '
            'Only extract when the document explicitly states a cumulative total.'
        ),
    },
    # ── Launch vehicles ──────────────────────────────────────────────────────
    {
        'key': 'launch_vehicle_name',
        'entity_type': 'asset',
        'label': 'Launch Vehicle Name',
        'datatype': 'text',
        'cardinality': 'single',
        'is_projected': True,
        'is_promoted': True,
        'volatility_days': None,
        'description': 'Official name of the launch vehicle (e.g. "Falcon 9", "Ariane 6", "New Glenn").',
    },
    {
        'key': 'payload_leo_kg',
        'entity_type': 'asset',
        'label': 'Payload to LEO (kg)',
        'datatype': 'decimal',
        'unit': 'kg',
        'cardinality': 'single',
        'is_projected': True,
        'is_promoted': False,
        'volatility_days': 730,
        'description': (
            'Maximum payload mass deliverable to low Earth orbit (LEO) in kilograms. '
            'If the document gives a range, use the upper bound.'
        ),
    },
    {
        'key': 'payload_gto_kg',
        'entity_type': 'asset',
        'label': 'Payload to GTO (kg)',
        'datatype': 'decimal',
        'unit': 'kg',
        'cardinality': 'single',
        'is_projected': True,
        'is_promoted': False,
        'volatility_days': 730,
        'description': 'Maximum payload mass deliverable to geostationary transfer orbit (GTO) in kilograms.',
    },
    {
        'key': 'launch_cost_usd',
        'entity_type': 'asset',
        'label': 'Launch Cost (USD)',
        'datatype': 'money',
        'unit': 'USD',
        'cardinality': 'single',
        'is_projected': True,
        'is_promoted': False,
        'volatility_days': 365,
        'description': 'Advertised or reported cost per launch in USD.',
    },
    {
        'key': 'reusability',
        'entity_type': 'asset',
        'label': 'Reusability',
        'datatype': 'bool',
        'cardinality': 'single',
        'is_projected': True,
        'is_promoted': False,
        'volatility_days': None,
        'description': 'True if the launch vehicle is designed for reuse (any stage), false otherwise.',
    },
    # ── Satellites / spacecraft ───────────────────────────────────────────
    {
        'key': 'satellite_count',
        'entity_type': 'company',
        'label': 'Satellites in Orbit',
        'datatype': 'int',
        'cardinality': 'single',
        'is_projected': True,
        'is_promoted': False,
        'volatility_days': 30,
        'description': 'Number of operational satellites currently in orbit owned or operated by the organization.',
    },
    {
        'key': 'constellation_target_count',
        'entity_type': 'company',
        'label': 'Constellation Target Size',
        'datatype': 'int',
        'cardinality': 'single',
        'is_projected': True,
        'is_promoted': False,
        'volatility_days': 365,
        'description': 'Planned or licensed total number of satellites in the company\'s constellation.',
    },
    {
        'key': 'orbit_altitude_km',
        'entity_type': 'asset',
        'label': 'Orbit Altitude (km)',
        'datatype': 'decimal',
        'unit': 'km',
        'cardinality': 'single',
        'is_projected': True,
        'is_promoted': False,
        'volatility_days': None,
        'description': 'Nominal orbital altitude in kilometres above Earth\'s surface.',
    },
]


class Command(BaseCommand):
    help = 'Seed AttributeDef rows for space-industry extraction.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--overwrite',
            action='store_true',
            help='Update description/label/volatility on existing rows.',
        )

    def handle(self, *args, **options):
        created = updated = skipped = 0
        for spec in ATTRIBUTES:
            unit = spec.pop('unit', None)
            obj, is_new = AttributeDef.objects.get_or_create(
                key=spec['key'],
                defaults={**spec, 'unit': unit},
            )
            if is_new:
                created += 1
            elif options['overwrite']:
                for field, val in {**spec, 'unit': unit}.items():
                    if field != 'key':
                        setattr(obj, field, val)
                obj.save()
                updated += 1
            else:
                skipped += 1
            # restore for idempotency across re-runs in the same process
            if unit:
                spec['unit'] = unit

        self.stdout.write(
            self.style.SUCCESS(
                f'seed_attributes: {created} created, {updated} updated, {skipped} skipped.'
            )
        )
