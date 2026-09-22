from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0024_event_dedup_amount_constraint'),
    ]

    operations = [
        migrations.AddField(
            model_name='entity',
            name='latitude',
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name='entity',
            name='longitude',
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AlterField(
            model_name='entity',
            name='entity_type',
            field=models.CharField(
                choices=[
                    ('company', 'Company'), ('investor', 'Investor'), ('entity', 'Entity'),
                    ('university', 'University'), ('facility', 'Facility'), ('asset', 'Asset'),
                    ('person', 'Person'), ('document_node', 'Document'), ('event', 'Event'),
                    ('program', 'Program'), ('funding_program', 'Funding Program'),
                    ('end_user', 'End User'), ('geography', 'Geography'),
                ],
                max_length=30,
            ),
        ),
    ]
