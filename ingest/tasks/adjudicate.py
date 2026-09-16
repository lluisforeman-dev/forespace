"""Stage 5 — Adjudicate.

Applies §8b resolution rules to newly created assertions:
  1. Identical value      → corroboration bump, reject duplicate
  2. Newer + volatile     → supersede old, accept new
  3. Similar recency      → conflict (medium)
  4. Immutable attribute  → always conflict (high)
  5. Out-of-range (>5×)  → conflict (high)

No LLM calls. Pure computation against the DB.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from celery import shared_task
from django.db import transaction
from django.utils import timezone
from psycopg2.extras import DateTimeTZRange

from core.models import Assertion, AttributeDef, Conflict

logger = logging.getLogger(__name__)


@shared_task(bind=True, queue='adjudicate', max_retries=2)
def adjudicate_assertions(self, assertion_ids: list[int]):
    """Run resolution rules on a batch of newly inserted assertions."""
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
    attr: AttributeDef = new_a.attribute
    is_immutable = attr.volatility_days is None

    # ── Rule 1: identical value from a different source ──────────────────
    if _values_equal(new_a, best):
        same_source = (
            new_a.document_id is not None
            and new_a.document_id == best.document_id
        )
        if same_source:
            # Same document repeated — no new evidence, just reject silently
            Assertion.objects.filter(pk=new_a.pk).update(status='rejected')
            return
        # Independent source corroborates — bump proportionally to its confidence
        bump = min(100, best.confidence + max(3, new_a.confidence // 10))
        with transaction.atomic():
            Assertion.objects.filter(pk=best.pk).update(confidence=bump)
            Assertion.objects.filter(pk=new_a.pk).update(status='rejected')
        logger.info(
            'Adjudicate: corroboration entity=%s attr=%s (src_conf=%d) — bump to %d',
            new_a.entity_id, new_a.attribute_id, new_a.confidence, bump,
        )
        return

    # ── Rule 4: immutable attribute with different value ──────────────────
    if is_immutable:
        _open_conflict(new_a, best, severity='high')
        logger.warning(
            'Adjudicate: immutable conflict entity=%s attr=%s',
            new_a.entity_id, new_a.attribute_id,
        )
        return

    # ── Rule 5: numeric value changed by >5× ─────────────────────────────
    if _is_out_of_range(new_a, best):
        _open_conflict(new_a, best, severity='high')
        logger.warning(
            'Adjudicate: out-of-range jump entity=%s attr=%s',
            new_a.entity_id, new_a.attribute_id,
        )
        return

    # ── Rule 2: volatile + new assertion is clearly newer ────────────────
    if _is_clearly_newer(new_a, best):
        _supersede(old=best, new=new_a)
        logger.info(
            'Adjudicate: superseded entity=%s attr=%s',
            new_a.entity_id, new_a.attribute_id,
        )
        return

    # ── Rule 3: different value, similar recency/confidence → conflict ────
    _open_conflict(new_a, best, severity='medium')
    logger.info(
        'Adjudicate: conflict entity=%s attr=%s',
        new_a.entity_id, new_a.attribute_id,
    )


# ── Helpers ───────────────────────────────────────────────────────────────

def _values_equal(a: Assertion, b: Assertion) -> bool:
    """True if both assertions carry the same value."""
    checks = [
        (a.value_text, b.value_text),
        (a.value_num, b.value_num),
        (a.value_bool, b.value_bool),
        (a.value_date, b.value_date),
    ]
    for va, vb in checks:
        if va is not None and vb is not None:
            return va == vb
    return False


def _is_out_of_range(new_a: Assertion, existing: Assertion) -> bool:
    """True if a numeric value jumped more than 5× in either direction."""
    n, e = new_a.value_num, existing.value_num
    if n is None or e is None or e == 0:
        return False
    ratio = float(n) / float(e)
    return ratio > 5 or ratio < 0.2


def _is_clearly_newer(new_a: Assertion, existing: Assertion) -> bool:
    """True if new assertion's document is measurably more recent."""
    new_pub = getattr(getattr(new_a, 'document', None), 'published_at', None)
    old_pub = getattr(getattr(existing, 'document', None), 'published_at', None)
    if new_pub and old_pub:
        return (new_pub - old_pub).days > 30
    # Fall back to observed_at
    return (new_a.observed_at - existing.observed_at).days > 30


def _supersede(old: Assertion, new: Assertion) -> None:
    """Close old assertion's validity range and accept the new one."""
    now = timezone.now()
    old_lower = old.valid_range.lower if old.valid_range else now
    new_lower = new.valid_range.lower if new.valid_range else now

    with transaction.atomic():
        Assertion.objects.filter(pk=old.pk).update(
            valid_range=DateTimeTZRange(old_lower, new_lower),
            superseded_at=now,
        )
        Assertion.objects.filter(pk=new.pk).update(status='accepted')


def _open_conflict(new_a: Assertion, existing: Assertion, severity: str) -> None:
    """Mark both assertions as conflicted and create a Conflict row."""
    with transaction.atomic():
        Assertion.objects.filter(pk__in=[new_a.pk, existing.pk]).update(status='conflicted')
        Conflict.objects.create(
            entity_id=new_a.entity_id,
            attribute_key=new_a.attribute_id,
            assertion_ids=[existing.pk, new_a.pk],
            severity=severity,
            resolution='pending',
        )
