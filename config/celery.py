import os
import logging
from celery import Celery

logger = logging.getLogger(__name__)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.prod')

# Set eviction policy before any broker writes so OOM doesn't block queue declarations.
try:
    import redis as _redis
    _r = _redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'))
    _r.config_set('maxmemory-policy', 'allkeys-lru')
    logger.info('Redis maxmemory-policy set to allkeys-lru')
except Exception as _exc:
    logger.warning('Could not set Redis maxmemory-policy: %s', _exc)

app = Celery('eigengraph')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

# Pipeline queues — each independently scalable (see blueprint §15).
# (task_queues_from_string is not a real Celery setting — it was silently
# ignored and only worked because queues get auto-created on demand.)
from kombu import Queue

app.conf.task_queues = [
    Queue(name) for name in (
        'crawl',        # I/O-bound, domain rate-limited, no LLM
        'parse',        # CPU-bound: HTML/PDF → text
        'triage',       # cheap LLM relevance check
        'extract',      # expensive LLM schema-constrained extraction
        'resolve',      # entity linking, occasional LLM adjudication
        'adjudicate',   # conflict detection + confidence scoring, pure compute
        'project',      # REFRESH MATERIALIZED VIEW, batched per run
        'analytics',    # nightly batch jobs
        'analysis',     # on-demand user-triggered plan/retrieve/synthesize
        'default',      # classify_supply_chain and misc
    )
]
app.conf.task_default_queue = 'extract'


# ── Periodic maintenance without celery-beat ─────────────────────────────────
# There is no beat process in the topology (Procfile: web + worker only), so
# recurring jobs are kicked by worker_ready and kept alive by self-rescheduling
# tasks. The Redis lock makes N workers schedule at most one chain.
from celery.signals import worker_ready  # noqa: E402


@worker_ready.connect
def _kick_periodic_maintenance(**_kwargs):
    try:
        from django.conf import settings as _settings
        import redis as _r
        r = _r.from_url(_settings.CELERY_BROKER_URL)
        if r.set('eigengraph:periodic:maintenance', '1', nx=True, ex=26 * 3600):
            from ingest.tasks.dedup import periodic_maintenance
            periodic_maintenance.apply_async(countdown=600)
            logger.info('periodic maintenance scheduled (+10 min)')
        else:
            logger.info('periodic maintenance already scheduled (lock held)')
    except Exception as exc:
        logger.warning('could not schedule periodic maintenance: %s', exc)
