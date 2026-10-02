"""The freshness loop — completes truth decay.

Truth decay *scores* stale facts downward at read time; this command closes the
loop by *re-verifying* them. It finds entities whose accepted volatile
assertions (attributes with a volatility_days window) are older than their
window — e.g. an employee_count from 9 months ago with a 180-day window — and
queues EigenSearch research for the stalest entities first.

Safe by default: run with --apply to actually enqueue. Respects the research
pause flag, the 7-day re-research guard, and the cascade cap is irrelevant here
(these are depth-0 roots).

Intended as a daily cron:
    python manage.py refresh_stale_facts --apply --limit 25
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Assertion, AttributeDef, Entity, ExtractionRun

_TYPE_TO_TOPIC = {
    'company': 'company', 'investor': 'company', 'entity': 'company',
    'university': 'company', 'asset': 'company',
    'funding_program': 'funding_program', 'end_user': 'end_user',
    'program': 'question', 'facility': 'question',
    'person': 'person', 'event': 'question',
}


def _effective_at(assertion):
    if assertion.valid_range is not None and assertion.valid_range.lower is not None:
        return assertion.valid_range.lower
    return assertion.observed_at


class Command(BaseCommand):
    help = 'Queue re-research for entities whose volatile facts are stale past their window.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=25,
                            help='Max entities to re-research per run.')
        parser.add_argument('--min-score', type=int, default=50,
                            help='Only entities with space_relevance >= this.')
        parser.add_argument('--stagger', type=int, default=20,
                            help='Seconds between enqueued runs.')
        parser.add_argument('--apply', action='store_true',
                            help='Actually enqueue research (default: report only).')

    def handle(self, *args, **options):
        from ingest.pause import is_paused

        now = timezone.now()
        week_ago = now - timedelta(days=7)
        min_score = options['min_score']

        if is_paused('research'):
            self.stdout.write(self.style.WARNING(
                'Research is paused — refresh_stale_facts doing nothing.'
            ))
            return

        # Entities researched in the last 7 days are skipped (same guard as the cascade)
        recently = set(
            ExtractionRun.objects
            .filter(started_at__gte=week_ago)
            .exclude(stats__topic=None)
            .values_list('stats__topic', flat=True)
        )

        volatility = dict(AttributeDef.objects.exclude(volatility_days=None)
                          .values_list('key', 'volatility_days'))

        # Max staleness per entity across its live volatile assertions
        stale: dict[str, float] = {}
        sample_values: dict[str, str] = {}
        qs = (
            Assertion.objects
            .filter(status='accepted', superseded_at__isnull=True,
                    attribute_id__in=volatility.keys())
            .exclude(entity__status='merged')
            .exclude(entity__entity_type__in=('geography', 'document_node'))
            .filter(entity__space_relevance__gte=min_score)
            .select_related('entity')
        )
        for a in qs:
            eid = str(a.entity_id)
            age_days = (now - _effective_at(a)).days
            window = volatility[a.attribute_id] or 3650
            excess = age_days - window
            if excess > 0 and excess > stale.get(eid, 0):
                stale[eid] = excess
                val = a.value_text or a.value_num or a.value_date or ''
                sample_values[eid] = f'{a.attribute_id}={val} ({a.entity.canonical_name[:30]})'

        # Skip recently researched, stalest first
        ranked = sorted(stale.items(), key=lambda kv: -kv[1])
        queued = 0
        self.stdout.write(f'{len(ranked)} entities have stale volatile facts.')

        for i, (eid, excess_days) in enumerate(ranked):
            entity = Entity.objects.filter(pk=eid).first()
            if not entity or entity.canonical_name in recently:
                continue
            if queued >= options['limit']:
                break
            topic_type = _TYPE_TO_TOPIC.get(entity.entity_type, 'company')
            line = (f'  stale {excess_days:>4}d · {entity.canonical_name[:40]:<40} '
                    f'· {sample_values.get(eid, "")}')
            if options['apply']:
                from ingest.tasks.research import research_topic
                research_topic.apply_async(
                    args=[entity.canonical_name, topic_type],
                    kwargs={'cascade_depth': 0},
                    countdown=i * options['stagger'],
                )
                self.stdout.write(self.style.SUCCESS(line))
            else:
                self.stdout.write(f'  [dry] {line}')
            queued += 1

        verb = 'Queued' if options['apply'] else 'Would queue'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} re-research for {queued} stale entities '
            f'(of {len(ranked)} candidates).'
        ))
        if not options['apply']:
            self.stdout.write(self.style.WARNING('Dry run — pass --apply to enqueue.'))
