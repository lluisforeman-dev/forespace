"""Runtime prompt loader.

Tasks call get_prompt(key, fallback) to fetch the active prompt from the DB.
If no DB entry exists (e.g. seed_prompts hasn't been run yet), the hardcoded
fallback string is used — so nothing breaks.
"""
import logging

logger = logging.getLogger(__name__)


def get_prompt(key: str, fallback: str) -> str:
    """Return the active system prompt for *key*, falling back to *fallback*."""
    try:
        from core.models import PromptTemplate
        pt = PromptTemplate.objects.filter(key=key, is_active=True).first()
        if pt:
            return pt.system_prompt
    except Exception as exc:
        logger.warning('get_prompt(%s): DB lookup failed, using fallback: %s', key, exc)
    return fallback
