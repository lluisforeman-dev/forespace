"""
entity_current v2 — staleness-aware projection (truth decay, §8d).

Changes vs 0004:
- Ranks competing values per (entity, attribute) by EFFECTIVE confidence:
  raw confidence minus a recency penalty anchored to the claim's world date
  (valid_range.lower, i.e. as_of). For volatile attributes the freshest value
  now wins even at slightly lower raw confidence — "100 employees as of 2016"
  no longer outranks "250 employees as of 2026". Immutable attributes
  (volatility_days NULL) keep pure confidence ranking.
- Exposes valid_from, volatility_days and effective_confidence so consumers
  (UI, API, future products) can compute display trust without re-joining
  attribute_def.

Refresh with:  REFRESH MATERIALIZED VIEW CONCURRENTLY entity_current;
(runs in project queue via ingest.tasks.project.refresh_entity_current)
"""
from django.db import migrations


_PENALTY_SQL = """GREATEST(a.confidence - CASE
        WHEN ad.volatility_days IS NULL THEN 0
        ELSE LEAST(30, FLOOR(
            GREATEST(0,
                EXTRACT(epoch FROM (now() - COALESCE(lower(a.valid_range), a.observed_at))) / 86400.0
                - ad.volatility_days
            ) / GREATEST(ad.volatility_days, 1) * 10
        ))::int
    END, 0)"""


_CREATE = f"""
DROP MATERIALIZED VIEW IF EXISTS entity_current;

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
    {_PENALTY_SQL}       AS effective_confidence,
    COALESCE(lower(a.valid_range), a.observed_at) AS valid_from,
    ad.volatility_days  AS volatility_days,
    a.observed_at,
    a.valid_range,
    a.document_id,
    a.method
FROM assertion a
JOIN entity e ON e.id = a.entity_id
LEFT JOIN attribute_def ad ON ad.key = a.attribute_key
WHERE a.status = 'accepted'
  AND a.superseded_at IS NULL
ORDER BY a.entity_id, a.attribute_key,
    {_PENALTY_SQL} DESC,
    a.confidence DESC,
    COALESCE(lower(a.valid_range), a.observed_at) DESC,
    a.observed_at DESC
WITH DATA;

CREATE UNIQUE INDEX entity_current_pk
    ON entity_current (entity_id, attribute_key);
"""

_DROP_V1 = """
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


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0027_llmcall_task_length'),
    ]

    operations = [
        migrations.RunSQL(_CREATE, reverse_sql=_DROP_V1),
    ]
