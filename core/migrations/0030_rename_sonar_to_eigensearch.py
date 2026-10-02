"""
Rename 'Perplexity Sonar' provenance to 'EigenSearch'.

The research pipeline no longer uses Perplexity Sonar — the web-research calls
run through OpenRouter's :online variant, now branded EigenSearch. This
migrates the existing Source row (so all historical documents consolidate
under the new name instead of leaving a stale duplicate source) and the
relate_sonar prompt template key.
"""
from django.db import migrations


_CREATE = """
UPDATE source
SET name = 'EigenSearch', domain = 'openrouter.ai'
WHERE name = 'Perplexity Sonar';

UPDATE core_prompttemplate
SET key = 'relate_eigensearch'
WHERE key = 'relate_sonar';

UPDATE core_prompttemplate
SET label = REPLACE(label, 'Sonar', 'EigenSearch'),
    description = REPLACE(description, 'Sonar', 'EigenSearch')
WHERE label ILIKE '%sonar%' OR description ILIKE '%sonar%';
"""

_REVERSE = """
UPDATE source
SET name = 'Perplexity Sonar', domain = 'perplexity.ai'
WHERE name = 'EigenSearch';

UPDATE core_prompttemplate
SET key = 'relate_sonar'
WHERE key = 'relate_eigensearch';

UPDATE core_prompttemplate
SET label = REPLACE(label, 'EigenSearch', 'Sonar'),
    description = REPLACE(description, 'EigenSearch', 'Sonar')
WHERE label ILIKE '%eigensearch%' OR description ILIKE '%eigensearch%';
"""


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0029_alter_entitysummary_id_alter_event_id_and_more'),
    ]

    operations = [
        migrations.RunSQL(_CREATE, reverse_sql=_REVERSE),
    ]
