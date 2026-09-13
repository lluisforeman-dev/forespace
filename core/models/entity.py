import uuid
from django.db import models
from django.contrib.postgres.fields import DateTimeRangeField


class Entity(models.Model):
    ENTITY_TYPES = [
        ('organization', 'Organization'),
        ('facility', 'Facility'),
        ('asset', 'Asset'),
        ('person', 'Person'),
        ('document_node', 'Document'),
        ('event', 'Event'),
        ('program', 'Program'),
    ]
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('merged', 'Merged'),
        ('disputed', 'Disputed'),
        ('stub', 'Stub'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entity_type = models.CharField(max_length=30, choices=ENTITY_TYPES)
    canonical_name = models.CharField(max_length=500)
    slug = models.SlugField(max_length=255, unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    # Set when merged — never delete the old entity, just redirect
    redirects_to = models.ForeignKey(
        'self', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='redirected_from',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'entity'
        indexes = [
            models.Index(fields=['entity_type', 'status']),
        ]

    def __str__(self):
        return self.canonical_name


class EntityIdentifier(models.Model):
    """External anchors — the single most valuable table for entity resolution quality."""
    SCHEMES = [
        ('wikidata', 'Wikidata QID'),
        ('lei', 'LEI'),
        ('cik', 'SEC CIK'),
        ('duns', 'DUNS'),
        ('domain', 'Domain'),
        ('norad', 'NORAD ID'),
        ('cospar', 'COSPAR ID'),
        ('ror', 'ROR'),
        ('crunchbase', 'Crunchbase'),
        ('isni', 'ISNI'),
    ]

    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name='identifiers')
    scheme = models.CharField(max_length=30, choices=SCHEMES)
    value = models.CharField(max_length=255)
    confidence = models.SmallIntegerField(default=100)
    document = models.ForeignKey(
        'core.Document', null=True, blank=True, on_delete=models.SET_NULL,
    )

    class Meta:
        db_table = 'entity_identifier'
        # Blueprint: PRIMARY KEY (scheme, value) — one external ID maps to exactly one entity
        unique_together = [('scheme', 'value')]

    def __str__(self):
        return f'{self.scheme}:{self.value}'


class EntityAlias(models.Model):
    ALIAS_KINDS = [
        ('legal', 'Legal Name'),
        ('trading', 'Trading Name'),
        ('former', 'Former Name'),
        ('ticker', 'Ticker'),
        ('abbrev', 'Abbreviation'),
        ('misspelling', 'Misspelling'),
        ('translit', 'Transliteration'),
    ]

    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name='aliases')
    alias = models.CharField(max_length=500)
    alias_norm = models.CharField(max_length=500)  # casefolded, unpunctuated, suffix-stripped
    alias_kind = models.CharField(max_length=20, choices=ALIAS_KINDS)
    lang = models.CharField(max_length=2, null=True, blank=True)
    valid_range = DateTimeRangeField(null=True, blank=True)  # former names have an end date
    document = models.ForeignKey(
        'core.Document', null=True, blank=True, on_delete=models.SET_NULL,
    )

    class Meta:
        db_table = 'entity_alias'
        indexes = [
            models.Index(fields=['alias_norm']),
            # A trigram index on alias_norm is added via migration:
            # CREATE INDEX entity_alias_trgm ON entity_alias USING gin (alias_norm gin_trgm_ops);
        ]

    def __str__(self):
        return f'{self.alias} ({self.alias_kind})'


class EntityMerge(models.Model):
    """Audit log for entity merges. Merges must be reversible — set reverted_at to undo."""

    kept = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name='merges_kept')
    merged = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name='merges_merged')
    merged_at = models.DateTimeField(auto_now_add=True)
    performed_by = models.CharField(max_length=255)  # 'auto:resolver@v3' or a username
    rationale = models.JSONField()  # scores, matched identifiers — for auditing and undoing
    reverted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'entity_merge'

    def __str__(self):
        return f'{self.merged} → {self.kept}'
