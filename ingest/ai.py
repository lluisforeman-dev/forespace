from functools import lru_cache
from openai import OpenAI
from django.conf import settings


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    """Return a cached OpenAI-compatible client pointed at OpenRouter."""
    return OpenAI(
        api_key=settings.AI_API_KEY,
        base_url=settings.AI_API_URL,
    )
