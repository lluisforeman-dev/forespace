from django.db import models
from django.contrib.postgres.fields import DateTimeRangeField, ArrayField
from django.utils import timezone
from psycopg2.extras import DateTimeTZRange


def _open_range():
    """Default valid_range: starts now, no end (currently true)."""
    return DateTimeTZRange(timezone.now(), None)


class PredicateDef(models.Model):
    """Controlled vocabulary for relation predicates."""
    key = models.CharField(max_length=100, primary_key=True)
    label = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    class Meta:
        db_table = 'predicate_def'

    def __str__(self):
        return self.key


class AttributeDef(models.Model):
    """
    Attributes as data — adding a new tracked attribute is an INSERT, not a migration.
    The classifier prompt reads `definition`; write it carefully.
    """
    DATATYPES = [
        ('int', 'Integer'),
        ('decimal', 'Decimal'),
        ('text', 'Text'),
        ('date', 'Date'),
        ('bool', 'Boolean'),
        ('money', 'Money'),
        ('geo', 'Geographic'),
        ('enum', 'Enum'),
        ('taxonomy_ref', 'Taxonomy Reference'),
    ]
    CARDINALITY = [
        ('single', 'Single'),
        ('multi', 'Multi'),
    ]

    key = models.CharField(max_length=100, primary_key=True)
    entity_type = models.CharField(max_length=30)
    label = models.CharField(max_length=255)
    datatype = models.CharField(max_length=20, choices=DATATYPES)
    unit = models.CharField(max_length=20, null=True, blank=True)  # 'USD', 'kg', 'km'
    cardinality = models.CharField(max_length=10, choices=CARDINALITY, default='single')
    taxonomy = models.ForeignKey(
        'core.Taxonomy', null=True, blank=True, on_delete=models.SET_NULL,
    )
    is_projected = models.BooleanField(default=True)   # appears in entity_current view
    is_promoted = models.BooleanField(default=False)   # has own typed column in entity_current
    volatility_days = models.IntegerField(null=True, blank=True)  # feeds scheduling + trust decay
    description = models.TextField(blank=True)

    class Meta:
        db_table = 'attribute_def'

    def __str__(self):
        return f'{self.key} ({self.entity_type})'


class Assertion(models.Model):
    """
    Append-only. UPDATE is almost never correct — insert a new assertion instead.
    Every value on screen must have a path back to a sentence in a source document.
    """
    METHODS = [
        ('extracted', 'Extracted'),
        ('structured_api', 'Structured API'),
        ('manual', 'Manual'),
        ('derived', 'Derived'),
        ('imputed', 'Imputed'),
        ('synthesis', 'Synthesis'),
    ]
    STATUSES = [
        ('accepted', 'Accepted'),
        ('candidate', 'Candidate'),
        ('rejected', 'Rejected'),
        ('superseded', 'Superseded'),
    ]
    REVIEW_STATES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('corrected', 'Corrected'),
    ]

    entity = models.ForeignKey('core.Entity', on_delete=models.CASCADE, related_name='assertions')
    attribute = models.ForeignKey(
        AttributeDef, on_delete=models.PROTECT,
        db_column='attribute_key', to_field='key',
    )

    # Typed value columns — exactly one populated per datatype
    value_text = models.TextField(null=True, blank=True)
    value_num = models.DecimalField(max_digits=30, decimal_places=10, null=True, blank=True)
    value_bool = models.BooleanField(null=True, blank=True)
    value_date = models.DateField(null=True, blank=True)
    value_json = models.JSONField(null=True, blank=True)
    value_entity = models.ForeignKey(
        'core.Entity', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='referenced_in_assertions',
    )
    unit = models.CharField(max_length=20, null=True, blank=True)
    value_low = models.DecimalField(max_digits=30, decimal_places=10, null=True, blank=True)
    value_high = models.DecimalField(max_digits=30, decimal_places=10, null=True, blank=True)

    # Time — bitemporality: valid_range = when true in the world, observed_at = when we learned it
    valid_range = DateTimeRangeField(default=_open_range)
    observed_at = models.DateTimeField(auto_now_add=True)
    superseded_at = models.DateTimeField(null=True, blank=True)

    # Provenance
    document = models.ForeignKey(
        'core.Document', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='assertions',
    )
    quote = models.TextField(null=True, blank=True)       # verbatim sentence from the document
    char_start = models.IntegerField(null=True, blank=True)
    char_end = models.IntegerField(null=True, blank=True)
    run = models.ForeignKey(
        'core.ExtractionRun', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='assertions',
    )
    method = models.CharField(max_length=20, choices=METHODS)
    derived_from = ArrayField(models.BigIntegerField(), null=True, blank=True)  # assertion ids

    # Quality
    confidence = models.SmallIntegerField()               # 0-100, computed (not self-reported)
    confidence_model_version = models.CharField(max_length=20, default='v1')
    status = models.CharField(max_length=20, choices=STATUSES, default='accepted')
    review_state = models.CharField(max_length=20, choices=REVIEW_STATES, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'assertion'
        indexes = [
            models.Index(
                fields=['entity', 'attribute', 'status'],
                condition=models.Q(superseded_at__isnull=True),
                name='assertion_active_idx',
            ),
            models.Index(fields=['document']),
        ]
        constraints = [
            # If a model can't point at the sentence, the claim does not enter the graph.
            # This eliminates the majority of hallucination paths.
            models.CheckConstraint(
                check=(
                    ~models.Q(method='extracted') |
                    (models.Q(document__isnull=False) & models.Q(quote__isnull=False))
                ),
                name='extracted_needs_source',
            ),
        ]
        # Note: the GiST exclusion constraint (no overlapping accepted validity windows)
        # cannot be expressed in Django ORM — add via RunSQL migration:
        #   ALTER TABLE assertion ADD CONSTRAINT no_overlapping_validity
        #   EXCLUDE USING gist (entity_id WITH =, attribute_key WITH =, valid_range WITH &&)
        #   WHERE (status = 'accepted' AND superseded_at IS NULL);

    def __str__(self):
        return f'{self.entity} · {self.attribute_id}'


class Conflict(models.Model):
    SEVERITIES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    ]
    RESOLUTIONS = [
        ('picked', 'Picked one'),
        ('range', 'Used range'),
        ('both_wrong', 'Both wrong'),
        ('pending', 'Pending'),
    ]

    entity = models.ForeignKey('core.Entity', on_delete=models.CASCADE, related_name='conflicts')
    attribute_key = models.CharField(max_length=100)
    assertion_ids = ArrayField(models.BigIntegerField())
    detected_at = models.DateTimeField(auto_now_add=True)
    severity = models.CharField(max_length=10, choices=SEVERITIES)
    resolution = models.CharField(max_length=20, choices=RESOLUTIONS, null=True, blank=True)
    resolved_by = models.CharField(max_length=255, null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'conflict'

    def __str__(self):
        return f'Conflict: {self.entity} · {self.attribute_key} [{self.severity}]'


class Relation(models.Model):
    METHODS = [
        ('extracted', 'Extracted'),
        ('structured_api', 'Structured API'),
        ('manual', 'Manual'),
        ('derived', 'Derived'),
    ]
    STATUSES = [
        ('accepted', 'Accepted'),
        ('candidate', 'Candidate'),
        ('rejected', 'Rejected'),
    ]

    subject = models.ForeignKey(
        'core.Entity', on_delete=models.CASCADE, related_name='subject_relations',
    )
    predicate = models.ForeignKey(
        PredicateDef, on_delete=models.PROTECT, to_field='key', db_column='predicate',
    )
    object = models.ForeignKey(
        'core.Entity', on_delete=models.CASCADE, related_name='object_relations',
    )
    qualifiers = models.JSONField(default=dict)  # role, share, tier, contract value
    valid_range = DateTimeRangeField(default=_open_range)
    observed_at = models.DateTimeField(auto_now_add=True)
    superseded_at = models.DateTimeField(null=True, blank=True)
    document = models.ForeignKey(
        'core.Document', null=True, blank=True, on_delete=models.SET_NULL,
    )
    quote = models.TextField(null=True, blank=True)
    run = models.ForeignKey(
        'core.ExtractionRun', null=True, blank=True, on_delete=models.SET_NULL,
    )
    method = models.CharField(max_length=20, choices=METHODS)
    confidence = models.SmallIntegerField()
    status = models.CharField(max_length=20, choices=STATUSES, default='accepted')

    class Meta:
        db_table = 'relation'
        indexes = [
            models.Index(
                fields=['subject', 'predicate'],
                condition=models.Q(superseded_at__isnull=True),
                name='relation_subject_idx',
            ),
            models.Index(
                fields=['object', 'predicate'],
                condition=models.Q(superseded_at__isnull=True),
                name='relation_object_idx',
            ),
        ]

    def __str__(self):
        return f'{self.subject} --{self.predicate_id}--> {self.object}'
