"""Analytics aggregation task — §12.

Computes sector counts, funding totals, and top entities per taxonomy facet.
Writes results to a simple key-value store (Django cache) so the dashboard
can display them without expensive real-time queries.

Run queue: analytics (low priority, can be delayed).
"""
from __future__ import annotations

import logging
from collections import defaultdict

from celery import shared_task
from django.core.cache import cache
from django.db.models import Count, Sum, Q

from core.models import Classification, Entity, Assertion, Taxonomy

logger = logging.getLogger(__name__)

CACHE_KEY = 'forespace:analytics:snapshot'
CACHE_TTL = 60 * 60 * 6   # 6 hours


@shared_task(queue='analytics')
def build_analytics_snapshot():
    """Compute aggregate stats and cache them for the dashboard."""
    snapshot = {}

    # ── Entity counts by type and status ──────────────────────────────────────
    type_counts = (
        Entity.objects
        .values('entity_type', 'status')
        .annotate(n=Count('id'))
    )
    entity_stats: dict = defaultdict(lambda: defaultdict(int))
    for row in type_counts:
        entity_stats[row['entity_type']][row['status']] += row['n']
    snapshot['entity_counts'] = {k: dict(v) for k, v in entity_stats.items()}

    # ── Assertions accepted / candidate ───────────────────────────────────────
    snapshot['assertion_counts'] = {
        s: Assertion.objects.filter(status=s).count()
        for s in ('accepted', 'candidate', 'rejected')
    }

    # ── Funding totals from assertions with attribute_key = 'funding_total_usd' ──
    funding_q = (
        Assertion.objects
        .filter(attribute_id='funding_total_usd', status='accepted', superseded_at__isnull=True)
        .aggregate(total=Sum('value_num'), count=Count('id'))
    )
    snapshot['funding'] = {
        'total_usd': float(funding_q['total'] or 0),
        'data_points': funding_q['count'],
    }

    # ── Taxonomy facet breakdowns ─────────────────────────────────────────────
    facet_counts: dict = {}
    for taxonomy in Taxonomy.objects.filter(status='active').prefetch_related('nodes'):
        node_counts = (
            Classification.objects
            .filter(node__taxonomy=taxonomy, is_primary=True)
            .values('node__label', 'node__path')
            .annotate(n=Count('entity_id', distinct=True))
            .order_by('-n')[:10]
        )
        facet_counts[taxonomy.key] = [
            {'label': r['node__label'], 'path': r['node__path'], 'count': r['n']}
            for r in node_counts
        ]
    snapshot['taxonomy'] = facet_counts

    # ── Top watchlist entities ────────────────────────────────────────────────
    snapshot['watchlist_count'] = Entity.objects.filter(watchlist=True).count()

    # ── Recent pipeline activity (last 24 h) ──────────────────────────────────
    from django.utils import timezone
    from datetime import timedelta
    cutoff = timezone.now() - timedelta(hours=24)
    from core.models import LLMCall
    recent_calls = LLMCall.objects.filter(called_at__gte=cutoff)
    snapshot['last_24h'] = {
        'llm_calls': recent_calls.count(),
        'cost_usd': float(recent_calls.aggregate(t=Sum('cost_usd'))['t'] or 0),
    }

    cache.set(CACHE_KEY, snapshot, CACHE_TTL)
    logger.info('analytics snapshot cached: %d entity types, %d facets',
                len(snapshot['entity_counts']), len(snapshot['taxonomy']))
    return snapshot


def get_snapshot() -> dict:
    """Return cached snapshot, or compute synchronously if cold."""
    data = cache.get(CACHE_KEY)
    if data is None:
        data = build_analytics_snapshot()
    return data
