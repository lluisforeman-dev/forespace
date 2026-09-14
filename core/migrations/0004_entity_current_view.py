"""
entity_current — the primary read surface of the knowledge graph.

DISTINCT ON (entity_id, attribute_key) ordered by confidence DESC, observed_at DESC
gives the single best accepted assertion per attribute per entity.

Refresh with:  REFRESH MATERIALIZED VIEW CONCURRENTLY entity_current;
(runs in project queue via ingest.tasks.project.refresh_entity_current)
"""
from django.db import migrations


_CREATE = """
CREATE MATERIALIZED VIEW entity_current AS
SELECT DISTINCT ON (a.entity_id, a.attribute_key)
    e.id                AS entity_id,
    e.canonical_name,
    e.entity_type,
    e.slug,
    e.status            AS entity_status,
    a.id                AS assertion_id,
    a.attribute_key,
    a.value_text,
    a.value_num,
    a.value_bool,
    a.value_date,
    a.value_json,
    a.unit,
    a.confidence,
    a.observed_at,
    a.valid_range,
    a.document_id,
    a.method
FROM assertion a
JOIN entity e ON e.id = a.entity_id
WHERE a.status = 'accepted'
  AND a.superseded_at IS NULL
ORDER BY a.entity_id, a.attribute_key, a.confidence DESC, a.observed_at DESC
WITH DATA;

CREATE UNIQUE INDEX entity_current_pk
    ON entity_current (entity_id, attribute_key);
"""

_DROP = """
DROP MATERIALIZED VIEW IF EXISTS entity_current;
"""


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_document_pipeline_status'),
    ]

    operations = [
        migrations.RunSQL(_CREATE, reverse_sql=_DROP),
    ]
