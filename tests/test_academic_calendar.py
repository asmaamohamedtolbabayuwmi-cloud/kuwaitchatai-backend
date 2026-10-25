import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.services.academic_calendar import (
    OFFICIAL_SOURCES,
    CalendarEvent,
    CalendarQuery,
    CollegeScope,
    EvidenceResolution,
    Semester,
    format_calendar_answer,
    lookup_official_calendar,
    resolve_calendar_query,
    verify_official_calendar_html,
)
from app.services.chat_service import process_chat


CONFIG = SimpleNamespace(
    HISTORY_TOKEN_BUDGET=3000,
    OPENAI_VECTOR_STORE_ID="vs-test",
    OPENAI_MODEL="test-model",
)
TODAY = date(2026, 10, 24)
CHECKED_AT = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)

GENERAL_HTML = """
<html><body>
<h2>الفصل الدراسي الأول 2027/2026</h2>
<div>آخر يوم لوقف القيد</div><div>22/10/2026</div>
<div>آخر يوم للانسحاب من المقررات</div><div>24/10/2026</div>
<div>آخر يوم لتقديم التماسات الانسحاب الكلي</div><div>26/11/2026</div>
<h2>الفصل الدراسي الثاني 2027/2026</h2>
<div>آخر يوم للانسحاب من المقررات</div><div>13/3/2027</div>
</body></html>
"""

LAW_HTML = """
<html><body>
<h2>2026 - 2027</h2><h3>الفصل الدراسي الأول</h3>
<table><tr><td>الخميس</td><td>2026/10/22</td><td>
<ul><li>آخر يوم لوقف القيد الاختياري</li><li>آخر يوم للانسحاب من المقررات</li></ul>
</td></tr></table>
<h3>الفصل الدراسي الثاني</h3>
<table><tr><td>2027/03/11</td><td>آخر يوم للانسحاب من المقررات</td></tr></table>
</body></html>
"""


def _discard_task(coroutine):
    coroutine.close()


def _payload(event: str) -> dict:
    return json.loads(event[6:].strip())


async def _load_schedules(*_args):
    return []


def _verified(query: CalendarQuery, html: str) -> EvidenceResolution:
    return verify_official_calendar_html(
        query,
        html,
        OFFICIAL_SOURCES[query.college_scope],
        checked_at=CHECKED_AT,
    )


def _run_chat(message: str, history: list[dict] | None = None):
    async def load_messages(*_args):
        return history or []

    async def official_lookup(query):
        html = LAW_HTML if query.college_scope is CollegeScope.LAW else GENERAL_HTML
        return _verified(query, html)

    async def unexpected_openai(*_args, **_kwargs):
        raise AssertionError("OpenAI must not generate official calendar dates")
        yield  # pragma: no cover

    async def run():
        return [event async for event in process_chat("uid", "session", message)]

    with (
        patch("app.services.chat_service.get_config", return_value=CONFIG),
        patch("app.services.chat_service.load_recent_messages", load_messages),
        patch("app.services.chat_service.load_user_schedules", _load_schedules),
        patch("app.services.chat_service.lookup_official_calendar", official_lookup),
        patch("app.services.chat_service.stream_response", unexpected_openai),
        patch(
            "app.services.chat_service.background_tasks.create_task",
            side_effect=_discard_task,
        ),
        patch("app.services.academic_calendar.kuwait_today", return_value=TODAY),
    ):
        return asyncio.run(run())


def test_general_fall_2026_course_withdrawal_is_verified_from_page_content():
    events = _run_chat("متى آخر يوم سحب مواد للفصل الأول 2026/2027؟")

    answer = _payload(events[1])["content"]
    citation = _payload(events[2])["citations"][0]
    assert "24 أكتوبر 2026" in answer
    assert "جامعة الكويت" in answer
    assert citation["url"] == OFFICIAL_SOURCES[CollegeScope.GENERAL].url
    assert _payload(events[0])["status"] == "searching_ku_web"


def test_law_calendar_overrides_general_calendar_for_same_event_and_term():
    events = _run_chat("متى آخر يوم سحب مواد بكلية الحقوق للفصل الأول 2026/2027؟")

    answer = _payload(events[1])["content"]
    citation = _payload(events[2])["citations"][0]
    assert "22 أكتوبر 2026" in answer
    assert "كلية الحقوق" in answer
    assert "24 أكتوبر" not in answer
    assert citation["url"] == OFFICIAL_SOURCES[CollegeScope.LAW].url


def test_college_follow_up_retains_event_year_and_semester():
    history = [
        {"isUser": False, "content": "إجابة سابقة"},
        {"isUser": True, "content": "متى آخر يوم سحب مواد للفصل الأول 2026/2027؟"},
    ]

    events = _run_chat("طب بكلية الحقوق؟", history)

    assert "22 أكتوبر 2026" in _payload(events[1])["content"]


def test_observed_law_college_clarification_retains_calendar_request():
    history = [
        {"isUser": False, "content": "الموعد العام هو 24 أكتوبر 2026"},
        {"isUser": True, "content": "متى آخر يوم للانسحاب من المقررات للفصل الأول؟"},
    ]

    events = _run_chat("انا كلية حقوق", history)

    assert [_payload(event)["type"] for event in events] == ["status", "chunk", "done"]
    assert "22 أكتوبر 2026" in _payload(events[1])["content"]
    assert "24 أكتوبر" not in _payload(events[1])["content"]


def test_exact_calendar_sequence_uses_persisted_first_turn_for_law_follow_up():
    messages = []

    async def load_messages(*_args):
        return list(reversed(messages))

    async def save_user(uid, session_id, content, **_kwargs):
        del uid, session_id
        messages.append({"isUser": True, "content": content})

    async def save_assistant(uid, session_id, content, citations):
        del uid, session_id
        messages.append({"isUser": False, "content": content, "citations": citations})

    async def official_lookup(query):
        html = LAW_HTML if query.college_scope is CollegeScope.LAW else GENERAL_HTML
        return _verified(query, html)

    async def run(message):
        return [event async for event in process_chat("uid", "session", message)]

    with (
        patch("app.services.chat_service.get_config", return_value=CONFIG),
        patch("app.services.chat_service.load_recent_messages", load_messages),
        patch("app.services.chat_service.load_user_schedules", _load_schedules),
        patch("app.services.chat_service.save_user_message", save_user),
        patch("app.services.chat_service.save_assistant_message", save_assistant),
        patch("app.services.chat_service.lookup_official_calendar", official_lookup),
        patch("app.services.academic_calendar.kuwait_today", return_value=TODAY),
    ):
        first = asyncio.run(run("متى آخر يوم للانسحاب من المقررات للفصل الأول؟"))
        second = asyncio.run(run("انا كلية حقوق"))

    assert "24 أكتوبر 2026" in _payload(first[-2])["content"]
    assert "22 أكتوبر 2026" in _payload(second[-2])["content"]
    assert "24 أكتوبر" not in _payload(second[-2])["content"]
    assert [_payload(event)["type"] for event in second] == ["status", "chunk", "done"]
    assert [message["isUser"] for message in messages] == [True, False, True, False]


def test_this_year_and_current_semester_follow_ups_retain_original_event():
    history = [
        {"isUser": False, "content": "إجابة غير صحيحة"},
        {"isUser": True, "content": "متى آخر يوم سحب مواد؟"},
    ]

    assert "24 أكتوبر 2026" in _payload(_run_chat("أقصد السنة دي", history)[1])["content"]
    assert "24 أكتوبر 2026" in _payload(_run_chat("أقصد الفصل الأول الحالي", history)[1])["content"]


def test_explicit_2027_question_does_not_reuse_2026_evidence():
    query = resolve_calendar_query(
        "When is the first semester course withdrawal deadline in 2027?",
        [],
        today=TODAY,
    )
    evidence = verify_official_calendar_html(
        query,
        GENERAL_HTML,
        OFFICIAL_SOURCES[CollegeScope.GENERAL],
        checked_at=CHECKED_AT,
    )

    assert query.academic_year == "2027/2028"
    assert evidence.status == "missing"
    answer = format_calendar_answer(query, evidence, english=True)
    assert "couldn't verify" in answer
    assert "24 October 2026" not in answer


def test_add_drop_is_not_confused_with_course_withdrawal():
    query = resolve_calendar_query(
        "متى السحب والإضافة للفصل الأول 2026/2027؟",
        [],
        today=TODAY,
    )
    evidence = verify_official_calendar_html(
        query,
        GENERAL_HTML,
        OFFICIAL_SOURCES[CollegeScope.GENERAL],
        checked_at=CHECKED_AT,
    )

    assert query.event is CalendarEvent.ADD_DROP
    assert evidence.status == "missing"
    assert "24 أكتوبر" not in format_calendar_answer(query, evidence)


def test_missing_and_conflicting_official_content_never_fabricates_a_date():
    query = CalendarQuery(
        CalendarEvent.COURSE_WITHDRAWAL,
        "2026/2027",
        Semester.FIRST,
    )
    conflicting_html = """
    <h2>الفصل الدراسي الأول 2026/2027</h2>
    <p>آخر يوم للانسحاب من المقررات 24/10/2026</p>
    <p>آخر يوم للانسحاب من المقررات 25/10/2026</p>
    """
    conflict = verify_official_calendar_html(
        query,
        conflicting_html,
        OFFICIAL_SOURCES[CollegeScope.GENERAL],
        checked_at=CHECKED_AT,
    )
    missing = verify_official_calendar_html(
        query,
        "<h2>الفصل الدراسي الأول 2026/2027</h2><p>لا توجد بيانات</p>",
        OFFICIAL_SOURCES[CollegeScope.GENERAL],
        checked_at=CHECKED_AT,
    )

    assert conflict.status == "conflict"
    assert "متعارضة" in format_calendar_answer(query, conflict)
    assert missing.status == "missing"
    assert "لن أخمّن" in format_calendar_answer(query, missing)


def test_lookup_refetches_official_source_and_does_not_cache_old_date():
    query = CalendarQuery(
        CalendarEvent.COURSE_WITHDRAWAL,
        "2026/2027",
        Semester.FIRST,
    )
    pages = [
        GENERAL_HTML,
        GENERAL_HTML.replace("24/10/2026", "25/10/2026"),
    ]
    calls = []

    async def fetcher(url):
        calls.append(url)
        return pages[len(calls) - 1]

    first = asyncio.run(lookup_official_calendar(query, fetcher=fetcher, checked_at=CHECKED_AT))
    second = asyncio.run(lookup_official_calendar(query, fetcher=fetcher, checked_at=CHECKED_AT))

    assert len(calls) == 2
    assert first.records[0].start_date == date(2026, 10, 24)
    assert second.records[0].start_date == date(2026, 10, 25)


def test_arabic_and_english_variations_resolve_same_event_and_scope():
    arabic = resolve_calendar_query(
        "آخر يوم للانسحاب من المقررات في كلية الحقوق الفصل الأول ٢٠٢٦/٢٠٢٧",
        [],
        today=TODAY,
    )
    english = resolve_calendar_query(
        "College of Law course withdrawal deadline for fall semester 2026-2027",
        [],
        today=TODAY,
    )

    expected = CalendarQuery(
        CalendarEvent.COURSE_WITHDRAWAL,
        "2026/2027",
        Semester.FIRST,
        CollegeScope.LAW,
    )
    assert arabic == english == expected


def test_citation_requires_supporting_event_date_term_and_scope_match():
    law_query = CalendarQuery(
        CalendarEvent.COURSE_WITHDRAWAL,
        "2026/2027",
        Semester.FIRST,
        CollegeScope.LAW,
    )

    wrong_scope = verify_official_calendar_html(
        law_query,
        GENERAL_HTML,
        OFFICIAL_SOURCES[CollegeScope.GENERAL],
        checked_at=CHECKED_AT,
    )
    right_scope = _verified(law_query, LAW_HTML)

    assert wrong_scope.status == "unsupported_scope"
    assert right_scope.status == "verified"
    assert "اخر يوم للانسحاب من المقررات" in right_scope.records[0].supporting_excerpt


def test_unknown_explicit_college_never_falls_back_to_general_calendar():
    query = resolve_calendar_query(
        "متى آخر يوم سحب مواد في كلية العلوم للفصل الأول 2026/2027؟",
        [],
        today=TODAY,
    )

    assert query.college_scope is CollegeScope.UNSUPPORTED
    evidence = asyncio.run(
        lookup_official_calendar(query, fetcher=lambda _url: None, checked_at=CHECKED_AT)
    )
    assert evidence.status == "unsupported_scope"
    assert evidence.records == ()


def test_my_college_scope_can_be_resolved_from_authenticated_schedule_context():
    query = resolve_calendar_query(
        "متى آخر يوم سحب مواد في كليتي للفصل الأول 2026/2027؟",
        [],
        today=TODAY,
        schedule_college="كلية الحقوق",
    )

    assert query.college_scope is CollegeScope.LAW


def test_common_official_date_events_are_detected_before_lookup():
    cases = {
        "متى بدء الدراسة للفصل الأول 2026/2027؟": CalendarEvent.START_OF_CLASSES,
        "متى الامتحانات النهائية للفصل الأول 2026/2027؟": CalendarEvent.FINAL_EXAMS,
        "ما آخر يوم للتسجيل للفصل الأول 2026/2027؟": CalendarEvent.REGISTRATION,
        "When is the grade submission deadline for fall semester 2026-2027?": CalendarEvent.GRADE_SUBMISSION,
    }

    for question, event in cases.items():
        assert resolve_calendar_query(question, [], today=TODAY).event is event
