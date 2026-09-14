import uuid
from django.db import models


class LLMCall(models.Model):
    """Every LLM API call logged for cost tracking, auditing, and cost-per-entity analytics.

    Log every call: model, tokens in/out, cost, task, run, entity (§10).
    You need cost-per-entity-per-month to know if the pipeline is economically viable.
    """
    TASKS = [
        ('triage', 'Triage'),
        ('extract', 'Extract'),
        ('resolve', 'Resolve'),
        ('analysis', 'Analysis'),
    ]

    run = models.ForeignKey(
        'core.ExtractionRun', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='llm_calls',
    )
    entity = models.ForeignKey(
        'core.Entity', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='llm_calls',
    )
    task = models.CharField(max_length=20, choices=TASKS)
    model = models.CharField(max_length=100)
    tokens_in = models.IntegerField()
    tokens_out = models.IntegerField()
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6)
    called_at = models.DateTimeField(auto_now_add=True)
    duration_ms = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = 'llm_call'
        indexes = [
            models.Index(fields=['called_at']),
            models.Index(fields=['entity', 'called_at']),
        ]

    def __str__(self):
        return f'{self.task}@{self.model} ${self.cost_usd}'


class ScheduledSource(models.Model):
    """A URL or RSS feed to check on a recurring schedule.

    The scheduler scores all due sources by priority and dispatches crawl tasks.
    """
    CADENCES = [
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
        ('event_driven', 'Event-driven'),
    ]
    FEED_TYPES = [
        ('rss', 'RSS/Atom Feed'),
        ('sitemap', 'Sitemap'),
        ('manual', 'Single URL'),
    ]

    source = models.ForeignKey(
        'core.Source', on_delete=models.PROTECT, related_name='scheduled_sources',
    )
    feed_url = models.TextField()
    feed_type = models.CharField(max_length=20, choices=FEED_TYPES, default='rss')
    cadence = models.CharField(max_length=20, choices=CADENCES, default='weekly')
    is_active = models.BooleanField(default=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    # Optional: if this feed covers only one company, hint the resolver
    entity_hint = models.ForeignKey(
        'core.Entity', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='scheduled_sources',
    )
    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'scheduled_source'

    def __str__(self):
        return f'{self.source.name} — {self.feed_url[:60]}'
