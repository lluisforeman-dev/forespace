"""Refresh map-critical attribute descriptions so the LLM prompt gets updated text."""
from django.db import migrations


def update_descriptions(apps, schema_editor):
    AttributeDef = apps.get_model('core', 'AttributeDef')
    AttributeDef.objects.filter(key='headquarters_city').update(
        description='City where the organization has its primary headquarters. Extract for companies, investors, and entities alike.',
    )
    AttributeDef.objects.filter(key='headquarters_country').update(
        description=(
            'Country of headquarters. Use ISO 3166-1 alpha-2 codes where possible (e.g. "US", "GB", "FR"). '
            'Extract for companies, investors, and entities alike.'
        ),
    )
    AttributeDef.objects.filter(key='employee_count').update(
        description=(
            'Total number of full-time employees. Extract the most recent figure mentioned. '
            'If a range is given, extract the midpoint and note it as approximate. '
            'Extract for companies, investors, and entities alike.'
        ),
    )
    AttributeDef.objects.filter(key='total_funding_usd').update(
        description=(
            'Cumulative total funding raised by the organization across all rounds, in USD. '
            'Only extract when the document explicitly states a cumulative total. '
            'Extract for companies, investors, and entities alike.'
        ),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0013_rename_organization_type'),
    ]

    operations = [
        migrations.RunPython(update_descriptions, migrations.RunPython.noop),
    ]
