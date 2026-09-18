from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0009_taxonomynode_status'),
    ]

    operations = [
        migrations.AlterField(
            model_name='assertion',
            name='method',
            field=models.CharField(
                choices=[
                    ('extracted', 'Extracted'),
                    ('structured_api', 'Structured API'),
                    ('manual', 'Manual'),
                    ('derived', 'Derived'),
                    ('imputed', 'Imputed'),
                    ('synthesis', 'Synthesis'),
                ],
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name='assertion',
            name='status',
            field=models.CharField(
                choices=[
                    ('accepted', 'Accepted'),
                    ('candidate', 'Candidate'),
                    ('rejected', 'Rejected'),
                    ('superseded', 'Superseded'),
                ],
                default='accepted',
                max_length=20,
            ),
        ),
    ]
