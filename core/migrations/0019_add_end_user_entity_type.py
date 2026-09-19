"""Add end_user to Entity.entity_type choices.

end_user: a company or organisation that consumes space services/data as a
downstream customer, but whose primary business is NOT in the space industry.
Examples: a shipping company using AIS tracking, a bank using satellite imagery
for commodity monitoring, a farmer using GNSS precision agriculture services.

Distinct from company (active space supply-chain participant) in that:
  - research pipeline uses space_angle topic (only extracts space touchpoints)
  - classification covers adjacent_sector + customer_type + downstream value_chain
  - excluded from space company views; queryable as demand-side market signal
"""
from django.db import migrations
import django.db.models.fields


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0018_add_funding_program_entity_type'),
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
                    ('end_user', 'End User'),
                ],
                max_length=30,
            ),
        ),
    ]
