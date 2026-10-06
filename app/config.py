"""
app/config.py

Reads and validates all required environment variables at startup.
Set these on Railway via the Railway dashboard → Variables tab.
"""
from dotenv import load_dotenv

import os
from functools import lru_cache

load_dotenv()
class Config:
    # ── OpenAI ────────────────────────────────────────────────────────────
    OPENAI_API_KEY: str
    OPENAI_VECTOR_STORE_ID: str
    # gpt-4o | gpt-4o-mini | etc.  Override via env var to change model.
    OPENAI_MODEL: str
    # Vision-capable model used to extract student schedule PDFs.
    OPENAI_SCHEDULE_MODEL: str

    # ── Firebase ──────────────────────────────────────────────────────────
    # Paste the entire service-account JSON as a single-line string.
    # Railway → Variables → FIREBASE_SERVICE_ACCOUNT_JSON = '{"type":"service_account",...}'
    FIREBASE_SERVICE_ACCOUNT_JSON: str | None

    # ── Chat history ──────────────────────────────────────────────────────
    # Rough token budget for conversation history injected into each request.
    HISTORY_TOKEN_BUDGET: int

    def __init__(self) -> None:
        self.OPENAI_API_KEY = _require("OPENAI_API_KEY")
        self.OPENAI_VECTOR_STORE_ID = _require("OPENAI_VECTOR_STORE_ID")
        self.OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.OPENAI_SCHEDULE_MODEL = os.getenv(
            "OPENAI_SCHEDULE_MODEL", self.OPENAI_MODEL
        )
        self.FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv(
            "FIREBASE_SERVICE_ACCOUNT_JSON"
        )
        self.HISTORY_TOKEN_BUDGET = int(
            os.getenv("HISTORY_TOKEN_BUDGET", "3000")
        )
        self.OPENAI_TIMEOUT = float(
            os.getenv("OPENAI_TIMEOUT", "60.0")
        )


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(
            f"Required environment variable '{key}' is not set. "
            "Add it in Railway → Variables."
        )
    return value


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Return a singleton Config instance (validated once on first call)."""
    return Config()
