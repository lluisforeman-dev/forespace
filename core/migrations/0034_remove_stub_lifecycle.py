from django.db import migrations, models


def promote_stubs_to_active(apps, schema_editor):
    """The stub lifecycle is removed: an entity created by resolution is a real
    entity. Convert every historical stub to active."""
    Entity = apps.get_model('core', 'Entity')
    Entity.objects.filter(status='stub').update(status='active')


def un_promote(apps, schema_editor):
    # Irreversible by design — there is no stub concept to return to.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0033_entitynonmerge_evidence_counts'),
    ]

    operations = [
        migrations.RunPython(promote_stubs_to_active, un_promote),
        migrations.AlterField(
            model_name='entity',
            name='status',
            field=models.CharField(
                choices=[
                    ('active', 'Active'),
                    ('merged', 'Merged'),
                    ('disputed', 'Disputed'),
                    ('dormant', 'Dormant'),
                ],
                default='active',
                max_length=20,
            ),
        ),
    ]
