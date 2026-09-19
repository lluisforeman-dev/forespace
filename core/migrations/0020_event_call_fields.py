"""
Add call_url and call_status to Event, and register the previously-unregistered
event types (grant_call, grant_award, ipo, spac, debt_financing, convertible,
crowdfunding) in the choices list.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0019_add_end_user_entity_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='event',
            name='call_url',
            field=models.URLField(blank=True, max_length=1000, null=True),
        ),
        migrations.AddField(
            model_name='event',
            name='call_status',
            field=models.CharField(
                blank=True,
                choices=[('open', 'Open'), ('upcoming', 'Upcoming'), ('closed', 'Closed')],
                max_length=20,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name='event',
            name='event_type',
            field=models.CharField(
                choices=[
                    ('funding_round',  'Funding Round'),
                    ('grant_award',    'Grant Award'),
                    ('grant_call',     'Grant Call (Open)'),
                    ('ipo',            'IPO'),
                    ('spac',           'SPAC'),
                    ('debt_financing', 'Debt Financing'),
                    ('convertible',    'Convertible Note'),
                    ('crowdfunding',   'Crowdfunding'),
                    ('launch',         'Launch'),
                    ('contract_award', 'Contract Award'),
                    ('partnership',    'Partnership'),
                    ('acquisition',    'Acquisition'),
                    ('failure',        'Failure / Anomaly'),
                    ('pivot',          'Strategic Pivot'),
                    ('regulatory',     'Regulatory'),
                    ('milestone',      'Milestone'),
                    ('leadership',     'Leadership Change'),
                    ('publication',    'Publication / Paper'),
                    ('research_grant', 'Research Grant'),
                ],
                max_length=30,
            ),
        ),
    ]
