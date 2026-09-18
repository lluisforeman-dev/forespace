from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0014_update_location_attribute_descriptions'),
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
                    ('university', 'University'),
                    ('facility', 'Facility'),
                    ('asset', 'Asset'),
                    ('person', 'Person'),
                    ('document_node', 'Document'),
                    ('event', 'Event'),
                    ('program', 'Program'),
                ],
            ),
        ),
    ]
