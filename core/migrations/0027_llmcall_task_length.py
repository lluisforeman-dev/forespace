from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0026_entity_has_street_address'),
    ]

    operations = [
        migrations.AlterField(
            model_name='llmcall',
            name='task',
            # 20 chars silently dropped rows for longer task names via log_call's
            # blanket except — 60 covers every caller ('synthesise_entity_summary_wk')
            field=models.CharField(choices=[
                ('triage', 'Triage'),
                ('extract', 'Extract'),
                ('resolve', 'Resolve'),
                ('analysis', 'Analysis'),
            ], max_length=60),
        ),
    ]
