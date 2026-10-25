import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.academic_calendar import (
    OFFICIAL_CALENDAR_INDEX_URL, CalendarEvent, CalendarQuery, CollegeScope,
    OfficialCalendarSource, Semester, clear_calendar_content_cache,
    discover_special_calendar_sources, format_calendar_answer,
    lookup_official_calendar, resolve_calendar_query, verify_official_calendar_html,
)
from app.services.chat_service import process_chat

CONFIG = SimpleNamespace(HISTORY_TOKEN_BUDGET=3000, OPENAI_VECTOR_STORE_ID="vs-test", OPENAI_MODEL="test-model")
TODAY = date(2026, 10, 24)
CHECKED_AT = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
LAW_URL = "https://do.ku.edu.kw/ar/lawcalender"
ALLIED_URL = "https://do.ku.edu.kw/ar/college-allied-health-sciencescalender"
DENTISTRY_URL = "https://do.ku.edu.kw/ar/college-dentistery-calender"
MEDICINE_URL = "https://do.ku.edu.kw/ar/college-medicinecalender"
PHARMACY_URL = "https://do.ku.edu.kw/ar/pharmacycalender"

INDEX_HTML = f"""
<nav><a href="{LAW_URL}">تقويم كلية الحقوق</a>
<a href="{ALLIED_URL}">تقويم كلية العلوم الطبية المساعدة</a>
<a href="{DENTISTRY_URL}">تقويم كلية طب الأسنان</a>
<a href="{MEDICINE_URL}">تقويم كلية الطب</a>
<a href="{PHARMACY_URL}">تقويم كلية الصيدلة</a>
<a href="https://example.com/fake-calendar">تقويم كلية مزيف</a></nav>
<h2>الفصل الدراسي الأول 2027/2026</h2>
<div>آخر يوم لوقف القيد</div><div>22/10/2026</div>
<div>آخر يوم للانسحاب من المقررات</div><div>24/10/2026</div>
<div>آخر يوم لتقديم التماسات الانسحاب الكلي</div><div>26/11/2026</div>
<h2>الفصل الدراسي الثاني 2027/2026</h2>
<div>آخر يوم للانسحاب من المقررات</div><div>13/3/2027</div>
"""
LAW_HTML = """<h2>2026 - 2027</h2><h3>الفصل الدراسي الأول</h3>
<p>2026/10/22 آخر يوم للانسحاب من المقررات</p>
<h3>الفصل الدراسي الثاني</h3><p>2027/03/11 آخر يوم للانسحاب من المقررات</p>"""
ALLIED_HTML = """<h2>2026/2027</h2><h3>الفصل الدراسي الأول</h3>
<p>2026/10/23 آخر يوم للانسحاب من المقررات</p>"""
DENTISTRY_HTML = """<h2>2026/2027</h2><h3>الفصل الدراسي الأول</h3><p>لا يوجد تاريخ منشور لهذا الحدث</p>"""
MEDICINE_HTML = """<h2>2026/2027</h2><h3>الفصل الدراسي الأول</h3>
<p>2026/10/21 آخر يوم للانسحاب من المقررات</p>"""
PHARMACY_HTML = """<h2>2026/2027</h2><h3>الفصل الدراسي الأول</h3>
<p>2026/10/22 آخر يوم للانسحاب من المقررات</p>"""


@pytest.fixture(autouse=True)
def _empty_cache():
    clear_calendar_content_cache()
    yield
    clear_calendar_content_cache()


def _fetcher(*, dentistry_error=False, pages=None):
    content = {OFFICIAL_CALENDAR_INDEX_URL: INDEX_HTML, LAW_URL: LAW_HTML,
               ALLIED_URL: ALLIED_HTML, DENTISTRY_URL: DENTISTRY_HTML,
               MEDICINE_URL: MEDICINE_HTML, PHARMACY_URL: PHARMACY_HTML,
               **(pages or {})}

    async def fetch(url):
        if dentistry_error and url == DENTISTRY_URL:
            raise TimeoutError("mock timeout")
        return content[url]
    return fetch


def _lookup(question, *, fetcher=None, history=None):
    query = resolve_calendar_query(question, history or [], today=TODAY)
    assert query is not None
    result = asyncio.run(lookup_official_calendar(
        query, fetcher=fetcher or _fetcher(), checked_at=CHECKED_AT
    ))
    return query, result


def _payload(event):
    return json.loads(event[6:].strip())


def _run_chat(message, history=None):
    async def load_messages(*_args): return history or []
    async def load_schedules(*_args): return []
    async def official_lookup(query):
        return await lookup_official_calendar(query, fetcher=_fetcher(), checked_at=CHECKED_AT)
    async def unexpected_openai(*_args, **_kwargs):
        raise AssertionError("OpenAI must not generate official calendar dates")
        yield
    def discard_task(coroutine): coroutine.close()
    async def run(): return [event async for event in process_chat("uid", "session", message)]

    with (patch("app.services.chat_service.get_config", return_value=CONFIG),
          patch("app.services.chat_service.load_recent_messages", load_messages),
          patch("app.services.chat_service.load_user_schedules", load_schedules),
          patch("app.services.chat_service.lookup_official_calendar", official_lookup),
          patch("app.services.chat_service.stream_response", unexpected_openai),
          patch("app.services.chat_service.background_tasks.create_task", side_effect=discard_task),
          patch("app.services.academic_calendar.kuwait_today", return_value=TODAY)):
        return asyncio.run(run())


def test_no_college_shows_general_and_verified_special_deadlines():
    events = _run_chat("متى آخر يوم سحب مواد للفصل الأول 2026/2027؟")
    answer = _payload(events[1])["content"]
    citations = _payload(events[2])["citations"]
    assert "24 أكتوبر 2026" in answer
    assert "كلية الحقوق" in answer and "22 أكتوبر 2026" in answer
    assert "كلية العلوم الطبية المساعدة" in answer and "23 أكتوبر 2026" in answer
    assert "طب الأسنان" in answer and "غير متحقق منه" in answer
    assert "كلية الطب" in answer and "21 أكتوبر 2026" in answer
    assert "كلية الصيدلة" in answer and "22 أكتوبر 2026" in answer
    assert "قد لا ينطبق الموعد العام" in answer
    assert {item["url"] for item in citations} == {
        OFFICIAL_CALENDAR_INDEX_URL, LAW_URL, ALLIED_URL, DENTISTRY_URL,
        MEDICINE_URL, PHARMACY_URL}


def test_law_clarification_highlights_own_deadline_and_general_difference():
    history = [{"isUser": True, "content": "متى آخر يوم سحب مواد للفصل الأول 2026/2027؟"}]
    answer = _payload(_run_chat("طب بكلية الحقوق؟", history)[1])["content"]
    assert answer.startswith("**كلية الحقوق: 22 أكتوبر 2026**")
    assert "التقويم الجامعي العام: **24 أكتوبر 2026**" in answer
    assert "لا يجوز استخدامه بدلاً" in answer
    assert DENTISTRY_URL not in answer


def test_engineering_shows_available_dates_and_student_affairs_warning():
    query, result = _lookup("متى آخر يوم سحب مواد في كلية الهندسة والبترول للفصل الأول 2026/2027؟")
    answer = format_calendar_answer(query, result)
    assert "ليست ضمن التقويمات الخاصة" in answer
    assert all(value in answer for value in ("24 أكتوبر 2026", "22 أكتوبر 2026", "23 أكتوبر 2026"))
    assert "مكتب شؤون الطلبة" in answer
    assert "الهندسة والبترول: 24" not in answer


def test_unavailable_special_is_explicitly_unverified_not_omitted():
    query, result = _lookup("متى آخر يوم سحب مواد للفصل الأول 2026/2027؟",
                            fetcher=_fetcher(dentistry_error=True))
    answer = format_calendar_answer(query, result)
    assert "طب الأسنان" in answer and "غير متحقق منه" in answer
    assert DENTISTRY_URL in answer


def test_comparison_never_mixes_event_year_or_semester():
    query, result = _lookup("متى آخر يوم سحب مواد للفصل الثاني 2026/2027؟")
    answer = format_calendar_answer(query, result)
    assert "13 مارس 2027" in answer and "11 مارس 2027" in answer
    assert all(value not in answer for value in ("24 أكتوبر", "22 أكتوبر", "23 أكتوبر"))

    add_drop, add_drop_result = _lookup("متى السحب والإضافة للفصل الأول 2026/2027؟")
    add_drop_answer = format_calendar_answer(add_drop, add_drop_result)
    assert add_drop.event is CalendarEvent.ADD_DROP
    assert "24 أكتوبر" not in add_drop_answer and "22 أكتوبر" not in add_drop_answer


def test_cache_refresh_invalidates_stale_comparison():
    query = CalendarQuery(CalendarEvent.COURSE_WITHDRAWAL, "2026/2027", Semester.FIRST)
    clock = [100.0]
    versions = [INDEX_HTML, INDEX_HTML.replace("24/10/2026", "25/10/2026")]
    calls = []
    async def fetch(url):
        calls.append(url)
        if url == OFFICIAL_CALENDAR_INDEX_URL:
            return versions[min(calls.count(url) - 1, 1)]
        return {LAW_URL: LAW_HTML, ALLIED_URL: ALLIED_HTML,
                DENTISTRY_URL: DENTISTRY_HTML, MEDICINE_URL: MEDICINE_HTML,
                PHARMACY_URL: PHARMACY_HTML}[url]

    first = asyncio.run(lookup_official_calendar(query, fetcher=fetch, monotonic=lambda: clock[0]))
    second = asyncio.run(lookup_official_calendar(query, fetcher=fetch, monotonic=lambda: clock[0]))
    clock[0] += 301
    third = asyncio.run(lookup_official_calendar(query, fetcher=fetch, monotonic=lambda: clock[0]))
    assert first.general.records[0].start_date == second.general.records[0].start_date == date(2026, 10, 24)
    assert third.general.records[0].start_date == date(2026, 10, 25)
    assert calls.count(OFFICIAL_CALENDAR_INDEX_URL) == 2


def test_discovery_is_official_only_bounded_and_special_fetches_are_concurrent():
    assert len(discover_special_calendar_sources(INDEX_HTML)) == 5
    calls = []
    active = 0
    max_active = 0
    async def delayed_fetch(url):
        nonlocal active, max_active
        calls.append(url)
        if url != OFFICIAL_CALENDAR_INDEX_URL:
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
        return await _fetcher()(url)
    _lookup("متى آخر يوم سحب مواد للفصل الأول 2026/2027؟", fetcher=delayed_fetch)
    assert len(calls) == 6
    assert max_active == 5


def test_missing_or_conflicting_general_evidence_fails_safely():
    conflict = INDEX_HTML.replace(
        "<div>آخر يوم للانسحاب من المقررات</div><div>24/10/2026</div>",
        "<div>آخر يوم للانسحاب من المقررات 24/10/2026</div>"
        "<div>آخر يوم للانسحاب من المقررات 25/10/2026</div>")
    query, result = _lookup("متى آخر يوم سحب مواد للفصل الأول 2026/2027؟",
                            fetcher=_fetcher(pages={OFFICIAL_CALENDAR_INDEX_URL: conflict}))
    answer = format_calendar_answer(query, result)
    assert result.general.status == "conflict"
    assert "التاريخ غير متحقق منه" in answer
    assert "25 أكتوبر" not in answer


def test_arabic_multiturn_context_and_explicit_override_are_preserved():
    history = [{"isUser": True, "content": "متى آخر يوم سحب مواد بكلية الحقوق للفصل الأول 2026/2027؟"}]
    inherited = resolve_calendar_query("أقصد هذه السنة", history, today=TODAY)
    overridden = resolve_calendar_query("لا، كلية طب الأسنان", history, today=TODAY)
    assert inherited.college_scope is CollegeScope.LAW and inherited.college_specified
    assert overridden.college_scope is CollegeScope.DENTISTRY
    assert overridden.event is CalendarEvent.COURSE_WITHDRAWAL
    assert overridden.academic_year == "2026/2027"


def test_exact_sequence_preserves_user_assistant_order_and_sse_contract():
    messages = []

    async def load_messages(*_args):
        return list(reversed(messages))

    async def load_schedules(*_args):
        return []

    async def save_user(uid, session_id, content, **_kwargs):
        del uid, session_id, _kwargs
        messages.append({"isUser": True, "content": content})

    async def save_assistant(uid, session_id, content, citations):
        del uid, session_id
        messages.append({"isUser": False, "content": content, "citations": citations})

    async def official_lookup(query):
        return await lookup_official_calendar(query, fetcher=_fetcher(), checked_at=CHECKED_AT)

    async def run(message):
        return [event async for event in process_chat("uid", "session", message)]

    with (patch("app.services.chat_service.get_config", return_value=CONFIG),
          patch("app.services.chat_service.load_recent_messages", load_messages),
          patch("app.services.chat_service.load_user_schedules", load_schedules),
          patch("app.services.chat_service.save_user_message", save_user),
          patch("app.services.chat_service.save_assistant_message", save_assistant),
          patch("app.services.chat_service.lookup_official_calendar", official_lookup),
          patch("app.services.academic_calendar.kuwait_today", return_value=TODAY)):
        first = asyncio.run(run("متى آخر يوم للانسحاب من المقررات للفصل الأول؟"))
        second = asyncio.run(run("انا كلية حقوق"))

    assert "24 أكتوبر 2026" in _payload(first[-2])["content"]
    assert _payload(second[-2])["content"].startswith("**كلية الحقوق: 22 أكتوبر 2026**")
    assert [_payload(event)["type"] for event in second] == ["status", "chunk", "done"]
    assert [message["isUser"] for message in messages] == [True, False, True, False]


def test_relative_year_and_semester_followups_retain_original_event():
    history = [
        {"isUser": False, "content": "إجابة سابقة"},
        {"isUser": True, "content": "متى آخر يوم سحب مواد؟"},
    ]
    assert "24 أكتوبر 2026" in _payload(_run_chat("أقصد السنة دي", history)[1])["content"]
    assert "24 أكتوبر 2026" in _payload(_run_chat("أقصد الفصل الأول الحالي", history)[1])["content"]


def test_explicit_future_year_never_reuses_older_evidence():
    query, result = _lookup("When is first semester course withdrawal in 2027?")
    answer = format_calendar_answer(query, result, english=True)
    assert query.academic_year == "2027/2028"
    assert "24 October 2026" not in answer


def test_arabic_and_english_variations_resolve_the_same_scope():
    arabic = resolve_calendar_query(
        "آخر يوم للانسحاب من المقررات في كلية الحقوق الفصل الأول ٢٠٢٦/٢٠٢٧", [], today=TODAY)
    english = resolve_calendar_query(
        "College of Law course withdrawal deadline for fall semester 2026-2027", [], today=TODAY)
    assert arabic == english
    assert arabic.college_scope is CollegeScope.LAW and arabic.college_specified


def test_verifier_rejects_a_calendar_from_the_wrong_college_scope():
    query = CalendarQuery(
        CalendarEvent.COURSE_WITHDRAWAL, "2026/2027", Semester.FIRST,
        CollegeScope.LAW, True)
    general_source = OfficialCalendarSource(
        CollegeScope.GENERAL, OFFICIAL_CALENDAR_INDEX_URL,
        "التقويم الجامعي", "General calendar")
    result = verify_official_calendar_html(query, INDEX_HTML, general_source, checked_at=CHECKED_AT)
    assert result.status == "unsupported_scope"


def test_unknown_explicit_college_is_not_assigned_the_general_deadline():
    query, result = _lookup("متى آخر يوم سحب مواد في كلية العلوم للفصل الأول 2026/2027؟")
    answer = format_calendar_answer(query, result)
    assert query.college_scope is CollegeScope.UNSUPPORTED
    assert "ليست ضمن التقويمات الخاصة" in answer
    assert "مكتب شؤون الطلبة" in answer
    assert "الكلية المحددة: 24" not in answer


def test_my_college_resolves_from_authenticated_schedule_context():
    query = resolve_calendar_query(
        "متى آخر يوم سحب مواد في كليتي للفصل الأول 2026/2027؟", [],
        today=TODAY, schedule_college="كلية الحقوق")
    assert query.college_scope is CollegeScope.LAW
    assert query.college_specified


def test_common_official_events_are_detected_before_lookup():
    cases = {
        "متى بدء الدراسة للفصل الأول 2026/2027؟": CalendarEvent.START_OF_CLASSES,
        "متى الامتحانات النهائية للفصل الأول 2026/2027؟": CalendarEvent.FINAL_EXAMS,
        "ما آخر يوم للتسجيل للفصل الأول 2026/2027؟": CalendarEvent.REGISTRATION,
        "When is grade submission for fall semester 2026-2027?": CalendarEvent.GRADE_SUBMISSION,
    }
    for question, event in cases.items():
        assert resolve_calendar_query(question, [], today=TODAY).event is event


def test_concurrent_cache_misses_are_coalesced_and_failures_are_not_cached():
    query = CalendarQuery(CalendarEvent.COURSE_WITHDRAWAL, "2026/2027", Semester.FIRST)
    calls = []

    async def fetch(url):
        calls.append(url)
        await asyncio.sleep(0.01)
        return await _fetcher()(url)

    async def run_successes():
        return await asyncio.gather(
            lookup_official_calendar(query, fetcher=fetch),
            lookup_official_calendar(query, fetcher=fetch),
        )

    asyncio.run(run_successes())
    assert calls.count(OFFICIAL_CALENDAR_INDEX_URL) == 1
    assert all(calls.count(url) == 1 for url in (
        LAW_URL, ALLIED_URL, DENTISTRY_URL, MEDICINE_URL, PHARMACY_URL))

    clear_calendar_content_cache()
    failure_calls = []

    async def fail(_url):
        failure_calls.append(_url)
        await asyncio.sleep(0.01)
        raise TimeoutError("upstream unavailable")

    async def run_failures():
        return await asyncio.gather(
            lookup_official_calendar(query, fetcher=fail),
            lookup_official_calendar(query, fetcher=fail),
        )

    first, second = asyncio.run(run_failures())
    assert failure_calls == [OFFICIAL_CALENDAR_INDEX_URL]
    assert first.index_status == second.index_status == "unavailable"
    asyncio.run(lookup_official_calendar(query, fetcher=fail))
    assert len(failure_calls) == 2  # a later request retries; the failure was not cached


def test_unavailable_index_never_claims_special_calendar_list_is_complete():
    async def fail(_url):
        raise TimeoutError("upstream unavailable")

    query = resolve_calendar_query(
        "متى سحب المواد في كلية الحقوق للفصل الأول 2026/2027؟", [], today=TODAY)
    result = asyncio.run(lookup_official_calendar(query, fetcher=fail))
    answer = format_calendar_answer(query, result)
    assert result.index_status == "unavailable"
    assert "تعذر تحديد ما إذا كانت كلية الحقوق لها تقويم منفصل" in answer
    assert "ليست ضمن التقويمات الخاصة" not in answer
    assert "لا يمكن إسناد موعد بأمان" in answer
