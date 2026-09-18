"""Data migration: seed PredicateDef rows.

Keeps the predicate vocabulary in sync with seed_predicates.py without
requiring a manual management-command run after deploy.
Uses update_or_create so re-running is safe and existing rows get updated.
"""
from django.db import migrations


def seed(apps, schema_editor):
    from core.management.commands.seed_predicates import PREDICATES
    PredicateDef = apps.get_model('core', 'PredicateDef')
    for spec in PREDICATES:
        PredicateDef.objects.update_or_create(
            key=spec['key'],
            defaults={'label': spec['label'], 'description': spec['description']},
        )


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0011_events_fragments_summary'),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
