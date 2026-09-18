"""Rename entity_type 'organization' → 'company' in entity and attribute_def tables."""
from django.db import migrations, models


def rename_organization_to_company(apps, schema_editor):
    schema_editor.execute(
        "UPDATE entity SET entity_type = 'company' WHERE entity_type = 'organization'"
    )
    schema_editor.execute(
        "UPDATE attribute_def SET entity_type = 'company' WHERE entity_type = 'organization'"
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0012_seed_predicates'),
    ]

    operations = [
        migrations.AlterField(
            model_name='entity',
            name='entity_type',
            field=models.CharField(
                max_length=30,
                choices=[
                    ('company', 'Company'),
                    ('investor', 'Investor'),
                    ('entity', 'Entity'),
                    ('facility', 'Facility'),
                    ('asset', 'Asset'),
                    ('person', 'Person'),
                    ('document_node', 'Document'),
                    ('event', 'Event'),
                    ('program', 'Program'),
                ],
            ),
        ),
        migrations.RunPython(rename_organization_to_company, migrations.RunPython.noop),
    ]
