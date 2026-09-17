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

from core.models import Assertion, Classification, Conflict, Entity, Relation, Source, ScheduledSource, Taxonomy, TaxonomyNode
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
    snapshot = get_snapshot()
    recent_entities = (
        Entity.objects
        .exclude(status='merged')
        .order_by('-created_at')[:50]
    )
    ctx = {
        'stub_count': Entity.objects.filter(status='stub').count(),
        'conflict_count': Conflict.objects.filter(resolution='pending').count(),
        'candidate_count': Assertion.objects.filter(status='candidate').count(),
        'analytics': snapshot,
        'recent_entities': recent_entities,
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
def conflicts(request):
    conflict_list = (
        Conflict.objects
        .filter(resolution='pending')
        .select_related('entity')
        .order_by('-detected_at')[:100]
    )
    return render(request, 'curation/conflicts.html', {
        'conflicts': conflict_list,
        'title': 'Open Conflicts',
    })


@staff_member_required
def resolve_conflict(request, conflict_id):
    """Pick a winner, mark range, or declare both wrong."""
    if request.method != 'POST':
        return HttpResponseRedirect(reverse('curation:conflicts'))

    conflict = get_object_or_404(Conflict, pk=conflict_id)
    action = request.POST.get('action')  # 'pick_first' | 'pick_second' | 'both_wrong' | 'range'
    notes = request.POST.get('notes', '')

    resolution_map = {
        'pick_first': 'picked',
        'pick_second': 'picked',
        'both_wrong': 'both_wrong',
        'range': 'range',
    }
    resolution = resolution_map.get(action, 'pending')

    with transaction.atomic():
        ids = conflict.assertion_ids or []
        if action == 'pick_first' and len(ids) >= 1:
            Assertion.objects.filter(pk=ids[0]).update(status='accepted')
            if len(ids) >= 2:
                Assertion.objects.filter(pk=ids[1]).update(status='rejected')
        elif action == 'pick_second' and len(ids) >= 2:
            Assertion.objects.filter(pk=ids[1]).update(status='accepted')
            Assertion.objects.filter(pk=ids[0]).update(status='rejected')
        elif action == 'both_wrong':
            Assertion.objects.filter(pk__in=ids).update(status='rejected')

        conflict.resolution = resolution
        conflict.resolved_by = request.user.username
        conflict.resolved_at = timezone.now()
        conflict.notes = notes
        conflict.save(update_fields=['resolution', 'resolved_by', 'resolved_at', 'notes'])

    return HttpResponseRedirect(reverse('curation:conflicts'))


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
        f._source_count = source_counts.get(f.attribute_id, 1)

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

    return render(request, 'curation/entity_profile.html', {
        'entity': entity,
        'facts': facts,
        'assertions': all_assertions,
        'relations_out': relations_out,
        'relations_in': relations_in,
        'classifications': classifications,
        'title': entity.canonical_name,
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
