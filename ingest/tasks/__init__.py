# Re-export all tasks so Celery autodiscover_tasks() finds them
from ingest.tasks.crawl import crawl_url
from ingest.tasks.parse import parse_document
from ingest.tasks.triage import triage_document
from ingest.tasks.extract import extract_document
from ingest.tasks.resolve import resolve_mention
from ingest.tasks.adjudicate import adjudicate_assertions
from ingest.tasks.project import refresh_entity_current, refresh_relation_current
from ingest.tasks.rss import ingest_rss_feed
from ingest.tasks.schedule import dispatch_scheduled_sources
from ingest.tasks.classify import classify_entity
from ingest.tasks.relate import extract_relations
from ingest.tasks.analytics import build_analytics_snapshot

__all__ = [
    'crawl_url', 'parse_document', 'triage_document',
    'extract_document', 'resolve_mention',
    'adjudicate_assertions', 'refresh_entity_current', 'refresh_relation_current',
    'ingest_rss_feed', 'dispatch_scheduled_sources',
    'classify_entity', 'extract_relations', 'build_analytics_snapshot',
]
