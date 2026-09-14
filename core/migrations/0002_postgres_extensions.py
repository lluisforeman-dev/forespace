from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
    ]

    operations = [
        # Extensions
        migrations.RunSQL(
            "CREATE EXTENSION IF NOT EXISTS btree_gist;",
            reverse_sql="DROP EXTENSION IF EXISTS btree_gist;",
        ),
        migrations.RunSQL(
            "CREATE EXTENSION IF NOT EXISTS pg_trgm;",
            reverse_sql="DROP EXTENSION IF EXISTS pg_trgm;",
        ),
        migrations.RunSQL(
            "CREATE EXTENSION IF NOT EXISTS ltree;",
            reverse_sql="DROP EXTENSION IF EXISTS ltree;",
        ),
        migrations.RunSQL(
            "CREATE EXTENSION IF NOT EXISTS vector;",
            reverse_sql="DROP EXTENSION IF EXISTS vector;",
        ),
        # Trigram index for fast fuzzy alias lookup
        migrations.RunSQL(
            "CREATE INDEX entity_alias_trgm ON entity_alias USING gin (alias_norm gin_trgm_ops);",
            reverse_sql="DROP INDEX IF EXISTS entity_alias_trgm;",
        ),
        # ltree path type on taxonomy_node
        migrations.RunSQL(
            "ALTER TABLE taxonomy_node ALTER COLUMN path TYPE ltree USING path::ltree;",
            reverse_sql="ALTER TABLE taxonomy_node ALTER COLUMN path TYPE varchar(500) USING path::text;",
        ),
        migrations.RunSQL(
            "CREATE INDEX taxonomy_node_path_gist ON taxonomy_node USING gist(path);",
            reverse_sql="DROP INDEX IF EXISTS taxonomy_node_path_gist;",
        ),
        # GiST exclusion: no two accepted, non-superseded assertions for the same
        # entity+attribute can have overlapping validity windows.
        migrations.RunSQL(
            """
            ALTER TABLE assertion ADD CONSTRAINT no_overlapping_validity
            EXCLUDE USING gist (
                entity_id WITH =,
                attribute_key WITH =,
                valid_range WITH &&
            )
            WHERE (status = 'accepted' AND superseded_at IS NULL);
            """,
            reverse_sql="ALTER TABLE assertion DROP CONSTRAINT IF EXISTS no_overlapping_validity;",
        ),
    ]
