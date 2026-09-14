"""Curation review interface — §14b of the blueprint.

Staff-only. A curator sees incoming candidates and can accept, correct, or reject.
Goal: a human can correct the graph and the correction sticks.
"""
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone

from core.models import Assertion, Conflict, Entity


@staff_member_required
def dashboard(request):
    ctx = {
        'stub_count': Entity.objects.filter(status='stub').count(),
        'conflict_count': Conflict.objects.filter(resolution='pending').count(),
        'candidate_count': Assertion.objects.filter(status='candidate').count(),
        'title': 'Curation Dashboard',
    }
    return render(request, 'curation/dashboard.html', ctx)


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
