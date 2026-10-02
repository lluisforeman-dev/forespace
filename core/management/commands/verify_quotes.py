"""Backfill: mechanically verify quotes of already-stored claims against their
cited sources (§7c on the eigensearch path).

Fetches cited URLs (robots.txt honored, per-domain paced, raw text archived on
the Document so it is never fetched twice) and checks each claim's quote is a
verbatim substring. Verified claims gain +5 confidence; fabrications lose 20
and drop out of 'accepted' if they fall under 50.

Preview by default — shows the outcome distribution without writing.
Unsafe-fetch failures (404, paywall, bot-blocked) never penalise a claim.

Usage:
    python manage.py verify_quotes                    # preview first 100
    python manage.py verify_quotes --apply --limit 500
"""
from django.core.management.base import BaseCommand

from core.models import Assertion


class Command(BaseCommand):
    help = 'Verify assertion quotes against their cited web sources.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--apply', action='store_true',
                            help='Write verification results (default: fetch + report only).')

    def handle(self, *args, **options):
        from ingest.tasks.verify import verify_assertion_quote

        assertions = (
            Assertion.objects
            .filter(document__isnull=False, document__url__isnull=False)
            .exclude(document__url='')
            .filter(quote_verified__isnull=True)
            .exclude(quote='')
            .select_related('document', 'document__source')
            .order_by('-confidence')[:options['limit']]
        )

        counts = {'verified': 0, 'failed': 0, 'weak': 0, 'unfetchable': 0, 'skipped': 0}
        for a in assertions:
            if options['apply']:
                outcome = verify_assertion_quote(a)
            else:
                # Preview: fetch + check, but write nothing
                from ingest.tasks.verify import _MIN_QUOTE_LEN
                import requests
                from ingest.tasks.crawl import _HEADERS, _pace_domain, _robots_parser
                from urllib.parse import urlparse
                import trafilatura

                doc, quote = a.document, (a.quote or '').strip()
                if not doc.url or not quote:
                    outcome = 'skipped'
                elif len(quote) < _MIN_QUOTE_LEN:
                    outcome = 'weak'
                else:
                    try:
                        domain = urlparse(doc.url).netloc
                        rp = _robots_parser(domain)
                        if rp is not None and not rp.can_fetch('EigenGraph', doc.url):
                            outcome = 'unfetchable'
                        else:
                            _pace_domain(domain)
                            resp = requests.get(doc.url, headers=_HEADERS,
                                                timeout=15, allow_redirects=True)
                            text = resp.content.decode('utf-8', errors='replace') \
                                if resp.status_code == 200 else ''
                            if not text:
                                outcome = 'unfetchable'
                            else:
                                hit = quote in text or quote in (
                                    trafilatura.extract(text, favor_recall=True) or '')
                                outcome = 'verified' if hit else 'failed'
                    except Exception:
                        outcome = 'unfetchable'
            counts[outcome] += 1
            mark = {'verified': '+', 'failed': 'X', 'weak': 'w',
                    'unfetchable': '?', 'skipped': '-'}[outcome]
            self.stdout.write(
                f'  [{mark}] {a.entity.canonical_name[:34]:<34} '
                f'{a.attribute_id[:24]:<24} {a.confidence:>3}'
            )

        self.stdout.write(self.style.SUCCESS(
            f"\n{sum(counts.values())} checked: {counts['verified']} verified, "
            f"{counts['failed']} failed, {counts['weak']} too weak, "
            f"{counts['unfetchable']} unfetchable, {counts['skipped']} skipped."
        ))
        if not options['apply']:
            self.stdout.write(self.style.WARNING('Dry run — pass --apply to write.'))
