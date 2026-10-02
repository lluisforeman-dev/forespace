from functools import lru_cache
from openai import OpenAI
from django.conf import settings


class _CreditGuardCompletions:
    """Catch OpenRouter 402 (insufficient credits) on ANY LLM call and raise
    the operator alert centrally — call sites keep their own error handling."""

    def __init__(self, inner):
        self._inner = inner

    def create(self, **kwargs):
        try:
            return self._inner.create(**kwargs)
        except Exception as exc:
            text = str(exc)
            if '402' in text or 'Insufficient credits' in text:
                try:
                    from ingest.alerts import mark_credits_exhausted
                    mark_credits_exhausted()
                except Exception:
                    pass
            raise


class _CreditGuardChat:
    def __init__(self, inner):
        self.completions = _CreditGuardCompletions(inner.completions)


class _CreditGuardClient:
    """Transparent wrapper around the OpenAI client; unknown attributes pass through."""

    def __init__(self, inner):
        self.chat = _CreditGuardChat(inner.chat)
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    """Return a cached OpenAI-compatible client pointed at OpenRouter."""
    return _CreditGuardClient(OpenAI(
        api_key=settings.AI_API_KEY,
        base_url=settings.AI_API_URL,
    ))
