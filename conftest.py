"""Pytest configuration — point at dev settings so Django is set up for tests."""
import django
from django.conf import settings


def pytest_configure():
    import os
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.dev')
    django.setup()
    # Tests create/drop a scratch database (test_neondb). Neon's PgBouncer
    # pooler holds server sessions and intermittently blocks CREATE/DROP
    # DATABASE ("being accessed by other users"). Point the test run at the
    # direct (non-pooler) endpoint.
    db = settings.DATABASES['default']
    host = db.get('HOST') or ''
    if '-pooler.' in host:
        db['HOST'] = host.replace('-pooler.', '.')
