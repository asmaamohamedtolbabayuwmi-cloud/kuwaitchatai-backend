"""Official-source verification for Kuwait University calendar questions.

The resolver determines event, academic year, semester, and college scope from
the conversation. Exact dates are extracted from a freshly fetched,
scope-appropriate Kuwait University page. No date is stored in application
code and a URL citation alone is never considered evidence.
"""

from __future__ import annotations

import re
import ssl
import warnings
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse
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


OFFICIAL_SOURCES: dict[CollegeScope, OfficialCalendarSource] = {
    CollegeScope.GENERAL: OfficialCalendarSource(
        CollegeScope.GENERAL,
        "https://do.ku.edu.kw/ar/academic-calendar-0",
        "التقويم الجامعي الرسمي لجامعة الكويت",
        "Kuwait University official academic calendar",
    ),
    CollegeScope.LAW: OfficialCalendarSource(
        CollegeScope.LAW,
        "https://do.ku.edu.kw/ar/lawcalender",
        "تقويم كلية الحقوق بجامعة الكويت",
        "Kuwait University College of Law calendar",
    ),
    CollegeScope.ALLIED_HEALTH: OfficialCalendarSource(
        CollegeScope.ALLIED_HEALTH,
        "https://do.ku.edu.kw/ar/college-allied-health-sciencescalender",
        "تقويم كلية العلوم الطبية المساعدة بجامعة الكويت",
        "Kuwait University College of Allied Health Sciences calendar",
    ),
    CollegeScope.DENTISTRY: OfficialCalendarSource(
        CollegeScope.DENTISTRY,
        "https://do.ku.edu.kw/ar/college-dentistery-calender",
        "تقويم كلية طب الأسنان بجامعة الكويت",
        "Kuwait University Faculty of Dentistry calendar",
    ),
    CollegeScope.ENGINEERING: OfficialCalendarSource(
        CollegeScope.ENGINEERING,
        "https://engineering.ku.edu.kw/academic-calendar",
        "تقويم كلية الهندسة والبترول بجامعة الكويت",
        "Kuwait University College of Engineering and Petroleum calendar",
    ),
}


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
        r"الفصل (?:الدراسي )?(?:ال)?اول",
        r"الترم (?:ال)?اول",
        r"(?:first|fall) semester",
    ),
    Semester.SECOND: (
        r"الفصل (?:الدراسي )?الثاني",
        r"الترم الثاني",
        r"(?:second|spring) semester",
    ),
    Semester.SUMMER: (
        r"الفصل (?:الدراسي )?الصيفي",
        r"الترم الصيفي",
        r"summer (?:semester|term)",
    ),
}

_COLLEGE_PATTERNS = {
    CollegeScope.LAW: (r"كلية الحقوق", r"(?:college|faculty) of law", r"\blaw college\b", r"بالحقوق"),
    CollegeScope.ALLIED_HEALTH: (
        r"كلية العلوم الطبية المساعدة",
        r"allied health sciences",
    ),
    CollegeScope.DENTISTRY: (r"كلية طب الاسنان", r"faculty of dentistry", r"dentistry college"),
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

    if college is None and re.search(r"(?:كليتي|my college)", current):
        college = _college_from_schedule(schedule_college)

    return CalendarQuery(
        event=event,
        academic_year=academic_year or current_year,
        semester=semester or current_semester,
        college_scope=college or CollegeScope.GENERAL,
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


async def lookup_official_calendar(
    query: CalendarQuery,
    *,
    fetcher: Callable[[str], Awaitable[str]] | None = None,
    checked_at: datetime | None = None,
) -> EvidenceResolution:
    """Freshly fetch and verify the scope-appropriate official KU calendar."""
    checked_at = checked_at or kuwait_now()
    source = OFFICIAL_SOURCES.get(query.college_scope)
    if source is None:
        return EvidenceResolution(
            "unsupported_scope", (), None, checked_at, "no registered official college calendar"
        )
    try:
        html = await (fetcher or _fetch_html)(source.url)
    except Exception as exc:
        return EvidenceResolution(
            "unavailable", (), source, checked_at, f"official source fetch failed: {type(exc).__name__}"
        )
    return verify_official_calendar_html(query, html, source, checked_at=checked_at)


def calendar_prompt_context(query: CalendarQuery, resolution: EvidenceResolution | None = None) -> str:
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
    resolution: EvidenceResolution,
    *,
    english: bool = False,
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
    college_ar = {
        CollegeScope.GENERAL: "جامعة الكويت",
        CollegeScope.LAW: "كلية الحقوق",
        CollegeScope.ALLIED_HEALTH: "كلية العلوم الطبية المساعدة",
        CollegeScope.DENTISTRY: "كلية طب الأسنان",
        CollegeScope.ENGINEERING: "كلية الهندسة والبترول",
        CollegeScope.UNSUPPORTED: "الكلية المحددة",
    }[query.college_scope]
    college_en = query.college_scope.value.replace("_", " ")

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
