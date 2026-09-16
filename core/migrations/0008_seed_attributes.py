"""Data migration: seed AttributeDef rows.

Keeps the attribute vocabulary in sync with seed_attributes.py without requiring
a manual management-command run after deploy.
"""
from django.db import migrations


def seed(apps, schema_editor):
    from core.management.commands.seed_attributes import ATTRIBUTES
    AttributeDef = apps.get_model('core', 'AttributeDef')
    for spec in ATTRIBUTES:
        spec = dict(spec)
        unit = spec.pop('unit', None)
        AttributeDef.objects.get_or_create(
            key=spec['key'],
            defaults={**spec, 'unit': unit},
        )


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0007_create_superuser'),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
