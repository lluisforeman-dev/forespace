import os
import logging
from celery import Celery
from celery.signals import worker_init

logger = logging.getLogger(__name__)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.prod')

app = Celery('forespace')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()


@worker_init.connect
def set_redis_eviction_policy(**kwargs):
    """Set allkeys-lru so Redis evicts old keys instead of blocking writes when maxmemory is hit."""
    try:
        import redis
        from django.conf import settings
        r = redis.from_url(settings.CELERY_BROKER_URL)
        r.config_set('maxmemory-policy', 'allkeys-lru')
        logger.info('Redis maxmemory-policy set to allkeys-lru')
    except Exception as exc:
        logger.warning('Could not set Redis maxmemory-policy: %s', exc)

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
