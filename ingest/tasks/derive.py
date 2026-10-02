"""Derived facts (§7g) — larger facts computed from smaller sourced facts.

The blueprint's rule: a derived fact must carry
  method='derived'  and  derived_from=[ids of the facts it was computed from]
so the dependency chain is explicit and auditable — and so a corrected input
invalidates the output. Derived facts are EXCLUDED from corroboration (§9.2):
a computation is never evidence.

First derivation: total_funding_usd = Σ individually-sourced funding events.
An extracted total ("SpaceX has raised $X", single-source, often stale or
wrong) is replaced by the sum of the graph's own deduplicated funding events,
each of which has a date, amount and source. The derived value inherits the
weakest contributor's confidence — a chain is never stronger than its weakest
link.

Pure DB computation. No LLM. Runs on the project queue.
"""
import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone
from psycopg2.extras import DateTimeTZRange

from core.models import Assertion, Entity, Event

logger = logging.getLogger(__name__)

# Equity-like capital events that sum into a company's total raised.
# Grants are excluded — non-dilutive capital is tracked separately.
_EQUITY_EVENT_TYPES = [
    'funding_round', 'ipo', 'spac', 'debt_financing', 'convertible', 'crowdfunding',
]

# Only capital-RAISING entity types get a derived total. "Total raised" on an
# asset (a rocket with company events wrongly attached) or on an investor whose
# portfolio participations leaked into their own events is exactly the kind of
# arithmetic nonsense derivation exists to surface — type-guard it off.
_RAISING_TYPES = {'company', 'university', 'end_user'}

_TOTAL_ATTR = 'total_funding_usd'


def rebuild_funding_totals(entity_ids=None) -> dict:
    """Recompute total_funding_usd as a derived assertion. Returns stats.

    Only entities that actually have equity-like funding events are processed —
    the working set comes from the events table, never a full entity scan.
    """
    from django.db.models import Q

    event_q = Q(event_type__in=_EQUITY_EVENT_TYPES, amount_usd__isnull=False)
    if entity_ids:
        event_q &= Q(entity_id__in=entity_ids)

    events = list(
        Event.objects.filter(event_q)
        .exclude(entity__status='merged')
        .exclude(entity__entity_type__in=('geography', 'document_node', 'person', 'event'))
        .select_related('entity')
        .order_by('entity_id', 'date', 'id')
    )
    stats = {'computed': 0, 'no_events': 0, 'total_usd': 0.0}

    # Group events by entity, deduplicating identical rounds as we go
    by_entity: dict = {}
    for ev in events:
        if ev.entity.entity_type not in _RAISING_TYPES:
            continue
        key = (round(float(ev.amount_usd), 2), ev.date.year if ev.date else None)
        rounds = by_entity.setdefault(ev.entity, [])
        if key not in {(round(float(r.amount_usd), 2), r.date.year if r.date else None)
                       for r in rounds}:
            rounds.append(ev)

    now = timezone.now()
    # Derived totals are projections, not evidence: stale ones on entities that
    # no longer qualify (wrong type, events removed) must not linger.
    Assertion.objects.filter(
        attribute_id=_TOTAL_ATTR, method='derived', superseded_at__isnull=True,
    ).update(superseded_at=now, status='superseded')

    for entity, rounds in by_entity.items():
        # Near-duplicate rounds: same year, amounts within 15% — almost always
        # two reports of the same round ("$75B IPO" vs "$74.4B IPO"), often
        # with inconsistent event typing (ipo vs funding_round). Keep the
        # higher-confidence report.
        deduped = []
        for ev in rounds:
            dup_idx = None
            for i, kept in enumerate(deduped):
                same_period = (kept.date and ev.date and kept.date.year == ev.date.year)
                if same_period:
                    ka, ea = abs(float(kept.amount_usd)), abs(float(ev.amount_usd))
                    if ka and ea and abs(ka - ea) <= 0.15 * max(ka, ea):
                        dup_idx = i
                        break
            if dup_idx is None:
                deduped.append(ev)
            elif ev.confidence > deduped[dup_idx].confidence:
                deduped[dup_idx] = ev
        rounds = deduped

        total = round(sum(float(r.amount_usd) for r in rounds), 2)
        # A chain is never stronger than its weakest link
        confidence = min(r.confidence for r in rounds)

        with transaction.atomic():
            # Supersede ALL prior totals — extracted guesses included. The
            # derived value is authoritative: it is traceable to every round.
            Assertion.objects.filter(
                entity=entity, attribute_id=_TOTAL_ATTR, superseded_at__isnull=True,
            ).update(superseded_at=now, status='superseded')
            Assertion.objects.create(
                entity=entity,
                attribute_id=_TOTAL_ATTR,
                value_num=total,
                unit='USD',
                method='derived',
                derived_from=[r.id for r in rounds],
                confidence=confidence,
                status='accepted',
                quote=f'[Derived] Sum of {len(rounds)} sourced funding events',
                valid_range=DateTimeTZRange(now, None),
            )
        stats['computed'] += 1
        stats['total_usd'] += total

    stats['no_events'] = max(0, len(entity_ids) - stats['computed']) if entity_ids else 0
    logger.info(
        'derive_funding_totals: %d computed (Σ $%.0fM)',
        stats['computed'], stats['total_usd'] / 1e6,
    )
    return stats


@shared_task(queue='project', max_retries=1)
def derive_funding_totals(entity_ids=None):
    return rebuild_funding_totals(entity_ids=entity_ids)
