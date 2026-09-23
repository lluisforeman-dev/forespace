"""Curation review interface — §14b of the blueprint.

Staff-only. A curator sees incoming candidates and can accept, correct, or reject.
Goal: a human can correct the graph and the correction sticks.
"""
from urllib.parse import urlparse, quote

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone

from core.models import Assertion, Classification, Entity, EntityAlias, EntitySummary, Event, KnowledgeFragment, PromptTemplate, Relation, Source, ScheduledSource, TaxonomyNode
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


def _worker_status():
    """Ping Celery workers. Cached 60s so dashboard load stays fast."""
    from django.core.cache import cache
    cached = cache.get('forespace:worker:status')
    if cached is not None:
        return cached
    try:
        from config.celery import app as celery_app
        result = celery_app.control.inspect(timeout=2).ping() or {}
        status = {'online': bool(result), 'workers': list(result.keys())}
    except Exception:
        status = {'online': False, 'workers': []}
    cache.set('forespace:worker:status', status, 60)
    return status


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
    total_active = Entity.objects.exclude(status='merged').count()
    with_summary = EntitySummary.objects.count()
    with_score = Entity.objects.exclude(status='merged').exclude(space_relevance__isnull=True).count()
    recently_assessed = (
        EntitySummary.objects
        .select_related('entity')
        .order_by('-last_synthesised')[:40]
    )

    from ingest.pause import paused_operations
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
        'worker_status': _worker_status(),
        'num_feeds': len(SPACE_NEWS_FEEDS),
        'title': 'ForeSpace',
        'total_active': total_active,
        'with_summary': with_summary,
        'with_score': with_score,
        'recently_assessed': recently_assessed,
        'paused_ops': paused_operations(),
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
    """Flush all pending Celery tasks and set global pause flag."""
    if request.method != 'POST':
        return HttpResponseRedirect(reverse('curation:dashboard'))

    import redis as _redis
    from django.conf import settings as _settings
    from ingest.pause import pause

    r = _redis.from_url(_settings.CELERY_BROKER_URL)
    pause()  # set global flag — every task checks this

    discarded = sum(r.llen(q) for q in CELERY_QUEUES)
    keys_to_delete = list(CELERY_QUEUES)
    for pattern in ('_kombu.binding.*', 'unacked*', 'celery-task-meta-*'):
        keys_to_delete.extend(k.decode() if isinstance(k, bytes) else k for k in r.keys(pattern))
    for key in keys_to_delete:
        r.delete(key)

    messages.warning(request, f'Stopped — {discarded} queued task(s) flushed. Click Resume when ready.')
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def stop_operation(request, operation: str):
    """Pause a named operation without flushing other queues."""
    if request.method != 'POST':
        return HttpResponseRedirect(reverse('curation:dashboard'))
    from ingest.pause import pause, OPERATIONS
    if operation not in OPERATIONS:
        messages.error(request, f'Unknown operation: {operation}')
    else:
        pause(operation)
        messages.warning(request, f'Paused: {operation}. New tasks of this type will be dropped.')
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def resume_operation(request, operation: str = None):
    """Resume a named operation (or all if operation is 'all')."""
    if request.method != 'POST':
        return HttpResponseRedirect(reverse('curation:dashboard'))
    from ingest.pause import resume
    resume(None if operation == 'all' else operation)
    label = 'all operations' if operation == 'all' else operation
    messages.success(request, f'Resumed: {label}.')
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def resume_tasks(request):
    """Clear all pause flags."""
    if request.method != 'POST':
        return HttpResponseRedirect(reverse('curation:dashboard'))
    from ingest.pause import resume
    resume(None)  # clears global + all named flags
    messages.success(request, 'All operations resumed.')
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
        .select_related('object', 'document', 'document__source')
        .order_by('predicate')[:50]
    )
    relations_in = (
        Relation.objects
        .filter(object=entity, superseded_at__isnull=True)
        .select_related('subject', 'document', 'document__source')
        .order_by('predicate')[:50]
    )

    classifications = (
        Classification.objects
        .filter(entity=entity, node__status='active', node__taxonomy__status='active')
        .select_related('node__taxonomy')
        .order_by('node__taxonomy__key', '-weight')
    )

    try:
        summary = entity.summary
    except EntitySummary.DoesNotExist:
        summary = None

    raw_events = list(
        Event.objects
        .filter(entity=entity)
        .prefetch_related('participants')
        .select_related('source')
        .order_by('date', 'created_at')[:80]
    )
    # Merge events that are the same real-world story (same type + date + title prefix).
    # Collect all source URLs so the profile can show every link.
    today = timezone.now().date()
    _seen_events = {}
    events = []
    for ev in raw_events:
        key = (ev.date, ev.event_type, ev.title[:80].lower().strip())
        if key in _seen_events:
            existing = _seen_events[key]
            if ev.source and ev.source.url and ev.source.url not in existing.source_urls:
                existing.source_urls.append(ev.source.url)
            if len(ev.description) > len(existing.description):
                existing.description = ev.description
            for p in ev.participants.all():
                if p not in existing.merged_participants:
                    existing.merged_participants.append(p)
            # Keep best call data when merging
            if ev.call_url and not existing.call_url:
                existing.call_url = ev.call_url
            if ev.call_status and not existing.call_status:
                existing.call_status = ev.call_status
        else:
            ev.source_urls = [ev.source.url] if ev.source and ev.source.url else []
            ev.merged_participants = list(ev.participants.all())
            _seen_events[key] = ev
            events.append(ev)

    # Derive display_call_status for every event (used in template)
    for ev in events:
        if ev.event_type == 'grant_call':
            if ev.call_status:
                ev.display_call_status = ev.call_status
            elif ev.date and ev.date >= today:
                ev.display_call_status = 'open'
            else:
                ev.display_call_status = 'closed'
        else:
            ev.display_call_status = None

    # For funding_program entities: all calls except explicitly closed for the top card
    active_calls = (
        [ev for ev in events
         if ev.event_type == 'grant_call' and ev.display_call_status != 'closed']
        if entity.entity_type == 'funding_program' else []
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

    # Provenance: which source documents brought this entity in
    source_docs = list(
        Assertion.objects
        .filter(entity=entity, document__isnull=False)
        .values('document__id', 'document__title', 'document__url', 'document__source__name')
        .distinct()[:8]
    )

    # Events where this entity appears as a participant (not primary entity)
    participant_events = list(
        Event.objects
        .filter(participants=entity)
        .select_related('entity', 'source')
        .order_by('-date', '-created_at')[:20]
    )

    hq_city    = next((f for f in facts if f.attribute_id == 'headquarters_city'),    None)
    hq_country = next((f for f in facts if f.attribute_id == 'headquarters_country'), None)
    hq_address = next((f for f in facts if f.attribute_id == 'headquarters_address'), None)
    # Fall back to candidate assertions for location fields (they rarely get adjudicated to accepted)
    for attr_key in ('headquarters_city', 'headquarters_country', 'headquarters_address'):
        already = {'headquarters_city': hq_city, 'headquarters_country': hq_country, 'headquarters_address': hq_address}[attr_key]
        if already is None:
            fb = (all_assertions
                  .filter(attribute_id=attr_key, status='candidate', superseded_at__isnull=True)
                  .order_by('-confidence').first())
            if fb:
                if attr_key == 'headquarters_city':    hq_city    = fb
                elif attr_key == 'headquarters_country': hq_country = fb
                elif attr_key == 'headquarters_address': hq_address = fb
    offices    = [r for r in relations_out if r.predicate_id == 'has_office_in']

    return render(request, 'curation/entity_profile.html', {
        'entity':               entity,
        'summary':              summary,
        'events':               events,
        'active_calls':         active_calls,
        'fragments_by_category': fragments_by_category,
        'facts':                facts,
        'assertions':           all_assertions,
        'relations_out':        relations_out,
        'relations_in':         relations_in,
        'classifications':      classifications,
        'source_docs':          source_docs,
        'participant_events':   participant_events,
        'hq_city':              hq_city,
        'hq_country':           hq_country,
        'hq_address':           hq_address,
        'offices':              offices,
        'title':                entity.canonical_name,
    })


@staff_member_required
def connections(request):
    """Network connections page — typed relations and event-based links."""
    from django.db.models import Q

    q = request.GET.get('q', '').strip()
    entity_type_filter = request.GET.get('entity_type', '').strip()

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

    entity_types = Entity.objects.values_list('entity_type', flat=True).distinct().order_by('entity_type')

    return render(request, 'curation/connections.html', {
        'event_connections': events_qs,
        'entity_types': entity_types,
        'q': q,
        'entity_type_filter': entity_type_filter,
        'event_connection_count': Event.objects.filter(participants__isnull=False).distinct().count(),
        'title': 'Connections',
    })


@staff_member_required
def research(request):
    """Research progress page — recent extraction runs and pipeline stats."""
    from core.models import ExtractionRun
    from django.db.models import Count, Q

    q = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()

    runs_qs = ExtractionRun.objects.order_by('-started_at')
    if q:
        runs_qs = runs_qs.filter(stats__topic__icontains=q)
    if status_filter:
        runs_qs = runs_qs.filter(status=status_filter)

    runs = runs_qs[:150]

    # Summary totals from all runs
    totals = ExtractionRun.objects.aggregate(
        total=Count('id'),
        completed=Count('id', filter=Q(status='completed')),
        failed=Count('id', filter=Q(status='failed')),
        running=Count('id', filter=Q(status='running')),
    )

    return render(request, 'curation/research.html', {
        'runs': runs,
        'totals': totals,
        'q': q,
        'status_filter': status_filter,
        'title': 'Research Progress',
    })


@staff_member_required
def taxonomy(request):
    """Read-only taxonomy browser — all active facets and nodes."""
    from core.models import Taxonomy, TaxonomyNode, Classification
    from django.db.models import Count

    # Count entities classified per node
    node_counts = {
        row['node_id']: row['n']
        for row in Classification.objects.values('node_id').annotate(n=Count('entity', distinct=True))
    }

    facets = []
    for tax in Taxonomy.objects.filter(status='active').order_by('key'):
        nodes = list(
            TaxonomyNode.objects
            .filter(taxonomy=tax)
            .order_by('path')
        )
        for node in nodes:
            node.depth = node.path.count('.')
            node.entity_count = node_counts.get(node.id, 0)
        facets.append({'taxonomy': tax, 'nodes': nodes})

    proposal_count = TaxonomyNode.objects.filter(status='proposed').count()

    return render(request, 'curation/taxonomy.html', {
        'facets': facets,
        'proposal_count': proposal_count,
        'title': 'Taxonomy',
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


@staff_member_required
def insights(request):
    """Research intelligence browser — publications, grants, and research fragments."""
    from core.models import Event, KnowledgeFragment
    from django.db.models import Q

    q = request.GET.get('q', '').strip()
    kind = request.GET.get('kind', '').strip()  # 'publications' | 'grants' | 'fragments' | ''

    pub_qs = (
        Event.objects
        .filter(event_type__in=['publication', 'research_grant'])
        .select_related('entity', 'source')
        .prefetch_related('participants')
        .order_by('-date', '-created_at')
    )
    if q:
        pub_qs = pub_qs.filter(
            Q(entity__canonical_name__icontains=q) |
            Q(title__icontains=q) |
            Q(description__icontains=q)
        )
    if kind == 'publications':
        pub_qs = pub_qs.filter(event_type='publication')
    elif kind == 'grants':
        pub_qs = pub_qs.filter(event_type='research_grant')

    frag_qs = (
        KnowledgeFragment.objects
        .filter(category='research')
        .select_related('entity', 'source')
        .order_by('-date_of_information', '-created_at')
    )
    if q:
        frag_qs = frag_qs.filter(
            Q(entity__canonical_name__icontains=q) | Q(text__icontains=q)
        )
    if kind in ('publications', 'grants'):
        frag_qs = frag_qs.none()

    pub_count = Event.objects.filter(event_type='publication').count()
    grant_count = Event.objects.filter(event_type='research_grant').count()
    fragment_count = KnowledgeFragment.objects.filter(category='research').count()

    return render(request, 'curation/insights.html', {
        'pub_events': pub_qs[:200],
        'fragments': frag_qs[:100],
        'pub_count': pub_count,
        'grant_count': grant_count,
        'fragment_count': fragment_count,
        'q': q,
        'kind': kind,
        'title': 'Research Insights',
    })


_FUNDING_EVENT_TYPES = [
    'funding_round', 'grant_award', 'grant_call', 'ipo', 'spac',
    'debt_financing', 'convertible', 'crowdfunding', 'research_grant',
]

_FUNDING_TYPE_LABELS = {
    'funding_round': 'Equity Round',
    'grant_award': 'Grant Award',
    'grant_call': 'Grant Call (Open)',
    'research_grant': 'Research Grant',
    'ipo': 'IPO',
    'spac': 'SPAC',
    'debt_financing': 'Debt',
    'convertible': 'Convertible',
    'crowdfunding': 'Crowdfunding',
}

_FUNDING_TYPE_COLORS = {
    'funding_round': '#1e40af',
    'grant_award': '#166534',
    'grant_call': '#065f46',
    'research_grant': '#14532d',
    'ipo': '#7c3aed',
    'spac': '#6d28d9',
    'debt_financing': '#92400e',
    'convertible': '#854d0e',
    'crowdfunding': '#1d4ed8',
}


@staff_member_required
def funding(request):
    """Funding intelligence — grants, equity, debt, convertibles, and open calls."""
    from core.models import Entity, Event, Relation
    from django.db.models import Count, Q, Sum

    q = request.GET.get('q', '').strip()
    ftype = request.GET.get('ftype', '').strip()

    events_qs = (
        Event.objects
        .filter(event_type__in=_FUNDING_EVENT_TYPES)
        .select_related('entity', 'source')
        .prefetch_related('participants')
        .order_by('-date', '-created_at')
    )
    if q:
        events_qs = events_qs.filter(
            Q(entity__canonical_name__icontains=q) |
            Q(title__icontains=q) |
            Q(description__icontains=q)
        )
    if ftype:
        events_qs = events_qs.filter(event_type=ftype)

    # Funding programs — program entities with their administrator and open calls
    from core.models import KnowledgeFragment
    program_entities = (
        Entity.objects
        .filter(entity_type='funding_program', status__in=('active', 'stub'))
        .order_by('canonical_name')[:60]
    )
    # For each program: find administrator, open calls, award count, best fragment
    programs = []
    for prog in program_entities:
        admin_rel = (
            Relation.objects
            .filter(predicate='administers', object_id=prog.id, superseded_at__isnull=True)
            .select_related('subject')
            .first()
        )
        open_calls = (
            Event.objects
            .filter(entity=prog, event_type='grant_call')
            .exclude(call_status='closed')
            .order_by('-date')[:3]
        )
        award_count = Event.objects.filter(
            participants=prog,
            event_type__in=['grant_award', 'research_grant', 'funding_round'],
        ).count()
        fragment = (
            KnowledgeFragment.objects
            .filter(entity=prog)
            .order_by('-confidence')
            .first()
        )
        programs.append({
            'entity': prog,
            'administrator': admin_rel.subject if admin_rel else None,
            'open_calls': list(open_calls),
            'award_count': award_count,
            'fragment': fragment,
        })
    # Sort: programs with open calls first, then by award count
    programs.sort(key=lambda p: (-len(p['open_calls']), -p['award_count']))

    # Grant calls across all programs — exclude explicitly closed, show rest
    open_calls = (
        Event.objects
        .filter(event_type='grant_call')
        .exclude(call_status='closed')
        .select_related('entity', 'source')
        .order_by('-date')[:30]
    )

    # Equity investors — with recent deals for context
    top_investors = (
        Relation.objects
        .filter(predicate='invested_in', superseded_at__isnull=True)
        .values('subject_id')
        .annotate(deals=Count('id'))
        .order_by('-deals')[:10]
    )
    investor_ids = [r['subject_id'] for r in top_investors]
    investor_entities = {
        str(e.id): e
        for e in Entity.objects.filter(id__in=investor_ids)
    }
    top_investors_list = [
        {'entity': investor_entities.get(str(r['subject_id'])), 'deals': r['deals']}
        for r in top_investors
        if investor_entities.get(str(r['subject_id']))
    ]

    # Summary counts
    counts = {
        et: Event.objects.filter(event_type=et).count()
        for et in _FUNDING_EVENT_TYPES
    }

    return render(request, 'curation/funding.html', {
        'events': events_qs[:300],
        'programs': programs,
        'open_calls': open_calls,
        'top_investors': top_investors_list,
        'counts': counts,
        'type_labels': _FUNDING_TYPE_LABELS,
        'type_colors': _FUNDING_TYPE_COLORS,
        'q': q,
        'ftype': ftype,
        'title': 'Funding Intelligence',
    })


@staff_member_required
def prompts_list(request):
    """List all active prompt templates."""
    prompts = PromptTemplate.objects.filter(is_active=True).order_by('key')
    return render(request, 'curation/prompts.html', {
        'prompts': prompts,
        'title': 'Prompt Templates',
    })


@staff_member_required
def prompt_edit(request, key):
    """Edit a prompt template — creates a new version, activates it."""
    prompt = get_object_or_404(PromptTemplate, key=key, is_active=True)
    history = PromptTemplate.objects.filter(key=key).order_by('-version')[:10]

    if request.method == 'POST':
        new_text = request.POST.get('system_prompt', '').strip()
        notes = request.POST.get('notes', '').strip()
        if not new_text:
            messages.error(request, 'Prompt text cannot be empty.')
        elif new_text == prompt.system_prompt:
            messages.info(request, 'No changes detected.')
        else:
            new_version = PromptTemplate.objects.create(
                key=key,
                label=prompt.label,
                description=prompt.description,
                system_prompt=new_text,
                version=prompt.version + 1,
                is_active=False,
                notes=notes,
            )
            new_version.activate()
            messages.success(request, f'Prompt "{key}" updated to v{new_version.version}.')
            return HttpResponseRedirect(reverse('curation:prompts_list'))

    return render(request, 'curation/prompt_edit.html', {
        'prompt': prompt,
        'history': history,
        'title': f'Edit Prompt — {prompt.label}',
    })



@staff_member_required
def research_all(request):
    """Queue research_topic for every entity that has no events yet — same pipeline as manual research."""
    if request.method == 'POST':
        from ingest.tasks.research import research_topic
        _TYPE_TO_TOPIC = {
            'company': 'company', 'investor': 'company', 'entity': 'company',
            'university': 'company', 'asset': 'company',
            'funding_program': 'funding_program', 'end_user': 'end_user',
            'program': 'question', 'facility': 'question',
            'person': 'person', 'event': 'question',
        }
        no_events = (
            Entity.objects
            .exclude(status='merged')
            .exclude(entity_type='geography')
            .filter(space_relevance__gte=50)
            .exclude(id__in=Event.objects.values('entity_id'))
            .only('id', 'canonical_name', 'entity_type', 'space_relevance')
        )
        from django.core.cache import cache
        queued = 0
        skipped = 0
        for i, entity in enumerate(no_events):
            lock_key = f'research_queued:{entity.id}'
            if cache.get(lock_key):
                skipped += 1
                continue
            topic_type = _TYPE_TO_TOPIC.get(entity.entity_type, 'company')
            # Adjacent entities (score=50): narrow search to space-specific facts only
            if entity.space_relevance is not None and entity.space_relevance < 100 and topic_type == 'company':
                topic_type = 'space_angle'
            research_topic.apply_async(
                args=[entity.canonical_name, topic_type],
                kwargs={'cascade_depth': 0},
                countdown=i * 2,
            )
            cache.set(lock_key, 1, timeout=7200)  # 2h — task should complete by then
            queued += 1
        msg = f'Queued research for {queued} entities.'
        if skipped:
            msg += f' Skipped {skipped} already in queue.'
        messages.success(request, msg)
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def classify_supply_chain_all(request):
    """Queue classify_supply_chain for entities with outputs but no tier, or with .other. tiers."""
    if request.method == 'POST':
        from ingest.tasks.research import classify_supply_chain
        from django.core.cache import cache

        _SC_TYPES = ('company', 'investor', 'entity', 'university', 'facility')
        has_tier = set(
            Assertion.objects
            .filter(attribute_id='value_chain_tier', superseded_at__isnull=True)
            .values_list('entity_id', flat=True)
        )
        # Old-vocab tiers (flat or containing old level2 values) — need reclassification
        _OLD_LEVEL2 = (
            'propulsion', 'structures', 'avionics', 'software', 'launch', 'comms',
            'earth_observation', 'ground_segment', 'navigation', 'power', 'thermal',
            'manufacturing', 'instruments', 'robotics', 'life_support', 're_entry',
            'services', 'data', 'finance', 'testing', 'integration', '.other.',
        )
        from django.db.models import Q as _Q
        old_vocab_q = _Q()
        for _old in _OLD_LEVEL2:
            old_vocab_q |= _Q(value_text__contains=f'.{_old}.')
        has_other_tier = set(
            Assertion.objects
            .filter(attribute_id='value_chain_tier', superseded_at__isnull=True)
            .filter(old_vocab_q)
            .values_list('entity_id', flat=True)
        )
        has_output = set(
            Assertion.objects
            .filter(attribute_id='output', superseded_at__isnull=True)
            .values_list('entity_id', flat=True)
        )
        to_classify = (has_output - has_tier) | (has_output & has_other_tier)
        entities = list(
            Entity.objects
            .filter(id__in=to_classify, entity_type__in=_SC_TYPES)
            .exclude(status='merged')
            .values_list('id', flat=True)
        )
        queued = 0
        for i, eid in enumerate(entities):
            lock_key = f'classify_sc:{eid}'
            if cache.get(lock_key):
                continue
            classify_supply_chain.apply_async(args=[str(eid)], countdown=i)
            cache.set(lock_key, 1, timeout=3600)
            queued += 1
        messages.success(request, f'Queued tier classification for {queued} entities.')
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def fill_supply_chain(request):
    """Queue extract_supply_chain for score=100 entities missing outputs."""
    if request.method == 'POST':
        from ingest.tasks.research import extract_supply_chain
        from django.core.cache import cache

        _SC_TYPES = ('company', 'entity', 'university', 'facility')
        has_output = set(
            Assertion.objects
            .filter(attribute_id='output', superseded_at__isnull=True)
            .values_list('entity_id', flat=True)
        )
        entities = list(
            Entity.objects
            .filter(space_relevance=100, entity_type__in=_SC_TYPES)
            .exclude(status='merged')
            .exclude(id__in=has_output)
            .only('id', 'canonical_name')
        )
        queued = skipped = 0
        for i, entity in enumerate(entities):
            lock_key = f'sc_extract:{entity.id}'
            if cache.get(lock_key):
                skipped += 1
                continue
            extract_supply_chain.apply_async(args=[str(entity.id)], countdown=i * 3)
            cache.set(lock_key, 1, timeout=7200)
            queued += 1
        msg = f'Queued supply chain extraction for {queued} entities.'
        if skipped:
            msg += f' Skipped {skipped} already in queue.'
        messages.success(request, msg)
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def assess_all(request):
    """Queue WK assessment + scoring for every entity that has no description yet.

    Queues synthesise_entity_summary for entities without an EntitySummary, and
    classify_entity for entities without a space_relevance score. No score filter —
    every entity without a description gets assessed.
    """
    if request.method == 'POST':
        from ingest.tasks.summarise import synthesise_entity_summary
        from ingest.tasks.classify import classify_entity, _FACETS_FOR_TYPE

        skip_types = {'geography', 'document_node'}
        classifiable_types = set(_FACETS_FOR_TYPE.keys())

        # Entities with no description at all
        no_summary_ids = set(
            Entity.objects
            .exclude(status='merged')
            .exclude(entity_type__in=skip_types)
            .exclude(id__in=EntitySummary.objects.values('entity_id'))
            .values_list('id', flat=True)
        )

        # Entities with no score yet (classifiable types only)
        no_score_ids = set(
            Entity.objects
            .filter(space_relevance__isnull=True)
            .exclude(status='merged')
            .exclude(entity_type__in=skip_types)
            .filter(entity_type__in=classifiable_types)
            .values_list('id', flat=True)
        )

        for i, eid in enumerate(no_summary_ids):
            synthesise_entity_summary.apply_async(args=[str(eid)], countdown=i)

        # Classify entities that still have no score and weren't already queued for summarise
        # (summarise WK path also scores, so only explicitly classify those not in no_summary_ids)
        only_no_score = no_score_ids - no_summary_ids
        for i, eid in enumerate(only_no_score):
            classify_entity.apply_async(args=[str(eid)], countdown=i * 2)

        # Stale WK summaries: entities that have events or fragments but whose summary
        # was written from world knowledge (source_count=0) — re-synthesise from evidence.
        from core.models import Event, KnowledgeFragment
        stale_summary_ids = set(
            EntitySummary.objects
            .filter(source_count=0)
            .exclude(entity__status='merged')
            .exclude(entity__entity_type__in=skip_types)
            .filter(
                entity__id__in=(
                    Event.objects.values('entity_id').union(
                        KnowledgeFragment.objects.values('entity_id')
                    )
                )
            )
            .values_list('entity_id', flat=True)
        ) - no_summary_ids  # don't double-queue
        for i, eid in enumerate(stale_summary_ids):
            synthesise_entity_summary.apply_async(args=[str(eid)], countdown=i)

        messages.success(
            request,
            f'Queued assessment for {len(no_summary_ids)} entities without a description, '
            f'scoring for {len(only_no_score)} unscored entities, '
            f'and re-synthesis for {len(stale_summary_ids)} entities with stale WK summaries.'
        )
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def classify_all(request):
    """Queue classify_entity for every entity with no space_relevance score yet."""
    if request.method == 'POST':
        from ingest.tasks.classify import classify_entity, _FACETS_FOR_TYPE
        classifiable_types = set(_FACETS_FOR_TYPE.keys())
        unscored = list(
            Entity.objects
            .filter(space_relevance__isnull=True)
            .exclude(status='merged')
            .exclude(entity_type__in=('geography', 'document_node'))
            .filter(entity_type__in=classifiable_types)
            .values_list('id', flat=True)
        )
        for i, eid in enumerate(unscored):
            classify_entity.apply_async(args=[str(eid)], countdown=i * 2)
        messages.success(request, f'Queued scoring for {len(unscored)} unscored entities.')
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def summarise_all(request):
    """Queue synthesise_entity_summary for every entity that has no summary yet."""
    if request.method == 'POST':
        from ingest.tasks.summarise import synthesise_entity_summary
        skip_types = {'person', 'geography', 'document_node'}
        no_summary_ids = list(
            Entity.objects
            .exclude(status='merged')
            .exclude(entity_type__in=skip_types)
            .exclude(space_relevance__lt=20, space_relevance__isnull=False)
            .exclude(id__in=EntitySummary.objects.values('entity_id'))
            .values_list('id', flat=True)
        )
        for eid in no_summary_ids:
            synthesise_entity_summary.apply_async(args=[str(eid)], countdown=1)
        messages.success(request, f'Queued summarisation for {len(no_summary_ids)} entities without a description.')
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def fill_locations_all(request):
    """Queue a focused location lookup for every org missing headquarters_country."""
    if request.method == 'POST':
        from ingest.tasks.research import research_topic
        from django.core.cache import cache

        missing = (
            Entity.objects
            .filter(entity_type__in=('company', 'investor', 'entity', 'university'))
            .exclude(status='merged')
            .filter(space_relevance__gte=50)
            .exclude(id__in=Assertion.objects.filter(
                attribute_id='headquarters_country',
                status='accepted',
                superseded_at__isnull=True,
            ).values('entity_id'))
            .only('id', 'canonical_name')
        )

        queued = skipped = 0
        for i, entity in enumerate(missing):
            lock_key = f'location_queued:{entity.id}'
            if cache.get(lock_key):
                skipped += 1
                continue
            research_topic.apply_async(
                args=[entity.canonical_name, 'location'],
                kwargs={'cascade_depth': 0},
                countdown=i * 3,
            )
            cache.set(lock_key, 1, timeout=7200)
            queued += 1

        msg = f'Queued location lookup for {queued} organisations missing headquarters data.'
        if skipped:
            msg += f' Skipped {skipped} already in queue.'
        messages.success(request, msg)
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def fill_addresses_all(request):
    """Queue a focused location lookup for every org that has city/country but no street address."""
    if request.method == 'POST':
        from ingest.tasks.research import research_topic
        from django.core.cache import cache

        # Orgs that have some location data but are missing a street address
        has_location = Assertion.objects.filter(
            attribute_id__in=('headquarters_city', 'headquarters_country'),
            status__in=('accepted', 'candidate'),
            superseded_at__isnull=True,
        ).values('entity_id')
        has_address = Assertion.objects.filter(
            attribute_id='headquarters_address',
            superseded_at__isnull=True,
        ).values('entity_id')

        missing = (
            Entity.objects
            .filter(
                entity_type__in=('company', 'investor', 'entity', 'university', 'facility'),
                id__in=has_location,
            )
            .exclude(status='merged')
            .filter(space_relevance__gte=50)
            .exclude(id__in=has_address)
            .only('id', 'canonical_name')
        )

        queued = skipped = 0
        for i, entity in enumerate(missing):
            lock_key = f'address_queued:{entity.id}'
            if cache.get(lock_key):
                skipped += 1
                continue
            research_topic.apply_async(
                args=[entity.canonical_name, 'location'],
                kwargs={'cascade_depth': 0},
                countdown=i * 3,
            )
            cache.set(lock_key, 1, timeout=7200)
            queued += 1

        msg = f'Queued address lookup for {queued} organisations.'
        if skipped:
            msg += f' Skipped {skipped} already in queue.'
        messages.success(request, msg)
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def geocode_offices(request):
    """Queue a Celery task to geocode all has_office_in relations with an address but no coords."""
    if request.method == 'POST':
        from ingest.tasks.research import geocode_all_offices
        geocode_all_offices.delay()
        messages.success(request, 'Geocoding offices queued — check map in a few minutes.')
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def merge_geography_duplicates(request):
    """Merge geography entities whose coordinates are within 0.005° (~500m) of each other."""
    if request.method == 'POST':
        from ingest.tasks.resolve import _add_alias, _normalize_mention
        from django.db import transaction

        THRESHOLD = 0.005

        rows = list(
            Entity.objects
            .filter(entity_type='geography', latitude__isnull=False, status='active')
            .values_list('id', 'canonical_name', 'latitude', 'longitude')
            .order_by('created_at')
        )

        merged_ids = set()
        pairs = []
        for i, (id1, name1, lat1, lon1) in enumerate(rows):
            if id1 in merged_ids:
                continue
            for id2, name2, lat2, lon2 in rows[i + 1:]:
                if id2 in merged_ids:
                    continue
                if abs(float(lat1) - float(lat2)) < THRESHOLD and abs(float(lon1) - float(lon2)) < THRESHOLD:
                    pairs.append((id1, name1, id2, name2))
                    merged_ids.add(id2)

        count = 0
        with transaction.atomic():
            for keep_id, keep_name, dup_id, dup_name in pairs:
                dup = Entity.objects.get(id=dup_id)
                Relation.objects.filter(object_id=dup_id).update(object_id=keep_id)
                for alias in EntityAlias.objects.filter(entity_id=dup_id):
                    if not EntityAlias.objects.filter(entity_id=keep_id, normalized=alias.normalized).exists():
                        alias.entity_id = keep_id
                        alias.save(update_fields=['entity'])
                    else:
                        alias.delete()
                norm = _normalize_mention(dup_name)
                if not EntityAlias.objects.filter(entity_id=keep_id, normalized=norm).exists():
                    _add_alias(str(keep_id), dup_name, norm, None)
                dup.status = 'merged'
                dup.save(update_fields=['status'])
                count += 1

        if count:
            messages.success(request, f'Merged {count} duplicate geography entity pair(s).')
        else:
            messages.info(request, 'No duplicate geography entities found.')
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def run_dedup_sweep(request):
    """Trigger a retrospective entity deduplication sweep."""
    if request.method == 'POST':
        from ingest.tasks.dedup import dedup_sweep
        dry_run = request.POST.get('dry_run') == '1'
        dedup_sweep.delay(dry_run=dry_run)
        mode = 'dry run' if dry_run else 'live'
        messages.success(request, f'Deduplication sweep queued ({mode}). Check worker logs for results.')
    return HttpResponseRedirect(reverse('curation:dashboard'))


@staff_member_required
def map_view(request):
    return render(request, 'curation/map.html', {'title': 'Map'})


@staff_member_required
def map_data(request):
    """JSON endpoint: all orgs with headquarters_country, plus has_office_in relations."""
    import json as _json

    # Entities with headquarters data
    hq_rows = (
        Assertion.objects
        .filter(attribute_id='headquarters_country', status__in=('accepted', 'candidate'), superseded_at__isnull=True)
        .exclude(value_text='')
        .select_related('entity')
        .values('entity_id', 'entity__canonical_name', 'entity__entity_type', 'value_text')
    )
    city_rows = {
        row['entity_id']: row['value_text']
        for row in Assertion.objects.filter(
            attribute_id='headquarters_city', status__in=('accepted', 'candidate'), superseded_at__isnull=True
        ).exclude(value_text='').values('entity_id', 'value_text')
    }

    entity_ids = list({row['entity_id'] for row in hq_rows})

    # Tier 1: entity.latitude set directly (geocoded street address)
    entity_coords = {
        str(e['id']): (float(e['latitude']), float(e['longitude']), bool(e.get('has_street_address')))
        for e in Entity.objects.filter(id__in=entity_ids, latitude__isnull=False)
        .values('id', 'latitude', 'longitude', 'has_street_address')
    }

    # Tier 2: coords from has_office_in relations (geocoded qualifier or geography entity)
    missing_ids = [eid for eid in entity_ids if str(eid) not in entity_coords]
    if missing_ids:
        for row in (
            Relation.objects
            .filter(subject_id__in=missing_ids, predicate_id='has_office_in', superseded_at__isnull=True)
            .values('subject_id', 'qualifiers', 'object__latitude', 'object__longitude')
        ):
            sid = str(row['subject_id'])
            if sid in entity_coords:
                continue
            q = row['qualifiers'] or {}
            lat = q.get('lat') or (float(row['object__latitude']) if row['object__latitude'] is not None else None)
            lon = q.get('lon') or (float(row['object__longitude']) if row['object__longitude'] is not None else None)
            if lat is not None:
                entity_coords[sid] = (lat, lon, bool(q.get('address') and q.get('lat')))

    # Tier 3: geography entity matching headquarters_city (city centroid, instant, no geocoding)
    missing_ids = [eid for eid in entity_ids if str(eid) not in entity_coords]
    if missing_ids:
        city_names_needed = {city_rows[eid] for eid in missing_ids if eid in city_rows}
        if city_names_needed:
            geo_city = {
                e['canonical_name']: (float(e['latitude']), float(e['longitude']))
                for e in Entity.objects.filter(
                    entity_type='geography', canonical_name__in=city_names_needed, latitude__isnull=False,
                ).values('canonical_name', 'latitude', 'longitude')
            }
            for eid in missing_ids:
                city = city_rows.get(eid)
                if city and city in geo_city:
                    entity_coords[str(eid)] = (*geo_city[city], False)

    markers = []
    seen = set()
    for row in hq_rows:
        eid = str(row['entity_id'])
        if eid in seen:
            continue
        seen.add(eid)
        city    = city_rows.get(row['entity_id'], '')
        country = row['value_text']
        coords  = entity_coords.get(eid)
        markers.append({
            'id':          eid,
            'name':        row['entity__canonical_name'],
            'type':        row['entity__entity_type'],
            'place':       city or country,
            'lat':         coords[0] if coords else None,
            'lon':         coords[1] if coords else None,
            'has_address': coords[2] if coords else False,
            'country':     country,
            'city':        city,
        })

    # Offices via has_office_in relation
    office_rows = (
        Relation.objects
        .filter(predicate_id='has_office_in', superseded_at__isnull=True)
        .select_related('subject', 'object')
        .values('subject_id', 'subject__canonical_name', 'subject__entity_type',
                'object__canonical_name', 'object__latitude', 'object__longitude', 'qualifiers')
    )
    for row in office_rows:
        q = row['qualifiers'] or {}
        lat = q.get('lat') or (float(row['object__latitude']) if row['object__latitude'] is not None else None)
        lon = q.get('lon') or (float(row['object__longitude']) if row['object__longitude'] is not None else None)
        markers.append({
            'id':          str(row['subject_id']),
            'name':        row['subject__canonical_name'],
            'type':        row['subject__entity_type'],
            'place':       row['object__canonical_name'],
            'lat':         lat,
            'lon':         lon,
            'has_address': bool(q.get('address') and q.get('lat')),
            'office_type': q.get('office_type', 'office'),
        })

    return JsonResponse({'markers': markers})


_SUPPLY_PREDICATES = ['supplies', 'contracted_by', 'customer_of', 'manufactures', 'launches_for', 'launched_payload']

_LEVELS = ['upstream', 'midstream', 'downstream', 'institutional', 'other']
_LEVEL_LABELS = {
    'upstream': 'Upstream', 'midstream': 'Midstream', 'downstream': 'Downstream',
    'institutional': 'Institutional', 'other': 'Other',
}
_ALL_LANES = ['upstream', 'midstream', 'downstream', 'institutional', 'other', 'pending']
_LANE_LABELS = {
    'upstream': 'Upstream', 'midstream': 'Midstream', 'downstream': 'Downstream',
    'institutional': 'Institutional', 'other': 'Other', 'pending': 'Pending',
}

# Map old flat tiers to levels for backward compat
_TIER_TO_LEVEL = {
    'raw_material': 'upstream', 'component': 'upstream', 'subsystem': 'upstream',
    'system': 'midstream', 'integrator': 'midstream',
    'operator': 'downstream', 'data_service': 'downstream', 'end_user': 'downstream',
}


@staff_member_required
def supply_chain(request):
    """Supply chain view — entities grouped by upstream/midstream/downstream."""
    from django.db.models import Count, Q

    q       = request.GET.get('q', '').strip()
    level_f = request.GET.get('level', '').strip()
    cat_f   = request.GET.get('cat', '').strip()

    def _multi_map(key):
        result = {}
        for row in Assertion.objects.filter(
            attribute_id=key,
            status__in=('accepted', 'candidate'),
            superseded_at__isnull=True,
        ).exclude(value_text='').values('entity_id', 'value_text'):
            eid = row['entity_id']
            if eid not in result:
                result[eid] = []
            result[eid].append(row['value_text'])
        return result

    def _single_map(key):
        return {
            row['entity_id']: row['value_text']
            for row in Assertion.objects.filter(
                attribute_id=key,
                status__in=('accepted', 'candidate'),
                superseded_at__isnull=True,
            ).exclude(value_text='').values('entity_id', 'value_text')
        }

    tier_map    = _multi_map('value_chain_tier')
    output_map  = _multi_map('output')
    input_map   = _multi_map('input')
    country_map = _single_map('headquarters_country')

    entity_ids = set(tier_map) | set(output_map) | set(input_map)

    def _path_level(path):
        """Return level1 from a path, supporting both new (upstream.x.y) and old (component) formats."""
        parts = path.split('.')
        if parts[0] in _LEVELS:
            return parts[0]
        return _TIER_TO_LEVEL.get(parts[0])

    _SC_TYPES = ('company', 'investor', 'entity', 'university', 'facility')
    qs = (
        Entity.objects
        .filter(id__in=entity_ids, status__in=('active', 'stub'), entity_type__in=_SC_TYPES)
    )
    if q:
        qs = qs.filter(Q(canonical_name__icontains=q) | Q(aliases__alias__icontains=q)).distinct()
    if level_f == 'pending':
        # Pending = has outputs/inputs but no tier
        qs = qs.exclude(id__in=list(tier_map.keys()))
    elif level_f:
        qs = qs.filter(id__in=[
            eid for eid, paths in tier_map.items()
            if any(_path_level(p) == level_f for p in paths)
        ])
    if cat_f:
        qs = qs.filter(id__in=[
            eid for eid, paths in tier_map.items()
            if any(cat_f.lower() in p.lower() for p in paths)
        ])

    entities = list(qs.order_by('canonical_name')[:300])
    eids = [e.id for e in entities]

    supply_out = {
        str(r['subject_id']): r['n']
        for r in Relation.objects.filter(
            subject_id__in=eids, predicate_id__in=_SUPPLY_PREDICATES, superseded_at__isnull=True,
        ).values('subject_id').annotate(n=Count('id'))
    }
    supply_in = {
        str(r['object_id']): r['n']
        for r in Relation.objects.filter(
            object_id__in=eids, predicate_id__in=_SUPPLY_PREDICATES, superseded_at__isnull=True,
        ).values('object_id').annotate(n=Count('id'))
    }

    for e in entities:
        eid = str(e.id)
        e.sc_tiers   = tier_map.get(e.id, [])
        e.sc_outputs = output_map.get(e.id, [])
        e.sc_inputs  = input_map.get(e.id, [])
        e.sc_country = country_map.get(e.id, '')
        e.supply_out = supply_out.get(eid, 0)
        e.supply_in  = supply_in.get(eid, 0)

    # Group by level — entities with no tier go into 'pending' lane
    by_level = {lane: [] for lane in _ALL_LANES}
    for e in entities:
        placed = False
        seen = set()
        for path in e.sc_tiers:
            lvl = _path_level(path)
            if lvl and lvl not in seen:
                by_level[lvl].append(e)
                seen.add(lvl)
                placed = True
        if not placed:
            by_level['pending'].append(e)

    # All unique level2 categories for filter
    all_cats = sorted({
        p.split('.')[1]
        for paths in tier_map.values()
        for p in paths
        if len(p.split('.')) >= 2 and p.split('.')[0] in _LEVELS
    })
    level_counts = {lane: len(lst) for lane, lst in by_level.items()}

    return render(request, 'curation/supply_chain.html', {
        'entities':     entities,
        'by_level':     by_level,
        'LANES':        _ALL_LANES,
        'LANE_LABELS':  _LANE_LABELS,
        'level_counts': level_counts,
        'all_cats':     all_cats,
        'level_f':      level_f,
        'cat_f':        cat_f,
        'q':            q,
        'total':        len(entities),
        'title':        'Supply Chain',
    })


@staff_member_required
def supply_chain_entity(request, entity_id):
    """JSON: upstream suppliers and downstream customers for one entity."""
    from django.db.models import Q

    entity = get_object_or_404(Entity, pk=entity_id)

    upstream = list(
        Relation.objects
        .filter(object=entity, predicate_id__in=_SUPPLY_PREDICATES, superseded_at__isnull=True)
        .select_related('subject')
        .values('subject__id', 'subject__canonical_name', 'subject__entity_type', 'predicate_id', 'qualifiers')
    )
    downstream = list(
        Relation.objects
        .filter(subject=entity, predicate_id__in=_SUPPLY_PREDICATES, superseded_at__isnull=True)
        .select_related('object')
        .values('object__id', 'object__canonical_name', 'object__entity_type', 'predicate_id', 'qualifiers')
    )

    def _assertion_map(key, entity_ids):
        return {
            row['entity_id']: row['value_text']
            for row in Assertion.objects.filter(
                attribute_id=key, entity_id__in=entity_ids,
                status__in=('accepted', 'candidate'), superseded_at__isnull=True,
            ).exclude(value_text='').values('entity_id', 'value_text')
        }

    all_ids = [r['subject__id'] for r in upstream] + [r['object__id'] for r in downstream]

    tiers_multi = {}
    outputs_multi = {}
    inputs_multi = {}
    for row in Assertion.objects.filter(
        attribute_id__in=('value_chain_tier', 'output', 'input'), entity_id__in=all_ids,
        status__in=('accepted', 'candidate'), superseded_at__isnull=True,
    ).exclude(value_text='').values('entity_id', 'value_text', 'attribute_id'):
        eid = row['entity_id']
        if row['attribute_id'] == 'value_chain_tier':
            tiers_multi.setdefault(eid, []).append(row['value_text'])
        elif row['attribute_id'] == 'output':
            outputs_multi.setdefault(eid, []).append(row['value_text'])
        else:
            inputs_multi.setdefault(eid, []).append(row['value_text'])

    def _enrich(rows, id_key, name_key, type_key):
        return [
            {
                'id':        str(r[id_key]),
                'name':      r[name_key],
                'type':      r[type_key],
                'predicate': r['predicate_id'],
                'product':   (r['qualifiers'] or {}).get('product', ''),
                'tiers':   tiers_multi.get(r[id_key], []),
                'outputs': outputs_multi.get(r[id_key], []),
                'inputs':  inputs_multi.get(r[id_key], []),
            }
            for r in rows
        ]

    return JsonResponse({
        'entity': {'id': str(entity.id), 'name': entity.canonical_name},
        'upstream':   _enrich(upstream,   'subject__id', 'subject__canonical_name', 'subject__entity_type'),
        'downstream': _enrich(downstream, 'object__id',  'object__canonical_name',  'object__entity_type'),
    })


@staff_member_required
def entity_search(request):
    """JSON autocomplete endpoint — returns entities matching the query string."""
    import json as _json
    from django.http import JsonResponse
    from django.db.models import Q
    q = request.GET.get('q', '').strip()
    if len(q) < 2:
        return JsonResponse({'results': []})
    qs = (
        Entity.objects
        .filter(
            Q(canonical_name__icontains=q) | Q(aliases__alias__icontains=q)
        )
        .exclude(status='merged')
        .exclude(entity_type='geography')
        .distinct()
        .only('id', 'canonical_name', 'entity_type', 'status')
        [:12]
    )
    results = [
        {
            'id': str(e.id),
            'name': e.canonical_name,
            'type': e.entity_type,
            'status': e.status,
            'url': reverse('curation:entity_profile', args=[e.id]),
        }
        for e in qs
    ]
    return JsonResponse({'results': results})
