import pytest


@pytest.fixture(autouse=True)
def _mock_chat_persistence(monkeypatch):
    """Unit tests must never reach Firestore unless they explicitly opt in."""
    from app.services import chat_service

    async def save_user(*_args, **_kwargs):
        return "user-message"

    async def save_assistant(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat_service, "save_user_message", save_user)
    monkeypatch.setattr(chat_service, "save_assistant_message", save_assistant)
