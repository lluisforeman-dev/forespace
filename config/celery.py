import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.prod')

app = Celery('forespace')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

# Pipeline queues — each independently scalable (see blueprint §15)
app.conf.task_queues_from_string = (
    'crawl',        # I/O-bound, domain rate-limited, no LLM
    'parse',        # CPU-bound: HTML/PDF → text
    'triage',       # cheap LLM relevance check
    'extract',      # expensive LLM schema-constrained extraction
    'resolve',      # entity linking, occasional LLM adjudication
    'adjudicate',   # conflict detection + confidence scoring, pure compute
    'project',      # REFRESH MATERIALIZED VIEW, batched per run
    'analytics',    # nightly igraph batch jobs
    'analysis',     # on-demand user-triggered plan/retrieve/synthesize
)
app.conf.task_default_queue = 'extract'
