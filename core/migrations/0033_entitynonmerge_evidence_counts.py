from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0032_corroboration_and_verification'),
    ]

    operations = [
        migrations.AddField(
            model_name='entitynonmerge',
            name='assertions_a',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='entitynonmerge',
            name='assertions_b',
            field=models.IntegerField(default=0),
        ),
    ]
