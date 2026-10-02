from django.db import migrations, models
from django.contrib.postgres.fields import ArrayField


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0031_document_raw_content_and_name_trgm'),
    ]

    operations = [
        # ── Corroboration as data (§8a/§8d) ──────────────────────────────
        # How many INDEPENDENT documents assert the same value. Without this
        # counter, "trust" is an opaque number that cannot be explained.
        migrations.AddField(
            model_name='assertion',
            name='corroboration_count',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='assertion',
            name='corroborated_by',
            field=ArrayField(base_field=models.BigIntegerField(), blank=True, null=True),
        ),
        # ── Mechanical quote verification (§7c on the web-research path) ─
        # null = not checked · true = verified verbatim in the fetched source ·
        # false = failed verification or quote too weak to check.
        migrations.AddField(
            model_name='assertion',
            name='quote_verified',
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='assertion',
            name='verified_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
