"""Print a cost summary for the last N days.

Usage:
    python manage.py cost_report
    python manage.py cost_report --days 7
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta


class Command(BaseCommand):
    help = 'Print LLM cost summary for the last N days.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30)

    def handle(self, *args, **options):
        from django.db.models import Sum, Count
        from core.models import LLMCall

        since = timezone.now() - timedelta(days=options['days'])
        qs = LLMCall.objects.filter(called_at__gte=since)

        total = qs.aggregate(
            total_cost=Sum('cost_usd'),
            total_calls=Count('id'),
            total_in=Sum('tokens_in'),
            total_out=Sum('tokens_out'),
        )

        self.stdout.write(f'\nCost report — last {options["days"]} days\n' + '─' * 40)
        self.stdout.write(f'  Calls:       {total["total_calls"] or 0}')
        self.stdout.write(f'  Tokens in:   {total["total_in"] or 0:,}')
        self.stdout.write(f'  Tokens out:  {total["total_out"] or 0:,}')
        self.stdout.write(f'  Total cost:  ${total["total_cost"] or 0:.4f}')

        self.stdout.write('\nBy task:')
        for row in qs.values('task').annotate(cost=Sum('cost_usd'), calls=Count('id')).order_by('-cost'):
            self.stdout.write(f'  {row["task"]:12} {row["calls"]:5} calls   ${row["cost"]:.4f}')

        self.stdout.write('\nBy model:')
        for row in qs.values('model').annotate(cost=Sum('cost_usd'), calls=Count('id')).order_by('-cost'):
            self.stdout.write(f'  {row["model"][:40]:40} {row["calls"]:5} calls   ${row["cost"]:.4f}')
