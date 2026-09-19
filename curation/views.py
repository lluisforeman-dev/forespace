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

    raw_events = list(
        Event.objects
        .filter(entity=entity)
        .prefetch_related('participants')
        .select_related('source')
        .order_by('date', 'created_at')[:80]
    )
    # Merge events that are the same real-world story (same type + date + title prefix).
    # Collect all source URLs so the profile can show every link.
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
        else:
            ev.source_urls = [ev.source.url] if ev.source and ev.source.url else []
            ev.merged_participants = list(ev.participants.all())
            _seen_events[key] = ev
            events.append(ev)

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
            .order_by('date')[:3]
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

    # Open grant calls across all programs
    open_calls = (
        Event.objects
        .filter(event_type='grant_call')
        .select_related('entity', 'source')
        .order_by('date')[:30]
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
