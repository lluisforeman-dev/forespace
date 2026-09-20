from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0020_event_call_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='PromptTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(db_index=True, max_length=100)),
                ('label', models.CharField(max_length=200)),
                ('description', models.TextField(blank=True)),
                ('system_prompt', models.TextField()),
                ('version', models.PositiveIntegerField(default=1)),
                ('is_active', models.BooleanField(db_index=True, default=False)),
                ('notes', models.TextField(blank=True, help_text='Reason for this version / what changed')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['key', '-version'],
            },
        ),
    ]
