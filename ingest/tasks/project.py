"""Stage 6 — Project.

Refreshes entity_current and relation_current materialized views concurrently
after a pipeline run. DB-bound, no LLM. Called once per pipeline run, not per assertion.
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
        logger.error('project: refresh entity_current failed: %s', exc)
        raise self.retry(exc=exc)


@shared_task(bind=True, queue='project', max_retries=1)
def refresh_relation_current(self):
    """REFRESH MATERIALIZED VIEW CONCURRENTLY relation_current."""
    try:
        with connection.cursor() as cursor:
            cursor.execute('REFRESH MATERIALIZED VIEW CONCURRENTLY relation_current;')
        logger.info('project: relation_current refreshed')
    except Exception as exc:
        logger.error('project: refresh relation_current failed: %s', exc)
        raise self.retry(exc=exc)
