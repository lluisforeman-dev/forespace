"""Stage 5 — Adjudicate.

Resolution rules for newly created assertions:
  0. Multi-cardinality attribute → every value is an independent fact, accept it
  1. Identical value, same source   → reject duplicate (no new evidence)
  2. Identical value, diff source   → corroboration bump, reject new
  3. Different value, clearly newer → supersede old, accept new
  4. Different value, wild jump (>5× / <0.2×) → flag as candidate for review (§8b rule 5)
  5. Different value                → LLM synthesis (confidence reflects disagreement)
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone
from psycopg2.extras import DateTimeTZRange

from core.models import Assertion, Entity
from ingest.confidence import documents_independent

logger = logging.getLogger(__name__)

_OUT_OF_RANGE_RATIO = 5.0


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
    # Derived facts are owned by the derive tasks — never adjudicated,
    # and never usable as evidence for anything else (§9.2).
    if new_a.method == 'derived':
        return

    # ── Rule 0: multi-cardinality attributes hold independent values ─────
    # e.g. funding_round_amount_usd — Series A $5M and Series B $40M are BOTH
    # correct; treating the second as a conflict would flatten the history
    # into one synthesised (wrong) value.
    if new_a.attribute.cardinality == 'multi':
        if new_a.status == 'candidate' and new_a.confidence >= 50:
            Assertion.objects.filter(pk=new_a.pk).update(status='accepted')
        return

    existing_qs = (
        Assertion.objects
        .filter(
            entity_id=new_a.entity_id,
            attribute_id=new_a.attribute_id,
            status='accepted',
            superseded_at__isnull=True,
        )
        # A derived fact is a computation, not evidence (§9.2) — it can never
        # corroborate an extracted claim or win an adjudication.
        .exclude(method='derived')
        .exclude(pk=new_a.pk)
        .select_related('attribute', 'document')
    )

    if not existing_qs.exists():
        # First assertion for this (entity, attribute) — accept if confident enough
        if new_a.status == 'candidate' and new_a.confidence >= 50:
            Assertion.objects.filter(pk=new_a.pk).update(status='accepted')
        return

    best = existing_qs.order_by('-confidence').first()

    # ── Rules 1 & 2: same value ───────────────────────────────────────────
    if _values_equal(new_a, best):
        same_document = (
            new_a.document_id is not None
            and new_a.document_id == best.document_id
        )
        if same_document:
            Assertion.objects.filter(pk=new_a.pk).update(status='rejected')
            return

        independent = documents_independent(new_a.document, best.document)
        gap = 100 - best.confidence
        with transaction.atomic():
            if independent:
                # True corroboration — bump confidence AND record the count
                new_count = (best.corroboration_count or 0) + 1
                docs = list(best.corroborated_by or [])
                if new_a.document_id and new_a.document_id not in docs:
                    docs.append(new_a.document_id)
                bump = min(99, best.confidence + max(5, int(gap * new_a.confidence / 300)))
                Assertion.objects.filter(pk=best.pk).update(
                    confidence=bump,
                    corroboration_count=new_count,
                    corroborated_by=docs,
                )
                Assertion.objects.filter(pk=new_a.pk).update(status='rejected')
                logger.info(
                    'Adjudicate: independent corroboration entity=%s attr=%s '
                    'conf %d→%d (sources=%d)',
                    new_a.entity_id, new_a.attribute_id, best.confidence, bump, new_count,
                )
            else:
                # Same story reprinted by the same outlet — recorded, but zero
                # trust gain: echoing your own claim is not confirmation.
                Assertion.objects.filter(pk=new_a.pk).update(status='rejected')
                logger.info(
                    'Adjudicate: non-independent echo entity=%s attr=%s (same domain/hash) — no bump',
                    new_a.entity_id, new_a.attribute_id,
                )
        return

    # ── Rule 3: volatile + clearly newer → supersede without LLM ─────────
    if new_a.attribute.volatility_days is not None and _is_clearly_newer(new_a, best):
        _supersede(old=best, new=new_a)
        logger.info('Adjudicate: superseded entity=%s attr=%s', new_a.entity_id, new_a.attribute_id)
        return

    # ── Rule 4 (§8b rule 5): wild value jump → flag, never auto-resolve ──
    # Order-of-magnitude jumps are usually unit errors or entity-resolution
    # failures. Park the new value as a candidate for human review instead of
    # letting synthesis silently pick one.
    if _is_out_of_range(new_a, best):
        from core.models import Conflict
        now = timezone.now()
        with transaction.atomic():
            Assertion.objects.filter(pk=new_a.pk).update(
                status='candidate', review_state='pending',
            )
            already_open = Conflict.objects.filter(
                entity_id=new_a.entity_id,
                attribute_key=new_a.attribute_id,
                resolution__isnull=True,
            ).exists()
            if not already_open:
                Conflict.objects.create(
                    entity_id=new_a.entity_id,
                    attribute_key=new_a.attribute_id,
                    assertion_ids=[best.pk, new_a.pk],
                    severity='high' if new_a.attribute.volatility_days is None else 'medium',
                )
        logger.info(
            'Adjudicate: out-of-range entity=%s attr=%s (%s vs %s) — flagged for review',
            new_a.entity_id, new_a.attribute_id, best.value_num, new_a.value_num,
        )
        return

    # ── Rule 5: conflicting values → LLM synthesis ───────────────────────
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


def _is_out_of_range(new_a: Assertion, existing: Assertion) -> bool:
    """True when a numeric value jumps by more than _OUT_OF_RANGE_RATIO (× or ÷).

    A 6× jump flags; exactly 5× does not. A zero existing value cannot be judged
    (0 → anything is normal for counters and totals) and never flags.
    """
    if new_a.value_num is None or existing.value_num is None:
        return False
    old = float(existing.value_num)
    new = float(new_a.value_num)
    if old == 0:
        return False  # zero denominator → cannot judge, no flag
    ratio = new / old
    return ratio > _OUT_OF_RANGE_RATIO or ratio < (1.0 / _OUT_OF_RANGE_RATIO)


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
