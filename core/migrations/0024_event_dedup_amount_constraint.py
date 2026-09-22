"""Add partial unique constraint on (entity, event_type, date, amount_usd)
for non-null, non-zero amounts to prevent duplicate event rows at the DB level.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0023_entity_non_merge'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='event',
            constraint=models.UniqueConstraint(
                fields=['entity', 'event_type', 'date', 'amount_usd'],
                condition=models.Q(amount_usd__gt=0),
                name='entity_event_dedup_amount',
            ),
        ),
    ]
