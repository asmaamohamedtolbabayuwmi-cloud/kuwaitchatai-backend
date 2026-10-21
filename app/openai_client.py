"""
app/openai_client.py

Provides a singleton async OpenAI client.
The async client allows non-blocking API calls inside FastAPI coroutines.
"""

from functools import lru_cache

from openai import AsyncOpenAI

from .config import get_config
from .local_notification_test import enabled as local_notification_test_enabled


@lru_cache(maxsize=1)
def get_openai_client():
    """Return a singleton async OpenAI client."""
    if local_notification_test_enabled():
        from .local_openai_mock import client

        return client
    config = get_config()
    return AsyncOpenAI(
        api_key=config.OPENAI_API_KEY,
        timeout=config.OPENAI_TIMEOUT
    )
