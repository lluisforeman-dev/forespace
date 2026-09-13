import uuid
from django.db import models
from django.contrib.postgres.fields import DateTimeTZRangeField
from django.utils import timezone
from psycopg2.extras import DateTimeTZRange


def _open_range():
    return DateTimeTZRange(timezone.now(), None)


class Taxonomy(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('deprecated', 'Deprecated'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=100)    # 'value_chain', 'orbit_regime', 'trl', ...
    version = models.IntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')

    class Meta:
        db_table = 'taxonomy'
        unique_together = [('key', 'version')]

    def __str__(self):
        return f'{self.key} v{self.version}'


class TaxonomyNode(models.Model):
    """
    path stores ltree-style dotted notation: 'upstream.launch.small_lift'
    Full ltree indexing is applied via migration:
        CREATE EXTENSION IF NOT EXISTS ltree;
        ALTER TABLE taxonomy_node ALTER COLUMN path TYPE ltree USING path::ltree;
        CREATE INDEX taxonomy_node_path_gist ON taxonomy_node USING gist(path);
    Until then, path works as a plain text field with LIKE prefix queries.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    taxonomy = models.ForeignKey(Taxonomy, on_delete=models.CASCADE, related_name='nodes')
    path = models.CharField(max_length=500)  # e.g. 'upstream.launch.small_lift.reusable'
    label = models.CharField(max_length=255)
    definition = models.TextField()   # the classifier prompt reads this directly
    examples = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = 'taxonomy_node'
        unique_together = [('taxonomy', 'path')]

    def __str__(self):
        return f'{self.taxonomy.key}: {self.path}'


class Classification(models.Model):
    """
    Multi-dimensional: a company can be classified along independent facets
    (value_chain, orbit_regime, customer_type, maturity, technology, adjacent_sector)
    with weighted membership (e.g. 70% upstream, 30% downstream).
    """
    METHODS = [
        ('extracted', 'Extracted'),
        ('manual', 'Manual'),
        ('derived', 'Derived'),
    ]

    entity = models.ForeignKey(
        'core.Entity', on_delete=models.CASCADE, related_name='classifications',
    )
    node = models.ForeignKey(TaxonomyNode, on_delete=models.PROTECT, related_name='classifications')
    weight = models.DecimalField(max_digits=4, decimal_places=3, default=1.0)
    is_primary = models.BooleanField(default=False)
    valid_range = DateTimeTZRangeField(default=_open_range)
    document = models.ForeignKey(
        'core.Document', null=True, blank=True, on_delete=models.SET_NULL,
    )
    run = models.ForeignKey(
        'core.ExtractionRun', null=True, blank=True, on_delete=models.SET_NULL,
    )
    confidence = models.SmallIntegerField()
    method = models.CharField(max_length=20, choices=METHODS)

    class Meta:
        db_table = 'classification'

    def __str__(self):
        return f'{self.entity} → {self.node}'
