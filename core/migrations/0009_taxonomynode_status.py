from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_seed_attributes'),
    ]

    operations = [
        migrations.AddField(
            model_name='taxonomynode',
            name='status',
            field=models.CharField(
                max_length=20,
                choices=[
                    ('proposed', 'Proposed'),
                    ('active', 'Active'),
                    ('deprecated', 'Deprecated'),
                ],
                default='active',
            ),
        ),
    ]
