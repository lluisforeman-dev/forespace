"""Add funding_program to Entity.entity_type choices.

Splits the generic 'program' type into:
  program         — space/commercial programmes (Artemis, Rideshare, ISS)
  funding_program — deployable funding instruments (Horizon Europe, ESA ARTES,
                    EIC Accelerator, BlackRock Space Fund)

CharField choices are not enforced at the DB level, so no schema change is needed.
"""
from django.db import migrations
import django.db.models.fields


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0017_research_area_taxonomy_and_attributes'),
    ]

    operations = [
        migrations.AlterField(
            model_name='entity',
            name='entity_type',
            field=django.db.models.fields.CharField(
                choices=[
                    ('company', 'Company'),
                    ('investor', 'Investor'),
                    ('entity', 'Entity'),
                    ('university', 'University'),
                    ('facility', 'Facility'),
                    ('asset', 'Asset'),
                    ('person', 'Person'),
                    ('document_node', 'Document'),
                    ('event', 'Event'),
                    ('program', 'Program'),
                    ('funding_program', 'Funding Program'),
                ],
                max_length=30,
            ),
        ),
    ]
