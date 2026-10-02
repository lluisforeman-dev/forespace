"""Operational alerts — provider-level conditions the operator must act on.

Currently: OpenRouter credit exhaustion. When any LLM call hits a 402, the
pipeline pauses (existing PAUSE_FLAG) AND a persistent alert is recorded so
the dashboard shows why — plus a best-effort email to the operator.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CREDITS_FLAG = 'eigengraph:alert:credits'


def _redis():
    import redis as _r
    from django.conf import settings
    return _r.from_url(settings.CELERY_BROKER_URL)


def mark_credits_exhausted() -> None:
    """Record the credits-exhausted alert and email the operator (once).

    Never raises — alerting must not break the error path it reports on.
    """
    try:
        r = _redis()
        if r.exists(CREDITS_FLAG):
            return  # already alerted — no duplicate emails
        r.set(CREDITS_FLAG, datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        logger.warning('credits alert: could not persist flag — %s', exc)
        return
    _send_email()
    logger.warning('ALERT: OpenRouter credits exhausted — research paused')


def credits_alert() -> dict | None:
    """Active alert payload ({'since': iso}) or None. Fail-safe."""
    try:
        raw = _redis().get(CREDITS_FLAG)
        if not raw:
            return None
        ts = raw.decode() if isinstance(raw, bytes) else str(raw)
        return {'since': ts}
    except Exception:
        return None


def clear_credits_alert() -> None:
    try:
        _redis().delete(CREDITS_FLAG)
    except Exception:
        pass


def _send_email() -> None:
    from django.conf import settings
    from django.core.mail import send_mail

    recipients = [addr for _, addr in getattr(settings, 'ADMINS', [])]
    if not recipients and getattr(settings, 'DEFAULT_FROM_EMAIL', ''):
        recipients = [settings.DEFAULT_FROM_EMAIL]
    if not recipients:
        logger.info('credits alert: no recipients configured (ADMINS/DEFAULT_FROM_EMAIL) — banner only')
        return
    try:
        send_mail(
            '[EigenGraph] OpenRouter credits exhausted — research paused',
            'OpenRouter returned 402 (insufficient credits). All research is paused.\n\n'
            'Add credits: https://openrouter.ai/settings/credits\n'
            'Then click "Resume all" on the dashboard.\n',
            settings.DEFAULT_FROM_EMAIL or 'noreply@eigengraph.local',
            recipients,
            fail_silently=True,
        )
    except Exception as exc:
        logger.warning('credits alert email failed: %s', exc)


def openrouter_credit_balance() -> dict | None:
    """Live credit state from OpenRouter: {'usage', 'limit', 'remaining'} or None.

    Cached 5 minutes. limit=None means an unlimited/open route — remaining None.
    """
    from django.core.cache import cache

    cache_key = 'eigengraph:credits:balance'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    from django.conf import settings
    if not getattr(settings, 'AI_API_KEY', ''):
        return None
    try:
        import requests
        resp = requests.get(
            'https://openrouter.ai/api/v1/auth/key',
            headers={'Authorization': f"Bearer {settings.AI_API_KEY}"},
            timeout=8,
        )
        data = (resp.json() or {}).get('data', {})
        usage = data.get('usage')
        limit = data.get('limit')
        remaining = (round(limit - usage, 2) if limit is not None and usage is not None else None)
        balance = {'usage': usage, 'limit': limit, 'remaining': remaining}
        cache.set(cache_key, balance, 300)
        return balance
    except Exception as exc:
        logger.debug('openrouter_credit_balance: %s', exc)
        return None
