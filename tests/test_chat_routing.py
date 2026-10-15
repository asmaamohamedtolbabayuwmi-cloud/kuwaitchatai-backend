import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from app.services.chat_service import process_chat


CONFIG = SimpleNamespace(
    HISTORY_TOKEN_BUDGET=3000,
    OPENAI_VECTOR_STORE_ID="vs-test",
    OPENAI_MODEL="test-model",
)


def _payload(event: str) -> dict:
    assert event.startswith("data: ")
    return json.loads(event[6:].strip())


def _discard_task(coroutine):
    coroutine.close()


def test_educational_request_skips_search_and_preserves_sse_contract():
    captured = {}

    async def load_messages(*_args):
        return []

    async def load_schedules(*_args):
        return []

    async def stream(messages, system_prompt, tools=None):
        captured["tools"] = tools
        yield 'data: {"type":"chunk","content":"الشرح"}\n\n'
        yield 'data: {"type":"done","citations":[]}\n\n'

    async def run():
        return [
            item
            async for item in process_chat(
                "uid", "session", "اشرح لي التفاضل مع مثال"
            )
        ]

    with (
        patch("app.services.chat_service.get_config", return_value=CONFIG),
        patch("app.services.chat_service.load_recent_messages", load_messages),
        patch("app.services.chat_service.load_user_schedules", load_schedules),
        patch("app.services.chat_service.stream_response", stream),
        patch("app.services.chat_service.background_tasks.create_task", side_effect=_discard_task),
    ):
        events = asyncio.run(run())

    assert captured["tools"] == []
    assert [_payload(event)["type"] for event in events] == ["status", "chunk", "done"]
    assert _payload(events[0])["status"] == "generating_answer"


def test_official_request_uses_file_search_then_open_web_fallback():
    calls = []

    async def load_messages(*_args):
        return []

    async def load_schedules(*_args):
        return []

    async def stream(messages, system_prompt, tools=None):
        calls.append({"prompt": system_prompt, "tools": tools})
        if tools and tools[0]["type"] == "file_search":
            yield 'data: {"type":"chunk","content":"لا أملك معلومات كافية"}\n\n'
            yield 'data: {"type":"done","citations":[]}\n\n'
        else:
            yield 'data: {"type":"chunk","content":"إجابة موثقة"}\n\n'
            yield 'data: {"type":"done","citations":[{"type":"url_citation","url":"https://example.com"}]}\n\n'

    async def run():
        return [
            item
            async for item in process_chat(
                "uid", "session", "ما شروط القبول في جامعة الكويت؟"
            )
        ]

    with (
        patch("app.services.chat_service.get_config", return_value=CONFIG),
        patch("app.services.chat_service.load_recent_messages", load_messages),
        patch("app.services.chat_service.load_user_schedules", load_schedules),
        patch("app.services.chat_service.stream_response", stream),
        patch("app.services.chat_service.background_tasks.create_task", side_effect=_discard_task),
    ):
        events = asyncio.run(run())

    assert calls[0]["tools"] == [
        {"type": "file_search", "vector_store_ids": ["vs-test"]}
    ]
    assert calls[1]["tools"] == [{"type": "web_search"}]
    assert "Search the open web" in calls[1]["prompt"]
    statuses = [_payload(event)["status"] for event in events if _payload(event)["type"] == "status"]
    assert statuses == ["searching_knowledge", "searching_ku_web"]
    assert [_payload(event)["type"] for event in events[-2:]] == ["chunk", "done"]


def test_uncertain_request_uses_existing_university_retrieval_path():
    captured = {}

    async def stream(messages, system_prompt, tools=None):
        captured["tools"] = tools
        yield 'data: {"type":"chunk","content":"معلومة من المصدر"}\n\n'
        yield 'data: {"type":"done","citations":[{"type":"file_citation","file_id":"file-1"}]}\n\n'

    async def run():
        return [
            item
            async for item in process_chat(
                "uid", "session", "متى تفتح مكتبة جابر؟"
            )
        ]

    with (
        patch("app.services.chat_service.get_config", return_value=CONFIG),
        patch("app.services.chat_service.load_recent_messages", return_value=[]),
        patch("app.services.chat_service.load_user_schedules", return_value=[]),
        patch("app.services.chat_service.stream_response", stream),
        patch("app.services.chat_service.background_tasks.create_task", side_effect=_discard_task),
    ):
        events = asyncio.run(run())

    assert captured["tools"] == [
        {"type": "file_search", "vector_store_ids": ["vs-test"]}
    ]
    statuses = [
        _payload(event)["status"]
        for event in events
        if _payload(event)["type"] == "status"
    ]
    assert statuses == ["searching_knowledge", "generating_answer"]
    assert [_payload(event)["type"] for event in events[-2:]] == ["chunk", "done"]


def test_time_sensitive_request_bypasses_file_search_and_uses_open_web():
    calls = []

    async def stream(messages, system_prompt, tools=None):
        calls.append({"prompt": system_prompt, "tools": tools})
        yield 'data: {"type":"chunk","content":"تعذر التحقق من المنشور الأصلي"}\n\n'
        yield 'data: {"type":"done","citations":[{"type":"url_citation","url":"https://example.com/lead"}]}\n\n'

    async def run():
        return [
            item
            async for item in process_chat(
                "uid",
                "session",
                "ما آخر إعلان نشرته جامعة الكويت على حسابها الرسمي في X؟",
            )
        ]

    with (
        patch("app.services.chat_service.get_config", return_value=CONFIG),
        patch("app.services.chat_service.load_recent_messages", return_value=[]),
        patch("app.services.chat_service.load_user_schedules", return_value=[]),
        patch("app.services.chat_service.stream_response", stream),
        patch("app.services.chat_service.background_tasks.create_task", side_effect=_discard_task),
    ):
        events = asyncio.run(run())

    assert len(calls) == 1
    assert calls[0]["tools"] == [{"type": "web_search"}]
    assert "original official post" in calls[0]["prompt"]
    assert "sotwe.com" in calls[0]["prompt"]
    assert [_payload(event)["type"] for event in events] == ["status", "chunk", "done"]
    assert _payload(events[0])["status"] == "searching_ku_web"


def test_history_snapshot_precedes_save_and_keeps_legitimate_repeat():
    order = []
    captured = {}

    async def load_messages(*_args):
        order.append("load")
        return [
            {"isUser": False, "content": "Previous answer"},
            {"isUser": True, "content": "اشرح التفاضل"},
        ]

    async def load_schedules(*_args):
        return []

    async def save_user(*_args, **_kwargs):
        order.append("save")

    def capture_task(coroutine):
        order.append("task-created")
        coroutine.close()

    async def stream(messages, system_prompt, tools=None):
        captured["messages"] = messages
        yield 'data: {"type":"done","citations":[]}\n\n'

    async def run():
        return [
            item
            async for item in process_chat(
                "uid", "session", "اشرح التفاضل"
            )
        ]

    with (
        patch("app.services.chat_service.get_config", return_value=CONFIG),
        patch("app.services.chat_service.load_recent_messages", load_messages),
        patch("app.services.chat_service.load_user_schedules", load_schedules),
        patch("app.services.chat_service.save_user_message", save_user),
        patch("app.services.chat_service.stream_response", stream),
        patch("app.services.chat_service.background_tasks.create_task", side_effect=capture_task),
    ):
        asyncio.run(run())

    assert order[0] == "load"
    assert order.index("load") < order.index("task-created")
    repeated = [
        item for item in captured["messages"]
        if item == {"role": "user", "content": "اشرح التفاضل"}
    ]
    assert len(repeated) == 2


def test_out_of_scope_request_does_not_search():
    captured = {}

    async def stream(messages, system_prompt, tools=None):
        captured["tools"] = tools
        yield 'data: {"type":"done","citations":[]}\n\n'

    async def run():
        return [item async for item in process_chat("uid", "session", "من فاز بالمباراة؟")]

    with (
        patch("app.services.chat_service.get_config", return_value=CONFIG),
        patch("app.services.chat_service.load_recent_messages", return_value=[]),
        patch("app.services.chat_service.load_user_schedules", return_value=[]),
        patch("app.services.chat_service.stream_response", stream),
        patch("app.services.chat_service.background_tasks.create_task", side_effect=_discard_task),
    ):
        asyncio.run(run())

    assert captured["tools"] == []
