"""Pytest configuration — point at dev settings so Django is set up for tests."""
import django
from django.conf import settings


def pytest_configure():
    import os
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.dev')
