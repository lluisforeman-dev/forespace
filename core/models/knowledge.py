from django.db import models


class Event(models.Model):
    """A discrete moment in an entity's history — launch, funding round, pivot, failure, etc."""

    EVENT_TYPES = [
        ('funding_round',   'Funding Round'),
        ('launch',          'Launch'),
        ('contract_award',  'Contract Award'),
        ('partnership',     'Partnership'),
        ('acquisition',     'Acquisition'),
        ('failure',         'Failure / Anomaly'),
        ('pivot',           'Strategic Pivot'),
        ('regulatory',      'Regulatory'),
        ('milestone',       'Milestone'),
        ('leadership',      'Leadership Change'),
    ]
    SIGNIFICANCE = [
        ('high',   'High'),
        ('medium', 'Medium'),
        ('low',    'Low'),
    ]
    DATE_PRECISION = [
        ('day',   'Day'),
        ('month', 'Month'),
        ('year',  'Year'),
    ]

    entity       = models.ForeignKey('core.Entity', on_delete=models.CASCADE, related_name='events')
    event_type   = models.CharField(max_length=30, choices=EVENT_TYPES)
    title        = models.CharField(max_length=500)
    date         = models.DateField(null=True, blank=True)
    date_precision = models.CharField(max_length=10, choices=DATE_PRECISION, default='year')
    description  = models.TextField()
    amount_usd   = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    significance = models.CharField(max_length=10, choices=SIGNIFICANCE, default='medium')
    confidence   = models.SmallIntegerField(default=60)
    participants = models.ManyToManyField(
        'core.Entity', related_name='participated_events', blank=True,
    )
    source       = models.ForeignKey(
        'core.Document', null=True, blank=True, on_delete=models.SET_NULL,
    )
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'entity_event'
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f'{self.entity} · {self.event_type} · {self.date}'


class KnowledgeFragment(models.Model):
    """A rich narrative paragraph about an entity — tech, strategy, challenges, competition."""

    CATEGORIES = [
        ('technical',     'Technical'),
        ('financial',     'Financial'),
        ('competitive',   'Competitive'),
        ('regulatory',    'Regulatory'),
        ('strategic',     'Strategic'),
        ('operational',   'Operational'),
        ('people',        'People'),
        ('challenge',     'Challenge'),
    ]

    entity              = models.ForeignKey('core.Entity', on_delete=models.CASCADE, related_name='fragments')
    category            = models.CharField(max_length=20, choices=CATEGORIES)
    text                = models.TextField()
    date_of_information = models.DateField(null=True, blank=True)
    confidence          = models.SmallIntegerField(default=60)
    source              = models.ForeignKey(
        'core.Document', null=True, blank=True, on_delete=models.SET_NULL,
    )
    created_at          = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'knowledge_fragment'
        ordering = ['-date_of_information', '-created_at']

    def __str__(self):
        return f'{self.entity} · {self.category}'


class EntitySummary(models.Model):
    """LLM-synthesised prose portrait of an entity, rebuilt as new intelligence arrives."""

    entity               = models.OneToOneField('core.Entity', on_delete=models.CASCADE, related_name='summary')
    overview             = models.TextField()
    challenges           = models.JSONField(default=list)
    strategic_bets       = models.JSONField(default=list)
    competitive_position = models.TextField(blank=True)
    last_synthesised     = models.DateTimeField(auto_now=True)
    source_count         = models.IntegerField(default=0)

    class Meta:
        db_table = 'entity_summary'

    def __str__(self):
        return f'Summary: {self.entity}'
