from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0022_entity_space_relevance'),
    ]

    operations = [
        migrations.CreateModel(
            name='EntityNonMerge',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('decided_at', models.DateTimeField(auto_now_add=True)),
                ('method', models.CharField(default='auto:dedup_sweep', max_length=50)),
                ('entity_a', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='+',
                    to='core.entity',
                )),
                ('entity_b', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='+',
                    to='core.entity',
                )),
            ],
            options={
                'db_table': 'entity_non_merge',
                'unique_together': {('entity_a', 'entity_b')},
            },
        ),
    ]
