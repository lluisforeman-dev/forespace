from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0021_prompttemplate'),
    ]

    operations = [
        migrations.AddField(
            model_name='entity',
            name='space_relevance',
            field=models.SmallIntegerField(
                blank=True, null=True,
                help_text='0 = confirmed non-space, 100 = confirmed space-relevant, null = unknown',
            ),
        ),
    ]
