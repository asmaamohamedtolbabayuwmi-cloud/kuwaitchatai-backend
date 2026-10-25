"""Official-source verification for Kuwait University calendar questions.

The resolver determines event, academic year, semester, and college scope from
the conversation. Exact dates are extracted from a freshly fetched,
scope-appropriate Kuwait University page. No date is stored in application
code and a URL citation alone is never considered evidence.
"""

from __future__ import annotations

import asyncio
import re
import ssl
import time
import warnings
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.utils import CryptographyDeprecationWarning


try:
    KUWAIT_TZ = ZoneInfo("Asia/Kuwait")
except Exception:  # Windows images may omit the optional IANA tzdata package.
    KUWAIT_TZ = timezone(timedelta(hours=3), name="Asia/Kuwait")


KU_INTERMEDIATE_PATH = (
    Path(__file__).resolve().parents[1]
    / "certs"
    / "sectigo-public-server-authentication-ca-dv-r36.pem"
)
KU_INTERMEDIATE_SHA256 = (
    "8c54c334b66ba4e426772af4a3f9136c19a1aec729fdb28c535c07a5a4ef22e0"
)
KU_INTERMEDIATE_SUBJECT = (
    "CN=Sectigo Public Server Authentication CA DV R36,"
    "O=Sectigo Limited,C=GB"
)
KU_INTERMEDIATE_ISSUER = (
    "CN=Sectigo Public Server Authentication Root R46,"
    "O=Sectigo Limited,C=GB"
)


class CalendarEvent(str, Enum):
    COURSE_WITHDRAWAL = "course_withdrawal"
    ADD_DROP = "add_drop"
    ENROLLMENT_SUSPENSION = "enrollment_suspension"
    FULL_SEMESTER_WITHDRAWAL = "full_semester_withdrawal"
    START_OF_CLASSES = "start_of_classes"
    FINAL_EXAMS = "final_exams"
    REGISTRATION = "registration"
    TRANSFER = "transfer"
    GRADE_SUBMISSION = "grade_submission"
    LAST_DAY_OF_CLASSES = "last_day_of_classes"


class Semester(str, Enum):
    FIRST = "first"
    SECOND = "second"
    SUMMER = "summer"


class CollegeScope(str, Enum):
    GENERAL = "general"
    LAW = "law"
    ALLIED_HEALTH = "allied_health"
    DENTISTRY = "dentistry"
    MEDICINE = "medicine"
    PHARMACY = "pharmacy"
    ENGINEERING = "engineering"
    UNSUPPORTED = "unsupported_college"


@dataclass(frozen=True)
class OfficialCalendarSource:
    scope: CollegeScope
    url: str
    title_ar: str
    title_en: str


@dataclass(frozen=True)
class CalendarQuery:
    event: CalendarEvent
    academic_year: str
    semester: Semester
    college_scope: CollegeScope = CollegeScope.GENERAL
    college_specified: bool = False


@dataclass(frozen=True)
class VerifiedCalendarRecord:
    event: CalendarEvent
    academic_year: str
    semester: Semester
    college_scope: CollegeScope
    start_date: date
    end_date: date
    source_url: str
    source_title: str
    supporting_excerpt: str
    checked_at: datetime


@dataclass(frozen=True)
class EvidenceResolution:
    status: str  # verified | missing | conflict | unavailable | unsupported_scope
    records: tuple[VerifiedCalendarRecord, ...]
    source: OfficialCalendarSource | None
    checked_at: datetime
    detail: str | None = None


@dataclass(frozen=True)
class CalendarComparisonResolution:
    """Same-event/term evidence collected from the official calendar index."""

    general: EvidenceResolution
    special: tuple[EvidenceResolution, ...]
    discovered_special_scopes: tuple[CollegeScope, ...]
    checked_at: datetime
    index_status: str = "verified"
    detail: str | None = None

    @property
    def records(self) -> tuple[VerifiedCalendarRecord, ...]:
        return tuple(
            record
            for resolution in (self.general, *self.special)
            for record in resolution.records
        )

    @property
    def status(self) -> str:
        if self.general.status == "verified" or any(
            item.status == "verified" for item in self.special
        ):
            return "verified"
        if any(item.status == "conflict" for item in (self.general, *self.special)):
            return "conflict"
        return self.general.status

    @property
    def citation_sources(self) -> tuple[OfficialCalendarSource, ...]:
        seen: set[str] = set()
        sources: list[OfficialCalendarSource] = []
        for resolution in (self.general, *self.special):
            if resolution.source and resolution.source.url not in seen:
                seen.add(resolution.source.url)
                sources.append(resolution.source)
        return tuple(sources)


OFFICIAL_CALENDAR_INDEX_URL = "https://do.ku.edu.kw/ar/academic-calendar-0"

# Only the index URL is configured. Special-calendar URLs are deliberately
# discovered from that official page on every cache refresh.
OFFICIAL_SOURCES: dict[CollegeScope, OfficialCalendarSource] = {
    CollegeScope.GENERAL: OfficialCalendarSource(
        CollegeScope.GENERAL,
        OFFICIAL_CALENDAR_INDEX_URL,
        "التقويم الجامعي الرسمي لجامعة الكويت",
        "Kuwait University official academic calendar",
    ),
}

_SOURCE_TITLES = {
    CollegeScope.LAW: ("تقويم كلية الحقوق بجامعة الكويت", "Kuwait University College of Law calendar"),
    CollegeScope.ALLIED_HEALTH: (
        "تقويم كلية العلوم الطبية المساعدة بجامعة الكويت",
        "Kuwait University College of Allied Health Sciences calendar",
    ),
    CollegeScope.DENTISTRY: (
        "تقويم كلية طب الأسنان بجامعة الكويت",
        "Kuwait University Faculty of Dentistry calendar",
    ),
    CollegeScope.MEDICINE: (
        "تقويم كلية الطب بجامعة الكويت",
        "Kuwait University Faculty of Medicine calendar",
    ),
    CollegeScope.PHARMACY: (
        "تقويم كلية الصيدلة بجامعة الكويت",
        "Kuwait University Faculty of Pharmacy calendar",
    ),
    CollegeScope.ENGINEERING: (
        "تقويم كلية الهندسة والبترول بجامعة الكويت",
        "Kuwait University College of Engineering and Petroleum calendar",
    ),
}

CALENDAR_CACHE_TTL_SECONDS = 300.0
MAX_SPECIAL_CALENDARS = 8
_CONTENT_CACHE: dict[str, tuple[float, str]] = {}
_INFLIGHT_FETCHES: dict[tuple[int, str], asyncio.Task[str]] = {}


_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

_EVENT_PATTERNS: tuple[tuple[CalendarEvent, tuple[str, ...]], ...] = (
    (
        CalendarEvent.FULL_SEMESTER_WITHDRAWAL,
        (
            r"الانسحاب (?:الكلي|الكامل)",
            r"سحب (?:الفصل|الترم) (?:كله|كامل)",
            r"(?:complete|full)[ -]semester withdrawal",
            r"withdraw(?:al)? from the (?:whole|entire) semester",
        ),
    ),
    (
        CalendarEvent.ADD_DROP,
        (
            r"السحب\s*و?\s*(?:ال[اإ]ضافة|[اإ]ضافة)",
            r"حذف\s*و?\s*(?:ال[اإ]ضافة|[اإ]ضافة)",
            r"add\s*(?:/|and|-)?\s*drop",
        ),
    ),
    (
        CalendarEvent.ENROLLMENT_SUSPENSION,
        (
            r"وقف القيد",
            r"[اإ]يقاف القيد",
            r"enrollment suspension",
            r"stopping registration",
            r"suspend(?:ing)? enrollment",
        ),
    ),
    (
        CalendarEvent.COURSE_WITHDRAWAL,
        (
            r"(?:ال|لل)انسحاب من (?:المقررات|المواد)",
            r"سحب (?:ال)?(?:مقررات|مواد|مادة)",
            r"course withdrawal",
            r"withdraw(?:al)? (?:from )?(?:a )?(?:course|courses|class|classes)",
        ),
    ),
    (
        CalendarEvent.START_OF_CLASSES,
        (r"بدء الدراسة", r"بداية الدراسة", r"start of classes", r"classes begin"),
    ),
    (
        CalendarEvent.FINAL_EXAMS,
        (r"الامتحانات النهائية", r"الاختبارات النهائية", r"final exam"),
    ),
    (
        CalendarEvent.GRADE_SUBMISSION,
        (r"[اإ]دخال الدرجات", r"تسليم الدرجات", r"grade submission"),
    ),
    (
        CalendarEvent.LAST_DAY_OF_CLASSES,
        (r"[آا]خر يوم (?:في|ل) الدراسة", r"last day of classes"),
    ),
    (
        CalendarEvent.TRANSFER,
        (r"فترة التحويل", r"موعد التحويل", r"transfer period"),
    ),
    (
        CalendarEvent.REGISTRATION,
        (
            r"(?:فترة|موعد|مواعيد|اخر يوم).{0,20}(?:التسجيل|للتسجيل)",
            r"registration(?: period| date| deadline)?",
        ),
    ),
)

_SEMESTER_PATTERNS = {
    Semester.FIRST: (
        r"(?:الفصل|للفصل) (?:الدراسي )?(?:ال)?اول",
        r"الترم (?:ال)?اول",
        r"(?:first|fall) semester",
    ),
    Semester.SECOND: (
        r"(?:الفصل|للفصل) (?:الدراسي )?الثاني",
        r"الترم الثاني",
        r"(?:second|spring) semester",
    ),
    Semester.SUMMER: (
        r"(?:الفصل|للفصل) (?:الدراسي )?الصيفي",
        r"الترم الصيفي",
        r"summer (?:semester|term)",
    ),
}

_COLLEGE_PATTERNS = {
    CollegeScope.LAW: (
        r"كلية (?:ال)?حقوق",
        r"(?:college|faculty) of law",
        r"\blaw college\b",
        r"بالحقوق",
    ),
    CollegeScope.ALLIED_HEALTH: (
        r"كلية العلوم الطبية المساعدة",
        r"allied health sciences",
    ),
    CollegeScope.DENTISTRY: (r"كلية طب الاسنان", r"faculty of dentistry", r"dentistry college"),
    CollegeScope.MEDICINE: (r"كلية الطب", r"faculty of medicine", r"medicine college"),
    CollegeScope.PHARMACY: (r"كلية الصيدلة", r"faculty of pharmacy", r"pharmacy college"),
    CollegeScope.ENGINEERING: (
        r"كلية الهندسة(?: والبترول)?",
        r"college of engineering(?: and petroleum)?",
    ),
}

_FOLLOW_UP_MARKERS = (
    r"(?:هذه السنة|هذي السنة|هالسنة|السنة دي|السنة الحالية)",
    r"(?:هذا الفصل|هالفصل|الفصل الحالي|الترم الحالي)",
    r"(?:this|current) (?:year|semester|term)",
    r"(?:first|second|fall|spring|summer) semester",
    r"الفصل (?:الدراسي )?(?:الأول|الاول|الثاني|الصيفي)",
    r"\b20\d{2}(?:\s*[/\-]\s*20\d{2})?\b",
    r"(?:كلية|college|faculty|بالحقوق)",
    r"(?:بشكل عام|التقويم العام|general university)",
)

_DATE_RE = re.compile(
    r"(?<!\d)(?:(20\d{2})\s*[/\-]\s*(\d{1,2})\s*[/\-]\s*(\d{1,2})|"
    r"(\d{1,2})\s*[/\-]\s*(\d{1,2})\s*[/\-]\s*(20\d{2}))(?!\d)"
)
_ACADEMIC_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})\s*[/\-]\s*(20\d{2})(?!\d)")
_RANGE_EVENTS = {
    CalendarEvent.ADD_DROP,
    CalendarEvent.FINAL_EXAMS,
    CalendarEvent.REGISTRATION,
    CalendarEvent.TRANSFER,
}


def kuwait_now() -> datetime:
    return datetime.now(KUWAIT_TZ)


def kuwait_today() -> date:
    return kuwait_now().date()


def _normalize(text: str) -> str:
    text = text.translate(_ARABIC_DIGITS).casefold()
    text = text.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا"}))
    text = re.sub(r"[\u064b-\u065f\u0670]", "", text)
    return " ".join(text.split())


def _extract_event(text: str) -> CalendarEvent | None:
    for event, patterns in _EVENT_PATTERNS:
        if any(re.search(pattern, text) for pattern in patterns):
            return event
    return None


def _extract_semester(text: str) -> Semester | None:
    for semester, patterns in _SEMESTER_PATTERNS.items():
        if any(re.search(pattern, text) for pattern in patterns):
            return semester
    return None


def _extract_college(text: str) -> CollegeScope | None:
    if re.search(r"(?:بشكل عام|التقويم العام|جامعة الكويت بشكل عام|general university)", text):
        return CollegeScope.GENERAL
    for college, patterns in _COLLEGE_PATTERNS.items():
        if any(re.search(pattern, text) for pattern in patterns):
            return college
    if re.search(r"(?:كلية|college|faculty)", text):
        return CollegeScope.UNSUPPORTED
    return None


def _college_from_schedule(value: str | None) -> CollegeScope | None:
    return _extract_college(_normalize(value or ""))


def _current_academic_context(today: date) -> tuple[str, Semester]:
    if today.month >= 9:
        return f"{today.year}/{today.year + 1}", Semester.FIRST
    academic_year = f"{today.year - 1}/{today.year}"
    if today.month <= 5:
        return academic_year, Semester.SECOND
    return academic_year, Semester.SUMMER


def _extract_academic_year(text: str) -> str | None:
    pair = _ACADEMIC_YEAR_RE.search(text)
    if pair:
        first, second = int(pair.group(1)), int(pair.group(2))
        if abs(first - second) == 1:
            start = min(first, second)
            return f"{start}/{start + 1}"
        return None
    single = re.search(r"(?<![/\d])(20\d{2})(?![/\d])", text)
    if single:
        start = int(single.group(1))
        return f"{start}/{start + 1}"
    return None


def _has_relative_year(text: str) -> bool:
    return bool(re.search(
        r"(?:هذه السنة|هذي السنة|السنة دي|هالسنة|السنة الحالية|this year|current academic year)",
        text,
    ))


def resolve_calendar_query(
    current_message: str,
    prior_messages_desc: list[dict],
    *,
    today: date | None = None,
    schedule_college: str | None = None,
) -> CalendarQuery | None:
    """Resolve calendar intent using current text and prior user turns only."""
    today = today or kuwait_today()
    current = _normalize(current_message)
    event = _extract_event(current)
    semester = _extract_semester(current)
    academic_year = _extract_academic_year(current)
    college = _extract_college(current)
    college_specified = college is not None

    is_follow_up = any(re.search(pattern, current) for pattern in _FOLLOW_UP_MARKERS)
    if event is None and is_follow_up:
        for message in prior_messages_desc:
            if message.get("isUser", True):
                event = _extract_event(_normalize(str(message.get("content", ""))))
                if event is not None:
                    break
    if event is None:
        return None

    current_year, current_semester = _current_academic_context(today)
    if academic_year is None and _has_relative_year(current):
        academic_year = current_year

    if is_follow_up and (academic_year is None or semester is None or college is None):
        for message in prior_messages_desc:
            if not message.get("isUser", True):
                continue
            prior = _normalize(str(message.get("content", "")))
            if academic_year is None:
                academic_year = _extract_academic_year(prior)
            if semester is None:
                semester = _extract_semester(prior)
            if college is None:
                college = _extract_college(prior)
                if college is not None:
                    college_specified = True

    if college is None and re.search(r"(?:كليتي|my college)", current):
        college = _college_from_schedule(schedule_college)
        college_specified = college is not None

    return CalendarQuery(
        event=event,
        academic_year=academic_year or current_year,
        semester=semester or current_semester,
        college_scope=college or CollegeScope.GENERAL,
        college_specified=college_specified,
    )


class _VisibleTextParser(HTMLParser):
    _BLOCK_TAGS = {
        "br", "div", "p", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "section"
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.ignored_depth += 1
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        lines = (_normalize(line) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


def _visible_text(html: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(html)
    return parser.text()


class _CalendarLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "a":
            self._href = next((value for key, value in attrs if key == "href"), None)
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._parts)))
            self._href = None
            self._parts = []


def _official_ku_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return host == "ku.edu.kw" or host.endswith(".ku.edu.kw")


def _source_scope(link_text: str, url: str) -> CollegeScope | None:
    combined = _normalize(f"{link_text} {urlparse(url).path.replace('-', ' ')}")
    path = urlparse(url).path.casefold()
    path_aliases = {
        CollegeScope.LAW: ("lawcalendar", "lawcalender"),
        CollegeScope.ALLIED_HEALTH: ("allied-health", "allied_health"),
        CollegeScope.DENTISTRY: ("dentistry", "dentistery"),
        CollegeScope.MEDICINE: ("medicinecalender", "medicinecalendar"),
        CollegeScope.PHARMACY: ("pharmacycalender", "pharmacycalendar"),
        CollegeScope.ENGINEERING: ("engineering",),
    }
    for scope, aliases in path_aliases.items():
        if any(alias in path for alias in aliases):
            return scope
    for scope, patterns in _COLLEGE_PATTERNS.items():
        if any(re.search(pattern, combined) for pattern in patterns):
            return scope
    # Preserve a published but newly introduced college calendar as an
    # independently verified source even before its college alias is known.
    if re.search(r"(?:كلية|college|faculty)", combined):
        return CollegeScope.UNSUPPORTED
    return None


def discover_special_calendar_sources(index_html: str) -> tuple[OfficialCalendarSource, ...]:
    """Discover special calendars only from links published by the KU index."""
    parser = _CalendarLinkParser()
    parser.feed(index_html)
    discovered: list[OfficialCalendarSource] = []
    seen_urls: set[str] = set()
    for href, link_text in parser.links:
        url = urljoin(OFFICIAL_CALENDAR_INDEX_URL, href.strip())
        if url == OFFICIAL_CALENDAR_INDEX_URL or not _official_ku_url(url):
            continue
        if "/taxonomy/" in urlparse(url).path.casefold():
            continue
        normalized = _normalize(f"{link_text} {urlparse(url).path}")
        if not re.search(r"(?:calendar|calender|تقويم)", normalized):
            continue
        scope = _source_scope(link_text, url)
        if scope is None or url in seen_urls:
            continue
        seen_urls.add(url)
        titles = _SOURCE_TITLES.get(scope)
        clean_title = " ".join(link_text.split())
        title_ar = titles[0] if titles else clean_title or "تقويم كلية خاص بجامعة الكويت"
        title_en = titles[1] if titles else clean_title or "Kuwait University special college calendar"
        discovered.append(OfficialCalendarSource(scope, url, title_ar, title_en))
        if len(discovered) >= MAX_SPECIAL_CALENDARS:
            break
    return tuple(discovered)


def _year_from_match(match: re.Match[str]) -> str | None:
    first, second = int(match.group(1)), int(match.group(2))
    if abs(first - second) != 1:
        return None
    start = min(first, second)
    return f"{start}/{start + 1}"


def _date_from_match(match: re.Match[str]) -> date | None:
    try:
        if match.group(1):
            year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        else:
            day, month, year = int(match.group(4)), int(match.group(5)), int(match.group(6))
        return date(year, month, day)
    except ValueError:
        return None


def _date_belongs_to_term(value: date, query: CalendarQuery) -> bool:
    start_year = int(query.academic_year.split("/")[0])
    if not date(start_year, 9, 1) <= value <= date(start_year + 1, 8, 31):
        return False
    if query.semester is Semester.FIRST:
        return date(start_year, 9, 1) <= value <= date(start_year + 1, 1, 31)
    if query.semester is Semester.SECOND:
        return date(start_year + 1, 1, 1) <= value <= date(start_year + 1, 5, 31)
    return date(start_year + 1, 6, 1) <= value <= date(start_year + 1, 8, 31)


def _nearest_preceding_context(text: str, position: int) -> tuple[str | None, Semester | None]:
    year: str | None = None
    for match in _ACADEMIC_YEAR_RE.finditer(text, 0, position):
        normalized = _year_from_match(match)
        if normalized:
            year = normalized

    semester: Semester | None = None
    semester_position = -1
    prefix = text[:position]
    for candidate, patterns in _SEMESTER_PATTERNS.items():
        for pattern in patterns:
            matches = list(re.finditer(pattern, prefix))
            if matches and matches[-1].start() > semester_position:
                semester = candidate
                semester_position = matches[-1].start()
    return year, semester


def _event_matches(text: str, query: CalendarQuery) -> list[re.Match[str]]:
    patterns = dict(_EVENT_PATTERNS)[query.event]
    matches: list[re.Match[str]] = []
    for pattern in patterns:
        matches.extend(re.finditer(pattern, text))
    return sorted(matches, key=lambda item: item.start())


def verify_official_calendar_html(
    query: CalendarQuery,
    html: str,
    source: OfficialCalendarSource,
    *,
    checked_at: datetime | None = None,
) -> EvidenceResolution:
    """Verify exact event/term/scope matches against fetched page content."""
    checked_at = checked_at or kuwait_now()
    if source.scope is not query.college_scope:
        return EvidenceResolution(
            "unsupported_scope", (), source, checked_at, "source scope does not match query"
        )

    text = _visible_text(html)
    records: list[VerifiedCalendarRecord] = []
    for event_match in _event_matches(text, query):
        year, semester = _nearest_preceding_context(text, event_match.start())
        if year != query.academic_year or semester is not query.semester:
            continue

        window_start = max(0, event_match.start() - 140)
        window_end = min(len(text), event_match.end() + 140)
        window = text[window_start:window_end]
        candidates: list[tuple[int, int, date]] = []
        for date_match in _DATE_RE.finditer(window):
            parsed = _date_from_match(date_match)
            if parsed is None or not _date_belongs_to_term(parsed, query):
                continue
            absolute_start = window_start + date_match.start()
            distance = min(abs(absolute_start - event_match.start()), abs(absolute_start - event_match.end()))
            candidates.append((distance, absolute_start, parsed))
        if not candidates:
            continue

        if source.scope is CollegeScope.GENERAL:
            preferred = [item for item in candidates if item[1] >= event_match.end()]
        else:
            preferred = [item for item in candidates if item[1] <= event_match.start()]
        candidates = preferred or candidates
        candidates.sort(key=lambda item: item[0])
        closest_distance = candidates[0][0]
        selected = [candidates[0][2]]
        if query.event in _RANGE_EVENTS:
            selected = [
                value
                for distance, _position, value in candidates
                if distance <= closest_distance + 45
            ][:2]
        records.append(VerifiedCalendarRecord(
            query.event,
            query.academic_year,
            query.semester,
            query.college_scope,
            min(selected),
            max(selected),
            source.url,
            source.title_ar,
            " ".join(window.split()),
            checked_at,
        ))

    if not records:
        return EvidenceResolution(
            "missing", (), source, checked_at, "no exact event/term/scope match on official page"
        )
    unique_ranges = {(record.start_date, record.end_date) for record in records}
    if len(unique_ranges) != 1:
        return EvidenceResolution("conflict", tuple(records), source, checked_at)
    return EvidenceResolution("verified", tuple(records), source, checked_at)


def _validated_ku_ssl_context(
    *,
    intermediate_path: Path = KU_INTERMEDIATE_PATH,
    now: datetime | None = None,
    base_context: ssl.SSLContext | None = None,
) -> ssl.SSLContext:
    """Add the packaged intermediate to normal roots after strict validation."""
    context = base_context or ssl.create_default_context()
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise ValueError("KU TLS context must require certificate and hostname verification")

    pem = intermediate_path.read_bytes()
    certificate = x509.load_pem_x509_certificate(pem)
    fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
    if fingerprint != KU_INTERMEDIATE_SHA256:
        raise ValueError("KU TLS intermediate fingerprint mismatch")
    if certificate.subject.rfc4514_string() != KU_INTERMEDIATE_SUBJECT:
        raise ValueError("KU TLS intermediate subject mismatch")
    if certificate.issuer.rfc4514_string() != KU_INTERMEDIATE_ISSUER:
        raise ValueError("KU TLS intermediate issuer mismatch")

    constraints = certificate.extensions.get_extension_for_class(
        x509.BasicConstraints
    ).value
    if not constraints.ca:
        raise ValueError("KU TLS intermediate is not a CA certificate")

    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    if not certificate.not_valid_before_utc <= checked_at <= certificate.not_valid_after_utc:
        raise ValueError("KU TLS intermediate is outside its validity window")

    issuer_roots: list[x509.Certificate] = []
    for item in context.get_ca_certs(binary_form=True):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", CryptographyDeprecationWarning)
                root = x509.load_der_x509_certificate(item)
        except ValueError:
            continue
        if root.subject == certificate.issuer:
            issuer_roots.append(root)
    if not issuer_roots:
        raise ValueError("KU TLS intermediate issuer is not in the normal trust store")
    if not any(_is_directly_issued_by(certificate, root) for root in issuer_roots):
        raise ValueError("KU TLS intermediate signature is not valid under a trusted root")

    # This augments this one context only. It does not modify system roots or
    # any OpenAI, Firebase, notification, or other HTTP client.
    context.load_verify_locations(cafile=str(intermediate_path))
    return context


def _is_directly_issued_by(certificate: x509.Certificate, root: x509.Certificate) -> bool:
    try:
        certificate.verify_directly_issued_by(root)
        return True
    except (ValueError, TypeError):
        return False


def _is_certificate_verification_error(exc: BaseException) -> bool:
    """Recognize only TLS certificate-verification failures for scoped retry."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(current).upper():
            return True
        current = current.__cause__ or current.__context__
    return False


async def _request_official_html(url: str, verify: bool | ssl.SSLContext) -> str:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(15.0),
        follow_redirects=True,
        headers={"User-Agent": "KunivAI-CalendarVerifier/1.0"},
        verify=verify,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        final_host = (urlparse(str(response.url)).hostname or "").casefold()
        if final_host != "ku.edu.kw" and not final_host.endswith(".ku.edu.kw"):
            raise ValueError("official calendar redirected outside ku.edu.kw")
        return response.text


async def _fetch_html(url: str) -> str:
    """Fetch with normal TLS first, then retry only the known missing-chain case."""
    try:
        return await _request_official_html(url, True)
    except httpx.TransportError as exc:
        if not _is_certificate_verification_error(exc):
            raise
    scoped_context = _validated_ku_ssl_context()
    return await _request_official_html(url, scoped_context)


def clear_calendar_content_cache() -> None:
    """Test/operations hook; cached values are official response bodies only."""
    _CONTENT_CACHE.clear()


async def _cached_official_html(
    url: str,
    fetcher: Callable[[str], Awaitable[str]],
    *,
    ttl_seconds: float,
    now: Callable[[], float],
) -> str:
    current = now()
    cached = _CONTENT_CACHE.get(url)
    if cached and current - cached[0] < ttl_seconds:
        return cached[1]

    loop = asyncio.get_running_loop()
    key = (id(loop), url)
    task = _INFLIGHT_FETCHES.get(key)
    if task is None:
        async def fetch_and_cache() -> str:
            html = await fetcher(url)
            _CONTENT_CACHE[url] = (now(), html)
            return html

        task = loop.create_task(fetch_and_cache())
        _INFLIGHT_FETCHES[key] = task

        def discard(completed: asyncio.Task[str]) -> None:
            if _INFLIGHT_FETCHES.get(key) is completed:
                _INFLIGHT_FETCHES.pop(key, None)

        task.add_done_callback(discard)

    # One failed upstream request is shared by concurrent callers but is never
    # inserted into the content cache, preventing both stale approval and a
    # same-moment retry stampede.
    return await asyncio.shield(task)


async def lookup_official_calendar(
    query: CalendarQuery,
    *,
    fetcher: Callable[[str], Awaitable[str]] | None = None,
    checked_at: datetime | None = None,
    cache_ttl_seconds: float = CALENDAR_CACHE_TTL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> CalendarComparisonResolution:
    """Compare same-event/term evidence from the official index and its links."""
    checked_at = checked_at or kuwait_now()
    fetch = fetcher or _fetch_html
    general_source = OFFICIAL_SOURCES[CollegeScope.GENERAL]
    try:
        index_html = await _cached_official_html(
            OFFICIAL_CALENDAR_INDEX_URL,
            fetch,
            ttl_seconds=cache_ttl_seconds,
            now=monotonic,
        )
    except Exception as exc:
        unavailable = EvidenceResolution(
            "unavailable",
            (),
            general_source,
            checked_at,
            f"official index fetch failed: {type(exc).__name__}",
        )
        return CalendarComparisonResolution(
            unavailable,
            (),
            (),
            checked_at,
            index_status="unavailable",
            detail=unavailable.detail,
        )

    general_query = CalendarQuery(
        query.event, query.academic_year, query.semester, CollegeScope.GENERAL
    )
    general = verify_official_calendar_html(
        general_query, index_html, general_source, checked_at=checked_at
    )
    discovered = discover_special_calendar_sources(index_html)
    requested_source = (
        next((source for source in discovered if source.scope is query.college_scope), None)
        if query.college_scope is not CollegeScope.UNSUPPORTED
        else None
    )
    if not query.college_specified:
        selected = discovered
    elif query.college_scope is CollegeScope.GENERAL:
        selected = ()
    elif requested_source is not None:
        selected = (requested_source,)
    else:
        # For a college not identified by the index, show the available
        # special-calendar landscape rather than assuming the general date.
        selected = discovered

    async def verify_source(source: OfficialCalendarSource) -> EvidenceResolution:
        source_query = CalendarQuery(
            query.event, query.academic_year, query.semester, source.scope, True
        )
        try:
            html = await _cached_official_html(
                source.url,
                fetch,
                ttl_seconds=cache_ttl_seconds,
                now=monotonic,
            )
        except Exception as exc:
            return EvidenceResolution(
                "unavailable",
                (),
                source,
                checked_at,
                f"official source fetch failed: {type(exc).__name__}",
            )
        return verify_official_calendar_html(
            source_query, html, source, checked_at=checked_at
        )

    special = tuple(await asyncio.gather(*(verify_source(source) for source in selected)))
    return CalendarComparisonResolution(
        general,
        special,
        tuple(source.scope for source in discovered),
        checked_at,
    )


def calendar_prompt_context(
    query: CalendarQuery,
    resolution: EvidenceResolution | CalendarComparisonResolution | None = None,
) -> str:
    lines = [
        "<academic_calendar_request>",
        f"event={query.event.value}",
        f"academic_year={query.academic_year}",
        f"semester={query.semester.value}",
        f"college_scope={query.college_scope.value}",
    ]
    if resolution is not None:
        lines.append(f"official_verification_status={resolution.status}")
    lines.append("</academic_calendar_request>")
    return "\n".join(lines)


def format_calendar_answer(
    query: CalendarQuery,
    resolution: EvidenceResolution | CalendarComparisonResolution,
    *,
    english: bool = False,
) -> str:
    if isinstance(resolution, CalendarComparisonResolution):
        return _format_comparison_answer(query, resolution, english=english)
    return _format_single_calendar_answer(query, resolution, english=english)


def _scope_name(scope: CollegeScope, *, english: bool) -> str:
    names = {
        CollegeScope.GENERAL: ("جامعة الكويت (التقويم العام)", "Kuwait University (general calendar)"),
        CollegeScope.LAW: ("كلية الحقوق", "College of Law"),
        CollegeScope.ALLIED_HEALTH: ("كلية العلوم الطبية المساعدة", "College of Allied Health Sciences"),
        CollegeScope.DENTISTRY: ("كلية طب الأسنان", "Faculty of Dentistry"),
        CollegeScope.MEDICINE: ("كلية الطب", "Faculty of Medicine"),
        CollegeScope.PHARMACY: ("كلية الصيدلة", "Faculty of Pharmacy"),
        CollegeScope.ENGINEERING: ("كلية الهندسة والبترول", "College of Engineering and Petroleum"),
        CollegeScope.UNSUPPORTED: ("الكلية المحددة", "the specified college"),
    }
    return names[scope][1 if english else 0]


def _record_date(record: VerifiedCalendarRecord, *, english: bool) -> str:
    if record.start_date == record.end_date:
        if english:
            return record.start_date.strftime("%d %B %Y").lstrip("0")
        months = [
            "", "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
            "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر",
        ]
        return f"{record.start_date.day} {months[record.start_date.month]} {record.start_date.year}"
    if english:
        start = record.start_date.strftime("%d %B %Y").lstrip("0")
        end = record.end_date.strftime("%d %B %Y").lstrip("0")
        return f"{start} to {end}"
    return (
        f"{record.start_date.day}/{record.start_date.month}/{record.start_date.year}"
        f" إلى {record.end_date.day}/{record.end_date.month}/{record.end_date.year}"
    )


def _source_link(source: OfficialCalendarSource, *, english: bool) -> str:
    return f"[{source.title_en if english else source.title_ar}]({source.url})"


def _verified_record(resolution: EvidenceResolution) -> VerifiedCalendarRecord | None:
    return resolution.records[0] if resolution.status == "verified" and resolution.records else None


def _format_comparison_answer(
    query: CalendarQuery,
    resolution: CalendarComparisonResolution,
    *,
    english: bool,
) -> str:
    general_record = _verified_record(resolution.general)
    requested = (
        next(
            (
                item for item in resolution.special
                if item.source and item.source.scope is query.college_scope
            ),
            None,
        )
        if query.college_scope is not CollegeScope.UNSUPPORTED
        else None
    )
    requested_published = (
        query.college_scope is not CollegeScope.UNSUPPORTED
        and query.college_scope in resolution.discovered_special_scopes
    )
    unsupported_request = (
        query.college_specified
        and query.college_scope is not CollegeScope.GENERAL
        and not requested_published
        and resolution.index_status == "verified"
    )
    discovery_incomplete = (
        query.college_specified
        and query.college_scope is not CollegeScope.GENERAL
        and resolution.index_status != "verified"
    )
    lines: list[str] = []

    if requested is not None:
        record = _verified_record(requested)
        source = requested.source
        if record and source:
            lines.append(
                (f"**{_scope_name(query.college_scope, english=True)}: {_record_date(record, english=True)}** "
                 f"— {_source_link(source, english=True)}")
                if english else
                (f"**{_scope_name(query.college_scope, english=False)}: {_record_date(record, english=False)}** "
                 f"— {_source_link(source, english=False)}")
            )
        elif source:
            lines.append(
                (f"**{_scope_name(query.college_scope, english=True)}: date unverified.** "
                 f"The published calendar could not provide a single matching date for this event, year, and semester: {_source_link(source, english=True)}")
                if english else
                (f"**{_scope_name(query.college_scope, english=False)}: التاريخ غير متحقق منه.** "
                 f"تعذر استخراج تاريخ واحد مطابق للحدث والسنة والفصل من التقويم المنشور: {_source_link(source, english=False)}")
            )
    elif unsupported_request:
        lines.append(
            (f"**{_scope_name(query.college_scope, english=True)} is not among the separate special calendars currently identified on the official KU index.**")
            if english else
            (f"**{_scope_name(query.college_scope, english=False)} ليست ضمن التقويمات الخاصة المنفصلة المحددة حالياً في فهرس جامعة الكويت الرسمي.**")
        )
    elif discovery_incomplete:
        lines.append(
            (f"**I couldn't determine whether {_scope_name(query.college_scope, english=True)} has a separate calendar because the official KU index is unavailable.**")
            if english else
            (f"**تعذر تحديد ما إذا كانت {_scope_name(query.college_scope, english=False)} لها تقويم منفصل لأن فهرس جامعة الكويت الرسمي غير متاح.**")
        )

    if general_record and resolution.general.source:
        prefix = "General university calendar" if english else "التقويم الجامعي العام"
        lines.append(
            f"- {prefix}: **{_record_date(general_record, english=english)}** — "
            f"{_source_link(resolution.general.source, english=english)}"
        )
    elif resolution.general.source:
        lines.append(
            (f"- General university calendar: **date unverified** for this exact event/year/semester — {_source_link(resolution.general.source, english=True)}")
            if english else
            (f"- التقويم الجامعي العام: **التاريخ غير متحقق منه** لهذا الحدث/السنة/الفصل تحديداً — {_source_link(resolution.general.source, english=False)}")
        )

    # Group only truly comparable records: same event, academic year and term
    # have already been enforced by the evidence verifier.
    groups: dict[tuple[date, date], list[EvidenceResolution]] = {}
    same_as_general: list[EvidenceResolution] = []
    for item in resolution.special:
        record = _verified_record(item)
        if record is None or item is requested:
            continue
        if general_record and (record.start_date, record.end_date) == (
            general_record.start_date, general_record.end_date
        ):
            same_as_general.append(item)
        else:
            groups.setdefault((record.start_date, record.end_date), []).append(item)

    for items in groups.values():
        record = _verified_record(items[0])
        assert record is not None
        names = ", ".join(
            _scope_name(item.source.scope, english=english)
            if item.source and item.source.scope is not CollegeScope.UNSUPPORTED
            else (item.source.title_en if english else item.source.title_ar)
            for item in items
            if item.source
        )
        links = ", ".join(
            _source_link(item.source, english=english) for item in items if item.source
        )
        lines.append(f"- {names}: **{_record_date(record, english=english)}** — {links}")

    if same_as_general:
        names = ", ".join(
            _scope_name(item.source.scope, english=english)
            for item in same_as_general if item.source
        )
        links = ", ".join(
            _source_link(item.source, english=english)
            for item in same_as_general if item.source
        )
        lines.append(
            (f"- Verified with the same date as the general calendar: {names} — {links}.")
            if english else
            (f"- تم التحقق من أنها تحمل التاريخ العام نفسه: {names} — {links}.")
        )

    unverified = [
        item for item in resolution.special
        if item.status != "verified" and item is not requested and item.source
    ]
    if unverified:
        entries = ", ".join(
            f"{_scope_name(item.source.scope, english=english)} ({_source_link(item.source, english=english)})"
            if item.source.scope is not CollegeScope.UNSUPPORTED else _source_link(item.source, english=english)
            for item in unverified
        )
        lines.append(
            (f"Coverage limitation: the matching date is **unverified** for {entries}; no date is inferred.")
            if english else
            (f"حدود التغطية: التاريخ المطابق **غير متحقق منه** لـ {entries}؛ ولم يتم استنتاج أي تاريخ.")
        )

    if query.college_specified and query.college_scope is not CollegeScope.GENERAL:
        if unsupported_request:
            lines.append(
                "Do not assume the general date applies; confirm the applicable deadline with the college's Student Affairs Office."
                if english else
                "لا تفترض أن الموعد العام ينطبق على الكلية؛ أكّد الموعد المطبق مع مكتب شؤون الطلبة في الكلية."
            )
        elif discovery_incomplete:
            lines.append(
                "No deadline can be assigned safely; confirm it with the college's Student Affairs Office."
                if english else
                "لا يمكن إسناد موعد بأمان؛ أكّده مع مكتب شؤون الطلبة في الكلية."
            )
        elif requested is not None and general_record:
            requested_record = _verified_record(requested)
            if requested_record is None:
                lines.append(
                    "Do not use the general date as a substitute; confirm the college deadline with its Student Affairs Office."
                    if english else
                    "لا تستخدم الموعد العام بديلاً؛ أكّد موعد الكلية مع مكتب شؤون الطلبة فيها."
                )
            elif (requested_record.start_date, requested_record.end_date) != (
                general_record.start_date, general_record.end_date
            ):
                lines.append(
                    "The general date is different and must not replace the college-specific deadline."
                    if english else
                    "الموعد العام مختلف ولا يجوز استخدامه بدلاً من موعد الكلية الخاص."
                )
    else:
        lines.append(
            "The general date may not apply to colleges with special calendars."
            if english else
            "قد لا ينطبق الموعد العام على الكليات ذات التقويمات الخاصة."
        )

    if resolution.index_status != "verified":
        lines.append(
            "The official calendar index was unavailable, so special-calendar coverage could not be verified."
            if english else
            "تعذر الوصول إلى فهرس التقويم الرسمي، لذلك لم يمكن التحقق من تغطية التقويمات الخاصة."
        )
    return "\n\n".join(lines)


def _format_single_calendar_answer(
    query: CalendarQuery,
    resolution: EvidenceResolution,
    *,
    english: bool,
) -> str:
    if resolution.status != "verified":
        scope_ar = "جامعة الكويت" if query.college_scope is CollegeScope.GENERAL else "الكلية المحددة"
        if resolution.status == "conflict":
            return (
                "The applicable official calendar contains conflicting matches, so I can't confirm an exact date."
                if english else
                "وجدت نتائج متعارضة في التقويم الرسمي المطبق، لذلك لا أستطيع تأكيد تاريخ محدد."
            )
        return (
            "I couldn't verify an exact date for this event, term, and college from the applicable official Kuwait University calendar, so I won't guess."
            if english else
            f"لم أتمكن من التحقق من تاريخ محدد لهذا الحدث والفصل ونطاق {scope_ar} من التقويم الرسمي المطبق، لذلك لن أخمّن."
        )

    record = resolution.records[0]
    event_ar = {
        CalendarEvent.COURSE_WITHDRAWAL: "الانسحاب من المقررات",
        CalendarEvent.ADD_DROP: "السحب والإضافة",
        CalendarEvent.ENROLLMENT_SUSPENSION: "وقف القيد",
        CalendarEvent.FULL_SEMESTER_WITHDRAWAL: "الانسحاب الكلي من الفصل",
        CalendarEvent.START_OF_CLASSES: "بدء الدراسة",
        CalendarEvent.FINAL_EXAMS: "الامتحانات النهائية",
        CalendarEvent.REGISTRATION: "التسجيل",
        CalendarEvent.TRANSFER: "التحويل",
        CalendarEvent.GRADE_SUBMISSION: "إدخال الدرجات",
        CalendarEvent.LAST_DAY_OF_CLASSES: "آخر يوم في الدراسة",
    }[query.event]
    event_en = query.event.value.replace("_", " ")
    semester_ar = {
        Semester.FIRST: "الفصل الدراسي الأول",
        Semester.SECOND: "الفصل الدراسي الثاني",
        Semester.SUMMER: "الفصل الصيفي",
    }[query.semester]
    semester_en = {
        Semester.FIRST: "first semester",
        Semester.SECOND: "second semester",
        Semester.SUMMER: "summer semester",
    }[query.semester]
    college_ar = _scope_name(query.college_scope, english=False)
    college_en = _scope_name(query.college_scope, english=True)

    if record.start_date == record.end_date:
        months_ar = ["", "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]
        date_ar = f"{record.start_date.day} {months_ar[record.start_date.month]} {record.start_date.year}"
        date_en = record.start_date.strftime("%d %B %Y").lstrip("0")
        lead_ar = f"موعد **{event_ar}** في {semester_ar} {query.academic_year} لنطاق {college_ar} هو **{date_ar}**."
        lead_en = f"The **{event_en}** date for the {semester_en} of {query.academic_year} for {college_en} is **{date_en}**."
    else:
        start_ar = f"{record.start_date.day}/{record.start_date.month}/{record.start_date.year}"
        end_ar = f"{record.end_date.day}/{record.end_date.month}/{record.end_date.year}"
        start_en = record.start_date.strftime("%d %B %Y").lstrip("0")
        end_en = record.end_date.strftime("%d %B %Y").lstrip("0")
        lead_ar = f"فترة **{event_ar}** في {semester_ar} {query.academic_year} لنطاق {college_ar} هي من **{start_ar}** إلى **{end_ar}**."
        lead_en = f"The **{event_en}** period for the {semester_en} of {query.academic_year} for {college_en} runs from **{start_en}** to **{end_en}**."

    source_title = resolution.source.title_en if english else resolution.source.title_ar
    source_label = "Official source" if english else "المصدر الرسمي"
    return f"{lead_en if english else lead_ar}\n\n{source_label}: [{source_title}]({record.source_url})"


def is_english_message(message: str) -> bool:
    latin = len(re.findall(r"[A-Za-z]", message))
    arabic = len(re.findall(r"[\u0600-\u06ff]", message))
    return latin > arabic
