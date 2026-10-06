import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.services.chat_service import process_chat


def test_chat_injects_every_schedule_and_skips_public_rag_for_personal_query():
    captured = {}
    schedules = [
        {
            "termKey": "term-1",
            "createdAt": datetime(2025, 9, 1, tzinfo=timezone.utc),
            "term": {"academicYear": "2025/2026", "semester": "الأول"},
            "courses": [{"courseCode": "101", "courseName": "Course One"}],
        },
        {
            "termKey": "term-2",
            "createdAt": datetime(2026, 2, 1, tzinfo=timezone.utc),
            "term": {"academicYear": "2025/2026", "semester": "الثاني"},
            "courses": [{"courseCode": "201", "courseName": "Course Two"}],
        },
    ]

    async def load_messages(*_args, **_kwargs):
        return []

    async def load_schedules(*_args, **_kwargs):
        return schedules

    async def stream(messages, system_prompt, tools=None):
        captured["messages"] = messages
        captured["system_prompt"] = system_prompt
        captured["tools"] = tools
        yield 'data: {"type":"done","citations":[]}\n\n'

    def discard_background_task(coroutine):
        coroutine.close()

    config = SimpleNamespace(
        HISTORY_TOKEN_BUDGET=3000,
        OPENAI_VECTOR_STORE_ID="vs-test",
        OPENAI_MODEL="test-model",
    )

    async def run_chat():
        return [
            event
            async for event in process_chat(
                uid="user-123",
                session_id="session-123",
                message="قارن بين جداولي",
            )
        ]

    with (
        patch("app.services.chat_service.get_config", return_value=config),
        patch("app.services.chat_service.load_recent_messages", load_messages),
        patch("app.services.chat_service.load_user_schedules", load_schedules),
        patch("app.services.chat_service.stream_response", stream),
        patch(
            "app.services.chat_service.background_tasks.create_task",
            side_effect=discard_background_task,
        ),
    ):
        events = asyncio.run(run_chat())

    assert any('"status": "generating_answer"' in event for event in events)
    assert captured["tools"] == []
    assert '"scheduleCount":2' in captured["system_prompt"]
    assert '"courseCode":"101"' in captured["system_prompt"]
    assert '"courseCode":"201"' in captured["system_prompt"]
