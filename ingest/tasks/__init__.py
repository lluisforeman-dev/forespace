# Re-export all tasks so Celery autodiscover_tasks() finds them
from ingest.tasks.crawl import crawl_url
from ingest.tasks.parse import parse_document
from ingest.tasks.triage import triage_document
from ingest.tasks.extract import extract_document
from ingest.tasks.resolve import resolve_mention

__all__ = ['crawl_url', 'parse_document', 'triage_document', 'extract_document', 'resolve_mention']
