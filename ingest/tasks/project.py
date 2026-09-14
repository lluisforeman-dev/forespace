"""Stage 6 — Project.

Refreshes the entity_current materialized view concurrently after a pipeline run.
DB-bound, no LLM.  Batched — called once per pipeline run, not per assertion.
"""
import logging

from celery import shared_task
from django.db import connection

logger = logging.getLogger(__name__)


@shared_task(bind=True, queue='project', max_retries=1)
def refresh_entity_current(self):
    """REFRESH MATERIALIZED VIEW CONCURRENTLY entity_current."""
    try:
        with connection.cursor() as cursor:
            cursor.execute('REFRESH MATERIALIZED VIEW CONCURRENTLY entity_current;')
        logger.info('project: entity_current refreshed')
    except Exception as exc:
        logger.error('project: refresh failed: %s', exc)
        raise self.retry(exc=exc)
