"""relation_current — active accepted relations, best confidence per (subject, predicate, object).

Analogous to entity_current. Refreshed by ingest.tasks.project after pipeline runs.
"""
from django.db import migrations

_CREATE = """
CREATE MATERIALIZED VIEW relation_current AS
SELECT DISTINCT ON (r.subject_id, r.predicate, r.object_id)
    r.id            AS relation_id,
    r.subject_id,
    s.canonical_name AS subject_name,
    s.entity_type   AS subject_type,
    r.predicate     AS predicate_key,
    r.object_id,
    o.canonical_name AS object_name,
    o.entity_type   AS object_type,
    r.qualifiers,
    r.confidence,
    r.valid_range,
    r.observed_at,
    r.document_id,
    r.method
FROM relation r
JOIN entity s ON s.id = r.subject_id
JOIN entity o ON o.id = r.object_id
WHERE r.status = 'accepted'
  AND r.superseded_at IS NULL
ORDER BY r.subject_id, r.predicate, r.object_id, r.confidence DESC
WITH DATA;

CREATE UNIQUE INDEX relation_current_pk
    ON relation_current (subject_id, predicate_key, object_id);
CREATE INDEX relation_current_subject ON relation_current (subject_id);
CREATE INDEX relation_current_object  ON relation_current (object_id);
"""

_DROP = "DROP MATERIALIZED VIEW IF EXISTS relation_current;"


class Migration(migrations.Migration):
    dependencies = [('core', '0005_phase4_cost_watchlist')]
    operations = [migrations.RunSQL(_CREATE, reverse_sql=_DROP)]
