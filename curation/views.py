"""Curation review interface — §14b of the blueprint.

Staff-only. A curator sees incoming candidates and can accept, correct, or reject.
Goal: a human can correct the graph and the correction sticks.
"""
from urllib.parse import urlparse, quote

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone

from core.models import Assertion, Classification, Entity, EntitySummary, Event, KnowledgeFragment, Relation, Source, ScheduledSource, TaxonomyNode
from ingest.tasks.analytics import get_snapshot


# Curated space industry RSS feeds for auto-news mode
SPACE_NEWS_FEEDS = [
    ('SpaceNews',           'https://spacenews.com/feed/'),
    ('NASASpaceflight',     'https://www.nasaspaceflight.com/feed/'),
    ('SpaceflightNow',      'https://spaceflightnow.com/feed/'),
    ('Ars Technica Space',  'https://feeds.arstechnica.com/arstechnica/space'),
    ('Payload',             'https://payloadspace.com/feed/'),
    ('The Planetary Society', 'https://www.planetary.org/rss/articles'),
    ('Teslarati',           'https://www.teslarati.com/feed/'),
]

CELERY_QUEUES = [
    'crawl', 'parse', 'triage', 'extract',
    'resolve', 'adjudicate', 'project', 'analytics', 'analysis',
]


def _queue_lengths():
    """Return total queued tasks across all Celery queues via Redis."""
    try:
        import redis
        from django.conf import settings
        r = redis.from_url(settings.CELERY_BROKER_URL)
        return sum(r.llen(q) for q in CELERY_QUEUES)
    except Exception:
        return None


def _get_or_create_scheduled_source(source_name, feed_url, kind='trade_press', trust=70):
    domain = urlparse(feed_url).netloc[:255]
    source, _ = Source.objects.get_or_create(
        name=source_name,
        defaults={'kind': kind, 'base_trust': trust, 'domain': domain},
    )
    sched, _ = ScheduledSource.objects.get_or_create(
        source=source,
        feed_url=feed_url,
        defaults={'feed_type': 'rss', 'cadence': 'daily', 'is_active': True},
    )
    return sched


@staff_member_required
def dashboard(request):
    from core.models import Event, ExtractionRun, Relation
    snapshot = get_snapshot()
    recent_entities = (
        Entity.objects
        .exclude(status='merged')
        .order_by('-created_at')[:50]
    )
    recent_runs = (
        ExtractionRun.objects
        .order_by('-started_at')[:30]
    )
    recent_relations = (
        Relation.objects
        .filter(superseded_at__isnull=True)
        .select_related('subject', 'object', 'predicate')
        .order_by('-id')[:30]
    )
    # Event-derived connections: events that link multiple entities
    event_connections = (
        Event.objects
        .filter(participants__isnull=False)
        .select_related('entity')
        .prefetch_related('participants')
        .order_by('-id')
        .distinct()[:30]
    )
    ctx = {
        'stub_count': Entity.objects.filter(status='stub').count(),
        'candidate_count': Assertion.objects.filter(status='candidate').count(),
        'relation_count': Relation.objects.filter(superseded_at__isnull=True).count(),
        'analytics': snapshot,
        'recent_entities': recent_entities,
        'recent_runs': recent_runs,
        'recent_relations': recent_relations,
        'event_connections': event_connections,
        'queued_tasks': _queue_lengths(),
        'num_feeds': len(SPACE_NEWS_FEEDS),
        'title': 'ForeSpace',
    }
    return render(request, 'curation/dashboard.html', ctx)


@staff_member_required
def auto_news(request):
    """Use Sonar to find and extract last week's space news."""
    if request.method != 'POST':
        return HttpResponseRedirect(reverse('curation:dashboard'))
    from ingest.tasks.research import research_topic
    research_topic.delay('space industry news last 7 days', 'news')
    messages.success(request, 'Space news research queued — Sonar is searching the web.')
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def company_research(request):
    """Use Sonar to research a specific company."""
    if request.method != 'POST':
        return HttpResponseRedirect(reverse('curation:dashboard'))
    company = request.POST.get('company', '').strip()
    if not company:
        messages.error(request, 'Enter a company name.')
        return HttpResponseRedirect(reverse('curation:dashboard'))
    from ingest.tasks.research import research_topic
    research_topic.delay(company, 'company')
    messages.success(request, f'Researching "{company}" — Sonar is on it.')
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def question_research(request):
    """Use Sonar to answer a question by searching the web."""
    if request.method != 'POST':
        return HttpResponseRedirect(reverse('curation:dashboard'))
    question = request.POST.get('question', '').strip()
    if not question:
        messages.error(request, 'Enter a question.')
        return HttpResponseRedirect(reverse('curation:dashboard'))
    from ingest.tasks.research import research_topic
    research_topic.delay(question, 'question')
    messages.success(request, f'Queued: "{question}"')
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def stop_all_tasks(request):
    """Purge all pending Celery tasks across every queue."""
    if request.method != 'POST':
        return HttpResponseRedirect(reverse('curation:dashboard'))
    from config.celery import app as celery_app
    discarded = celery_app.control.purge()
    messages.warning(request, f'Stopped — {discarded} queued task(s) discarded. Running tasks will finish naturally.')
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def ingest_trigger(request):
    """Legacy: manual RSS or URL ingest."""
    if request.method != 'POST':
        return HttpResponseRedirect(reverse('curation:dashboard'))
    kind = request.POST.get('kind')
    if kind == 'rss':
        feed_url = request.POST.get('feed_url', '').strip()
        source_name = request.POST.get('source_name', '').strip()
        if feed_url and source_name:
            from ingest.tasks.rss import ingest_rss_feed
            sched = _get_or_create_scheduled_source(source_name, feed_url)
            ingest_rss_feed.delay(sched.id)
            messages.success(request, f'RSS feed queued: {feed_url}')
        else:
            messages.error(request, 'Source name and feed URL are required.')
    elif kind == 'url':
        url = request.POST.get('url', '').strip()
        source_name = request.POST.get('source_name', 'manual').strip() or 'manual'
        if url:
            from ingest.tasks.crawl import crawl_url
            crawl_url.delay(url, source_name)
            messages.success(request, f'URL queued: {url}')
        else:
            messages.error(request, 'URL is required.')
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def stubs(request):
    stub_list = (
        Entity.objects
        .filter(status='stub')
        .prefetch_related('aliases', 'assertions')
        .order_by('-created_at')[:100]
    )
    return render(request, 'curation/stubs.html', {
        'stubs': stub_list,
        'title': 'Stub Entities',
    })


@staff_member_required
def promote_stub(request, entity_id):
    """Promote a stub to active (curator has verified it's a real entity)."""
    if request.method != 'POST':
        return HttpResponseRedirect(reverse('curation:stubs'))
    entity = get_object_or_404(Entity, pk=entity_id, status='stub')
    canonical = request.POST.get('canonical_name', '').strip()
    with transaction.atomic():
        if canonical:
            entity.canonical_name = canonical
        entity.status = 'active'
        entity.save(update_fields=['canonical_name', 'status'])
        # Re-accept any candidate assertions on this entity
        Assertion.objects.filter(
            entity=entity, status='candidate', confidence__gte=50,
        ).update(status='accepted')
    return HttpResponseRedirect(reverse('curation:stubs'))



@staff_member_required
def candidates(request):
    candidate_list = (
        Assertion.objects
        .filter(status='candidate')
        .select_related('entity', 'attribute', 'document__source')
        .order_by('-confidence', '-observed_at')[:200]
    )
    return render(request, 'curation/candidates.html', {
        'candidates': candidate_list,
        'title': 'Candidate Assertions',
    })


@staff_member_required
def review_assertion(request, assertion_id):
    """Accept or reject a single candidate assertion."""
    if request.method != 'POST':
        return HttpResponseRedirect(reverse('curation:candidates'))

    assertion = get_object_or_404(Assertion, pk=assertion_id, status='candidate')
    action = request.POST.get('action')

    with transaction.atomic():
        if action == 'accept':
            assertion.status = 'accepted'
            assertion.review_state = 'approved'
        elif action == 'reject':
            assertion.status = 'rejected'
            assertion.review_state = 'corrected'
        assertion.save(update_fields=['status', 'review_state'])

    return HttpResponseRedirect(reverse('curation:candidates'))


@staff_member_required
def entity_profile(request, entity_id):
    """Full profile view for a single entity — assertions, relations, classifications."""
    entity = get_object_or_404(Entity, pk=entity_id)

    if request.method == 'POST':
        new_type = request.POST.get('entity_type')
        new_name = request.POST.get('canonical_name', '').strip()
        new_status = request.POST.get('status')
        valid_types = [t[0] for t in Entity.ENTITY_TYPES]
        valid_statuses = [s[0] for s in Entity.STATUS_CHOICES]
        if new_type and new_type in valid_types:
            entity.entity_type = new_type
        if new_name:
            entity.canonical_name = new_name
        if new_status and new_status in valid_statuses:
            entity.status = new_status
        entity.save()
        return redirect('curation:entity_profile', entity_id=entity_id)

    all_assertions = (
        Assertion.objects
        .filter(entity=entity, superseded_at__isnull=True)
        .select_related('attribute', 'document__source')
        .order_by('attribute_id', '-confidence')
    )

    # ── Aggregate facts: best assertion per attribute + source count ──────
    from django.db.models import Count
    source_counts = {
        row['attribute_id']: row['n']
        for row in (
            Assertion.objects
            .filter(entity=entity, superseded_at__isnull=True)
            .values('attribute_id')
            .annotate(n=Count('document_id', distinct=True))
        )
    }
    best_per_attr = {}
    for a in all_assertions.filter(status='accepted'):
        if a.attribute_id not in best_per_attr:
            best_per_attr[a.attribute_id] = a
    facts = sorted(best_per_attr.values(), key=lambda a: -a.confidence)
    for f in facts:
        f.source_count = source_counts.get(f.attribute_id, 1)

    relations_out = (
        Relation.objects
        .filter(subject=entity, superseded_at__isnull=True)
        .select_related('object')
        .order_by('predicate')[:50]
    )
    relations_in = (
        Relation.objects
        .filter(object=entity, superseded_at__isnull=True)
        .select_related('subject')
        .order_by('predicate')[:50]
    )

    classifications = (
        Classification.objects
        .filter(entity=entity)
        .select_related('node__taxonomy')
        .order_by('node__taxonomy__key', '-weight')
    )

    try:
        summary = entity.summary
    except EntitySummary.DoesNotExist:
        summary = None

    events = (
        Event.objects
        .filter(entity=entity)
        .prefetch_related('participants')
        .order_by('date', 'created_at')[:60]
    )

    # Group fragments by category
    from itertools import groupby
    raw_fragments = list(
        KnowledgeFragment.objects
        .filter(entity=entity)
        .select_related('source')
        .order_by('category', '-date_of_information', '-created_at')[:80]
    )
    fragments_by_category = {}
    for frag in raw_fragments:
        fragments_by_category.setdefault(frag.category, []).append(frag)

    return render(request, 'curation/entity_profile.html', {
        'entity':               entity,
        'summary':              summary,
        'events':               events,
        'fragments_by_category': fragments_by_category,
        'facts':                facts,
        'assertions':           all_assertions,
        'relations_out':        relations_out,
        'relations_in':         relations_in,
        'classifications':      classifications,
        'title':                entity.canonical_name,
    })


@staff_member_required
def connections(request):
    """Network connections page — typed relations and event-based links."""
    from core.models import PredicateDef
    from django.db.models import Q

    q = request.GET.get('q', '').strip()
    predicate_filter = request.GET.get('predicate', '').strip()
    entity_type_filter = request.GET.get('entity_type', '').strip()

    # ── Typed relations ───────────────────────────────────────────────────
    relations_qs = (
        Relation.objects
        .filter(superseded_at__isnull=True)
        .select_related('subject', 'object', 'predicate')
    )
    if q:
        relations_qs = relations_qs.filter(
            Q(subject__canonical_name__icontains=q) |
            Q(object__canonical_name__icontains=q)
        )
    if predicate_filter:
        relations_qs = relations_qs.filter(predicate_id=predicate_filter)
    if entity_type_filter:
        relations_qs = relations_qs.filter(
            Q(subject__entity_type=entity_type_filter) |
            Q(object__entity_type=entity_type_filter)
        )
    relations_qs = relations_qs.order_by('-confidence', '-id')[:200]

    # ── Event-based connections ───────────────────────────────────────────
    events_qs = (
        Event.objects
        .filter(participants__isnull=False)
        .select_related('entity')
        .prefetch_related('participants')
        .distinct()
    )
    if q:
        events_qs = events_qs.filter(
            Q(entity__canonical_name__icontains=q) |
            Q(participants__canonical_name__icontains=q) |
            Q(title__icontains=q)
        ).distinct()
    if entity_type_filter:
        events_qs = events_qs.filter(
            Q(entity__entity_type=entity_type_filter) |
            Q(participants__entity_type=entity_type_filter)
        ).distinct()
    events_qs = events_qs.order_by('-id')[:200]

    predicates = PredicateDef.objects.order_by('label')
    entity_types = Entity.objects.values_list('entity_type', flat=True).distinct().order_by('entity_type')

    return render(request, 'curation/connections.html', {
        'relations': relations_qs,
        'event_connections': events_qs,
        'predicates': predicates,
        'entity_types': entity_types,
        'q': q,
        'predicate_filter': predicate_filter,
        'entity_type_filter': entity_type_filter,
        'relation_count': Relation.objects.filter(superseded_at__isnull=True).count(),
        'event_connection_count': Event.objects.filter(participants__isnull=False).distinct().count(),
        'title': 'Connections',
    })


@staff_member_required
def taxonomy_proposals(request):
    """Review LLM-proposed taxonomy nodes — approve or reject."""
    if request.method == 'POST':
        node_id = request.POST.get('node_id')
        action = request.POST.get('action')
        node = get_object_or_404(TaxonomyNode, pk=node_id, status='proposed')
        if action == 'approve':
            node.status = 'active'
            node.save()
            messages.success(request, f'Approved: {node.taxonomy.key}/{node.path}')
        elif action == 'reject':
            node.status = 'deprecated'
            node.save()
            messages.info(request, f'Rejected: {node.path}')
        return HttpResponseRedirect(request.path)

    proposals = (
        TaxonomyNode.objects
        .filter(status='proposed')
        .select_related('taxonomy')
        .order_by('taxonomy__key', 'path')
    )
    return render(request, 'curation/taxonomy_proposals.html', {
        'proposals': proposals,
        'title': 'Taxonomy Proposals',
    })


@staff_member_required
def run_evolve_taxonomy(request):
    """Trigger a taxonomy evolution run."""
    if request.method == 'POST':
        from ingest.tasks.evolve import evolve_taxonomy
        evolve_taxonomy.delay()
        messages.success(request, 'Taxonomy evolution task queued.')
    return HttpResponseRedirect(reverse('curation:taxonomy_proposals'))
