import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from app.services.chat_service import process_chat
from app.services.grounding import sanitize_university_id_answer


CONFIG = SimpleNamespace(
    HISTORY_TOKEN_BUDGET=3000,
    OPENAI_VECTOR_STORE_ID="vs-test",
    OPENAI_MODEL="test-model",
)


def _payload(event: str) -> dict:
    return json.loads(event[6:].strip())


async def _empty_schedules(*_args):
    return []


def _discard_task(coroutine):
    coroutine.close()


def test_unsupported_university_id_renewal_interval_is_removed():
    answer = (
        "تُصدر الهوية من إدارة الأمن والسلامة. "
        "ويتم تجديد الهوية الجامعية كل فصل دراسي."
    )

    sanitized, changed = sanitize_university_id_answer(
        "صلاحية الهوية الجامعية وتجديدها",
        answer,
        ["تُصدر الهوية من إدارة الأمن والسلامة مع مستندات إثبات الطالب."],
    )

    assert changed is True
    assert "إدارة الأمن والسلامة" in sanitized
    assert "كل فصل دراسي" not in sanitized
    assert "لن أفترض" in sanitized


def test_explicit_retrieved_id_renewal_interval_is_preserved():
    claim = "يتم تجديد الهوية الجامعية كل عام دراسي."
    sanitized, changed = sanitize_university_id_answer(
        "صلاحية الهوية الجامعية وتجديدها",
        claim,
        ["يتم تجديد الهوية الجامعية كل عام دراسي."],
    )

    assert changed is False
    assert sanitized == claim


def test_diacritized_arabic_and_english_renewal_variations_are_removed():
    cases = (
        (
            "تجديد الهوية الجامعية",
            "تُجَدَّدُ الهوية الجامعية سنويًّا.",
            "تُجَدَّدُ",
        ),
        (
            "University ID renewal",
            "The university ID is renewed every academic semester.",
            "every academic semester",
        ),
        (
            "صلاحية البطاقة الجامعية",
            "صلاحية البطاقة الجامعية فصل دراسي واحد.",
            "فصل دراسي واحد",
        ),
    )
    for question, answer, unsupported in cases:
        sanitized, changed = sanitize_university_id_answer(question, answer, [])
        assert changed is True
        assert unsupported not in sanitized


def test_guard_preserves_supported_clause_and_readable_punctuation():
    answer = (
        "تُصدر الهوية من إدارة الأمن والسلامة، وتجدد الهوية الجامعية كل فصل دراسي، "
        "ويستلزم بدل الفاقد مستندات محددة."
    )
    sanitized, changed = sanitize_university_id_answer(
        "صلاحية الهوية الجامعية وتجديدها",
        answer,
        ["تُصدر الهوية من إدارة الأمن والسلامة، ويستلزم بدل الفاقد مستندات محددة."],
    )

    assert changed is True
    assert "تُصدر الهوية من إدارة الأمن والسلامة" in sanitized
    assert "بدل الفاقد" in sanitized
    assert "كل فصل دراسي" not in sanitized
    assert "،." not in sanitized


def test_guard_preserves_supported_fact_joined_to_risky_claim():
    answer = "تصدر الهوية من إدارة الأمن ويتم تجديد الهوية الجامعية كل فصل دراسي."
    sanitized, changed = sanitize_university_id_answer(
        "صلاحية الهوية الجامعية وتجديدها",
        answer,
        ["تصدر الهوية من إدارة الأمن."],
    )

    assert changed is True
    assert "تصدر الهوية من إدارة الأمن" in sanitized
    assert "كل فصل دراسي" not in sanitized


def test_unrelated_frequency_sentence_does_not_trigger_guard():
    answer = "يمكن طلب بدل فاقد للهوية. وتستخدم بوابة التسجيل كل فصل دراسي."
    sanitized, changed = sanitize_university_id_answer(
        "تجديد الهوية الجامعية", answer, []
    )

    assert changed is False
    assert sanitized == answer


def test_unrelated_evidence_terms_do_not_support_renewal_interval():
    answer = "يتم تجديد الهوية الجامعية كل عام دراسي."
    evidence = ["يمكن تجديد بدل الفاقد. ويحدث التسجيل في المقررات كل عام دراسي."]
    sanitized, changed = sanitize_university_id_answer(
        "University ID renewal", answer, evidence
    )

    assert changed is True
    assert "كل عام دراسي" not in sanitized


def test_repeated_identical_id_queries_take_same_route_and_safe_answer():
    calls = []

    async def stream(_messages, _prompt, tools=None):
        calls.append(tools)
        yield 'data: {"type":"chunk","content":"تصدر من إدارة الأمن. وتجدد الهوية الجامعية كل فصل دراسي."}\n\n'
        yield 'data: {"type":"done","citations":[{"type":"file_citation","file_id":"file-1"}],"evidence":["تصدر الهوية من إدارة الأمن."]}\n\n'

    async def run_once():
        return [
            event
            async for event in process_chat(
                "uid", "session", "صلاحية الهوية الجامعية وتجديدها"
            )
        ]

    with (
        patch("app.services.chat_service.get_config", return_value=CONFIG),
        patch("app.services.chat_service.load_recent_messages", return_value=[]),
        patch("app.services.chat_service.load_user_schedules", _empty_schedules),
        patch("app.services.chat_service.stream_response", stream),
        patch("app.services.chat_service.background_tasks.create_task", side_effect=_discard_task),
    ):
        first = asyncio.run(run_once())
        second = asyncio.run(run_once())

    assert calls == [
        [{"type": "file_search", "vector_store_ids": ["vs-test"]}],
        [{"type": "file_search", "vector_store_ids": ["vs-test"]}],
    ]
    first_answer = _payload(first[-2])["content"]
    second_answer = _payload(second[-2])["content"]
    assert first_answer == second_answer
    assert "كل فصل دراسي" not in first_answer
    assert _payload(first[-1])["type"] == "done"
    assert "evidence" not in _payload(first[-1])


def test_normal_confusion_follow_up_uses_history_without_retrieval():
    captured = {}
    history = [
        {"isUser": False, "content": "الشرح السابق"},
        {"isUser": True, "content": "اشرح لي التفاضل"},
    ]

    async def stream(messages, _prompt, tools=None):
        captured["messages"] = messages
        captured["tools"] = tools
        yield 'data: {"type":"chunk","content":"خلني أوضحها بمثال أبسط."}\n\n'
        yield 'data: {"type":"done","citations":[]}\n\n'

    async def run():
        return [event async for event in process_chat("uid", "session", "مش فاهم")]

    with (
        patch("app.services.chat_service.get_config", return_value=CONFIG),
        patch("app.services.chat_service.load_recent_messages", return_value=history),
        patch("app.services.chat_service.load_user_schedules", _empty_schedules),
        patch("app.services.chat_service.stream_response", stream),
    ):
        events = asyncio.run(run())

    assert captured["tools"] == []
    assert captured["messages"][-3:] == [
        {"role": "user", "content": "اشرح لي التفاضل"},
        {"role": "assistant", "content": "الشرح السابق"},
        {"role": "user", "content": "مش فاهم"},
    ]
    assert [_payload(event)["type"] for event in events] == ["status", "chunk", "done"]


def test_no_citations_after_web_fallback_returns_missing_evidence_answer():
    calls = 0

    async def stream(_messages, _prompt, tools=None):
        nonlocal calls
        calls += 1
        if tools[0]["type"] == "file_search":
            yield 'data: {"type":"chunk","content":"لا توجد معلومات"}\n\n'
            yield 'data: {"type":"done","citations":[]}\n\n'
        else:
            yield 'data: {"type":"chunk","content":"عادةً تتجدد الهوية كل سنة."}\n\n'
            yield 'data: {"type":"done","citations":[]}\n\n'

    async def run():
        return [
            event
            async for event in process_chat(
                "uid", "session", "صلاحية الهوية الجامعية وتجديدها"
            )
        ]

    with (
        patch("app.services.chat_service.get_config", return_value=CONFIG),
        patch("app.services.chat_service.load_recent_messages", return_value=[]),
        patch("app.services.chat_service.load_user_schedules", _empty_schedules),
        patch("app.services.chat_service.stream_response", stream),
        patch("app.services.chat_service.background_tasks.create_task", side_effect=_discard_task),
    ):
        events = asyncio.run(run())

    assert calls == 2
    answer = _payload(events[-2])["content"]
    assert "كل سنة" not in answer
    assert "لن أخمّن" in answer
    assert _payload(events[-1]) == {"type": "done", "citations": []}


def test_sse_done_follows_sequential_firestore_writes():
    order = []

    async def save_user(*_args, **_kwargs):
        order.append("user-save-start")
        await asyncio.sleep(0)
        order.append("user-save-end")

    async def save_assistant(*_args, **_kwargs):
        order.append("assistant-save-start")
        await asyncio.sleep(0)
        order.append("assistant-save-end")

    async def stream(*_args, **_kwargs):
        yield 'data: {"type":"chunk","content":"جواب"}\n\n'
        yield 'data: {"type":"done","citations":[]}\n\n'

    async def run():
        events = []
        async for event in process_chat("uid", "session", "اشرح التفاضل"):
            events.append(event)
            order.append(_payload(event)["type"])
        return events

    with (
        patch("app.services.chat_service.get_config", return_value=CONFIG),
        patch("app.services.chat_service.load_recent_messages", return_value=[]),
        patch("app.services.chat_service.load_user_schedules", _empty_schedules),
        patch("app.services.chat_service.save_user_message", save_user),
        patch("app.services.chat_service.save_assistant_message", save_assistant),
        patch("app.services.chat_service.stream_response", stream),
    ):
        events = asyncio.run(run())

    assert order.index("user-save-end") < order.index("assistant-save-start")
    assert order.index("assistant-save-end") < order.index("done")
    assert [_payload(event)["type"] for event in events] == ["status", "chunk", "done"]


def test_initial_status_precedes_slow_bounded_user_persistence():
    assistant_calls = 0

    async def slow_user(*_args, **_kwargs):
        await asyncio.sleep(10)

    async def save_assistant(*_args, **_kwargs):
        nonlocal assistant_calls
        assistant_calls += 1

    async def stream(*_args, **_kwargs):
        yield 'data: {"type":"chunk","content":"جواب"}\n\n'
        yield 'data: {"type":"done","citations":[]}\n\n'

    async def run():
        generator = process_chat("uid", "session", "اشرح التفاضل")
        first = await asyncio.wait_for(anext(generator), timeout=0.1)
        rest = [event async for event in generator]
        return [first, *rest]

    with (
        patch("app.services.chat_service.PERSISTENCE_TIMEOUT_SECONDS", 0.01),
        patch("app.services.chat_service.get_config", return_value=CONFIG),
        patch("app.services.chat_service.load_recent_messages", return_value=[]),
        patch("app.services.chat_service.load_user_schedules", _empty_schedules),
        patch("app.services.chat_service.save_user_message", slow_user),
        patch("app.services.chat_service.save_assistant_message", save_assistant),
        patch("app.services.chat_service.stream_response", stream),
    ):
        events = asyncio.run(run())

    assert _payload(events[0]) == {"type": "status", "status": "generating_answer"}
    assert [_payload(event)["type"] for event in events].count("done") == 1
    assert assistant_calls == 0


def test_assistant_persistence_failure_is_not_retried_or_duplicated():
    attempts = 0

    async def failing_assistant(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("Firestore unavailable")

    async def stream(*_args, **_kwargs):
        yield 'data: {"type":"chunk","content":"جواب"}\n\n'
        yield 'data: {"type":"done","citations":[]}\n\n'

    async def run():
        return [event async for event in process_chat("uid", "session", "اشرح التفاضل")]

    with (
        patch("app.services.chat_service.get_config", return_value=CONFIG),
        patch("app.services.chat_service.load_recent_messages", return_value=[]),
        patch("app.services.chat_service.load_user_schedules", _empty_schedules),
        patch("app.services.chat_service.save_assistant_message", failing_assistant),
        patch("app.services.chat_service.stream_response", stream),
    ):
        events = asyncio.run(run())

    assert attempts == 1
    assert [_payload(event)["type"] for event in events] == ["status", "chunk", "done"]


def test_slow_assistant_persistence_is_bounded_and_attempted_once():
    attempts = 0

    async def slow_assistant(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(10)

    async def stream(*_args, **_kwargs):
        yield 'data: {"type":"chunk","content":"جواب"}\n\n'
        yield 'data: {"type":"done","citations":[]}\n\n'

    async def run():
        return await asyncio.wait_for(
            _collect(process_chat("uid", "session", "اشرح التفاضل")),
            timeout=0.2,
        )

    with (
        patch("app.services.chat_service.PERSISTENCE_TIMEOUT_SECONDS", 0.01),
        patch("app.services.chat_service.get_config", return_value=CONFIG),
        patch("app.services.chat_service.load_recent_messages", return_value=[]),
        patch("app.services.chat_service.load_user_schedules", _empty_schedules),
        patch("app.services.chat_service.save_assistant_message", slow_assistant),
        patch("app.services.chat_service.stream_response", stream),
    ):
        events = asyncio.run(run())

    assert attempts == 1
    assert [_payload(event)["type"] for event in events].count("done") == 1


def test_cancellation_persists_partial_answer_once_without_done():
    attempts = 0
    chunk_seen = asyncio.Event()

    async def save_assistant(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1

    async def stream(*_args, **_kwargs):
        yield 'data: {"type":"chunk","content":"جزء من الجواب"}\n\n'
        chunk_seen.set()
        await asyncio.Event().wait()

    async def run():
        observed = []

        async def consume():
            async for event in process_chat("uid", "session", "اشرح التفاضل"):
                observed.append(event)

        task = asyncio.create_task(consume())
        await asyncio.wait_for(chunk_seen.wait(), timeout=0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return observed

    with (
        patch("app.services.chat_service.get_config", return_value=CONFIG),
        patch("app.services.chat_service.load_recent_messages", return_value=[]),
        patch("app.services.chat_service.load_user_schedules", _empty_schedules),
        patch("app.services.chat_service.save_assistant_message", save_assistant),
        patch("app.services.chat_service.stream_response", stream),
    ):
        events = asyncio.run(run())

    assert attempts == 1
    assert [_payload(event)["type"] for event in events] == ["status", "chunk"]


def test_web_tool_error_is_forwarded_without_answer_or_done():
    assistant_calls = 0

    async def save_assistant(*_args, **_kwargs):
        nonlocal assistant_calls
        assistant_calls += 1

    async def stream(*_args, **_kwargs):
        yield 'data: {"type":"error","message":"tool failed"}\n\n'

    async def run():
        return [
            event
            async for event in process_chat(
                "uid", "session", "ما آخر إعلان نشرته جامعة الكويت اليوم؟"
            )
        ]

    with (
        patch("app.services.chat_service.get_config", return_value=CONFIG),
        patch("app.services.chat_service.load_recent_messages", return_value=[]),
        patch("app.services.chat_service.load_user_schedules", _empty_schedules),
        patch("app.services.chat_service.save_assistant_message", save_assistant),
        patch("app.services.chat_service.stream_response", stream),
    ):
        events = asyncio.run(run())

    assert [_payload(event)["type"] for event in events] == ["status", "error"]
    assert assistant_calls == 0


async def _collect(generator):
    return [event async for event in generator]
