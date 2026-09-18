"""Stage 5 — Adjudicate.

Resolution rules for newly created assertions:
  1. Identical value, same source   → reject duplicate (no new evidence)
  2. Identical value, diff source   → corroboration bump, reject new
  3. Different value, clearly newer → supersede old, accept new
  4. Different value                → LLM synthesis (confidence reflects disagreement)
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone
from psycopg2.extras import DateTimeTZRange

from core.models import Assertion

logger = logging.getLogger(__name__)


@shared_task(bind=True, queue='adjudicate', max_retries=2)
def adjudicate_assertions(self, assertion_ids: list[int]):
    assertions = (
        Assertion.objects
        .filter(id__in=assertion_ids)
        .select_related('attribute', 'document__source')
    )
    for new_a in assertions:
        try:
            _adjudicate_one(new_a)
        except Exception as exc:
            logger.error('adjudicate_one failed for assertion %s: %s', new_a.pk, exc)


def _adjudicate_one(new_a: Assertion) -> None:
    existing_qs = (
        Assertion.objects
        .filter(
            entity_id=new_a.entity_id,
            attribute_id=new_a.attribute_id,
            status='accepted',
            superseded_at__isnull=True,
        )
        .exclude(pk=new_a.pk)
        .select_related('attribute')
    )

    if not existing_qs.exists():
        # First assertion for this (entity, attribute) — accept if confident enough
        if new_a.status == 'candidate' and new_a.confidence >= 50:
            Assertion.objects.filter(pk=new_a.pk).update(status='accepted')
        return

    best = existing_qs.order_by('-confidence').first()

    # ── Rules 1 & 2: same value ───────────────────────────────────────────
    if _values_equal(new_a, best):
        same_source = (
            new_a.document_id is not None
            and new_a.document_id == best.document_id
        )
        if same_source:
            Assertion.objects.filter(pk=new_a.pk).update(status='rejected')
            return
        # Independent corroboration — asymptotic bump toward 100
        gap = 100 - best.confidence
        bump = min(99, best.confidence + max(3, int(gap * new_a.confidence / 300)))
        with transaction.atomic():
            Assertion.objects.filter(pk=best.pk).update(confidence=bump)
            Assertion.objects.filter(pk=new_a.pk).update(status='rejected')
        logger.info(
            'Adjudicate: corroboration entity=%s attr=%s conf %d→%d',
            new_a.entity_id, new_a.attribute_id, best.confidence, bump,
        )
        return

    # ── Rule 3: volatile + clearly newer → supersede without LLM ─────────
    if new_a.attribute.volatility_days is not None and _is_clearly_newer(new_a, best):
        _supersede(old=best, new=new_a)
        logger.info('Adjudicate: superseded entity=%s attr=%s', new_a.entity_id, new_a.attribute_id)
        return

    # ── Rule 4: conflicting values → LLM synthesis ───────────────────────
    from ingest.tasks.synthesize import synthesize_conflict
    synthesize_conflict.delay(
        str(new_a.entity_id),
        new_a.attribute_id,
        [best.pk, new_a.pk],
    )
    logger.info(
        'Adjudicate: queued synthesis entity=%s attr=%s',
        new_a.entity_id, new_a.attribute_id,
    )


# ── Helpers ───────────────────────────────────────────────────────────────

def _values_equal(a: Assertion, b: Assertion) -> bool:
    for va, vb in [
        (a.value_text, b.value_text),
        (a.value_num, b.value_num),
        (a.value_bool, b.value_bool),
        (a.value_date, b.value_date),
    ]:
        if va is not None and vb is not None:
            return va == vb
    return False


def _is_clearly_newer(new_a: Assertion, existing: Assertion) -> bool:
    new_pub = getattr(getattr(new_a, 'document', None), 'published_at', None)
    old_pub = getattr(getattr(existing, 'document', None), 'published_at', None)
    if new_pub and old_pub:
        return (new_pub - old_pub).days > 30
    return (new_a.observed_at - existing.observed_at).days > 30


def _supersede(old: Assertion, new: Assertion) -> None:
    now = timezone.now()
    old_lower = old.valid_range.lower if old.valid_range else now
    new_lower = new.valid_range.lower if new.valid_range else now
    with transaction.atomic():
        Assertion.objects.filter(pk=old.pk).update(
            valid_range=DateTimeTZRange(old_lower, new_lower),
            superseded_at=now,
            status='superseded',
        )
        Assertion.objects.filter(pk=new.pk).update(status='accepted')
