from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0030_rename_sonar_to_eigensearch'),
    ]

    operations = [
        migrations.AddField(
            model_name='document',
            name='raw_content',
            field=models.TextField(blank=True, null=True),
        ),
        # Trigram index for icontains entity search (entity_search, dashboard
        # autocomplete, curation filters all do canonical_name lookups).
        migrations.RunSQL(
            "CREATE INDEX entity_name_trgm ON entity USING gin (canonical_name gin_trgm_ops);",
            reverse_sql="DROP INDEX IF EXISTS entity_name_trgm;",
        ),
    ]
