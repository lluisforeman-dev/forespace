from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0025_entity_geocoords'),
    ]

    operations = [
        migrations.AddField(
            model_name='entity',
            name='has_street_address',
            field=models.BooleanField(default=False),
        ),
    ]
