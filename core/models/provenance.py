import uuid
from django.db import models


class Source(models.Model):
    SOURCE_KINDS = [
        ('regulator', 'Regulator'),
        ('primary', 'Primary'),
        ('trade_press', 'Trade Press'),
        ('aggregator', 'Aggregator'),
        ('social', 'Social'),
        ('llm', 'LLM'),
        ('external_contributor', 'External Contributor'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True)
    kind = models.CharField(max_length=30, choices=SOURCE_KINDS)
    domain = models.CharField(max_length=255, null=True, blank=True)
    base_trust = models.SmallIntegerField(default=50)  # 0-100, hand-set
    robots_policy = models.TextField(null=True, blank=True)
    license_note = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'source'

    def __str__(self):
        return self.name


class Document(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name='documents')
    url = models.TextField(null=True, blank=True)
    # SHA-256 hex digest — unique dedupe key: same bytes = same document, skip extraction
    content_sha256 = models.CharField(max_length=64, unique=True)
    storage_key = models.TextField()  # object storage path for raw bytes
    media_type = models.CharField(max_length=100, null=True, blank=True)
    title = models.TextField(null=True, blank=True)
    lang = models.CharField(max_length=2, null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    fetched_at = models.DateTimeField(auto_now_add=True)
    text_content = models.TextField(null=True, blank=True)
    # pgvector embedding — uncomment after: pip install pgvector + CREATE EXTENSION vector;
    # from pgvector.django import VectorField
    # embedding = VectorField(dimensions=1536, null=True, blank=True)
    trust_override = models.SmallIntegerField(null=True, blank=True)
    PIPELINE_STATUSES = [
        ('fetched', 'Fetched'),
        ('parsed', 'Parsed'),
        ('triaged', 'Triaged'),
        ('skipped', 'Skipped'),
        ('extracted', 'Extracted'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ]
    pipeline_status = models.CharField(
        max_length=20, choices=PIPELINE_STATUSES, default='fetched',
    )

    class Meta:
        db_table = 'document'
        indexes = [
            models.Index(fields=['source', '-published_at']),
        ]

    def __str__(self):
        return self.title or str(self.id)

    @property
    def effective_trust(self):
        return self.trust_override if self.trust_override is not None else self.source.base_trust


class ExtractionRun(models.Model):
    STATUS_CHOICES = [
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('partial', 'Partial'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.CharField(max_length=100)         # 'company_profile', 'funding_round', ...
    prompt_sha256 = models.CharField(max_length=64) # exact prompt template used
    model = models.CharField(max_length=100)
    code_version = models.CharField(max_length=40)  # git sha
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='running')
    stats = models.JSONField(default=dict)

    class Meta:
        db_table = 'extraction_run'

    def __str__(self):
        return f'{self.task} @ {self.started_at:%Y-%m-%d %H:%M}'
