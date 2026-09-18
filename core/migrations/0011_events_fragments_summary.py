from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0010_assertion_synthesis'),
    ]

    operations = [
        migrations.CreateModel(
            name='Event',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False)),
                ('event_type', models.CharField(choices=[
                    ('funding_round', 'Funding Round'), ('launch', 'Launch'),
                    ('contract_award', 'Contract Award'), ('partnership', 'Partnership'),
                    ('acquisition', 'Acquisition'), ('failure', 'Failure / Anomaly'),
                    ('pivot', 'Strategic Pivot'), ('regulatory', 'Regulatory'),
                    ('milestone', 'Milestone'), ('leadership', 'Leadership Change'),
                ], max_length=30)),
                ('title', models.CharField(max_length=500)),
                ('date', models.DateField(blank=True, null=True)),
                ('date_precision', models.CharField(
                    choices=[('day', 'Day'), ('month', 'Month'), ('year', 'Year')],
                    default='year', max_length=10,
                )),
                ('description', models.TextField()),
                ('amount_usd', models.DecimalField(blank=True, decimal_places=2, max_digits=20, null=True)),
                ('significance', models.CharField(
                    choices=[('high', 'High'), ('medium', 'Medium'), ('low', 'Low')],
                    default='medium', max_length=10,
                )),
                ('confidence', models.SmallIntegerField(default=60)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('entity', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='events', to='core.entity',
                )),
                ('participants', models.ManyToManyField(
                    blank=True, related_name='participated_events', to='core.entity',
                )),
                ('source', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    to='core.document',
                )),
            ],
            options={'db_table': 'entity_event', 'ordering': ['-date', '-created_at']},
        ),
        migrations.CreateModel(
            name='KnowledgeFragment',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False)),
                ('category', models.CharField(choices=[
                    ('technical', 'Technical'), ('financial', 'Financial'),
                    ('competitive', 'Competitive'), ('regulatory', 'Regulatory'),
                    ('strategic', 'Strategic'), ('operational', 'Operational'),
                    ('people', 'People'), ('challenge', 'Challenge'),
                ], max_length=20)),
                ('text', models.TextField()),
                ('date_of_information', models.DateField(blank=True, null=True)),
                ('confidence', models.SmallIntegerField(default=60)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('entity', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='fragments', to='core.entity',
                )),
                ('source', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    to='core.document',
                )),
            ],
            options={'db_table': 'knowledge_fragment', 'ordering': ['-date_of_information', '-created_at']},
        ),
        migrations.CreateModel(
            name='EntitySummary',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False)),
                ('overview', models.TextField()),
                ('challenges', models.JSONField(default=list)),
                ('strategic_bets', models.JSONField(default=list)),
                ('competitive_position', models.TextField(blank=True)),
                ('last_synthesised', models.DateTimeField(auto_now=True)),
                ('source_count', models.IntegerField(default=0)),
                ('entity', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='summary', to='core.entity',
                )),
            ],
            options={'db_table': 'entity_summary'},
        ),
    ]
