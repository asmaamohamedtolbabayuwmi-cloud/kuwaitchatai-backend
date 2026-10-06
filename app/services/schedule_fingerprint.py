"""Server-side canonicalization and hashing for extracted schedules."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from .schedule_models import FingerprintCourseData, FingerprintData


_DIGIT_TRANSLATION = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)
_INVISIBLE_CHARS = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
_SEMESTER_TERM_SUFFIXES = {
    "الأول": "first",
    "الاول": "first",
    "first": "first",
    "1": "first",
    "الثاني": "second",
    "second": "second",
    "2": "second",
    "الصيفي": "summer",
    "صيفي": "summer",
    "summer": "summer",
}


class InvalidFingerprintData(ValueError):
    """Raised when required fingerprint fields cannot be made canonical."""


@dataclass(frozen=True)
class ScheduleIdentity:
    fingerprint: str
    term_key: str
    canonical_string: str
    fingerprint_data: FingerprintData


def _normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value))
    normalized = normalized.translate(_DIGIT_TRANSLATION).translate(_INVISIBLE_CHARS)
    return re.sub(r"\s+", " ", normalized).strip()


def _normalize_compact(value: str | None) -> str:
    return re.sub(r"\s+", "", _normalize_text(value)).upper()


def _normalize_student_id(value: str | None) -> str:
    student_id = _normalize_compact(value)
    if not student_id or not student_id.isdigit():
        raise InvalidFingerprintData("A numeric studentId is required.")
    return student_id


def _normalize_academic_year(value: str | None) -> str:
    year = _normalize_text(value).replace("∕", "/").replace("⁄", "/")
    match = re.fullmatch(r"(\d{4})\s*[/_-]\s*(\d{4})", year)
    if not match:
        raise InvalidFingerprintData("academicYear must use the format YYYY/YYYY.")
    return f"{match.group(1)}/{match.group(2)}"


def _normalize_semester(value: str | None) -> str:
    semester = _normalize_text(value)
    if not semester:
        raise InvalidFingerprintData("semester is required.")
    return semester


def _term_suffix(semester: str) -> str:
    key = semester.casefold()
    suffix = _SEMESTER_TERM_SUFFIXES.get(key)
    if suffix is None:
        raise InvalidFingerprintData(
            "semester must identify the first, second, or summer term."
        )
    return suffix


def build_schedule_identity(data: FingerprintData) -> ScheduleIdentity:
    """Normalize model output and calculate a stable server-owned SHA-256."""
    student_id = _normalize_student_id(data.studentId)
    academic_year = _normalize_academic_year(data.academicYear)
    semester = _normalize_semester(data.semester)

    course_tuples = {
        (
            _normalize_compact(course.courseCode),
            _normalize_compact(course.section),
            _normalize_text(course.status),
        )
        for course in data.courses
    }
    if any(not course_code for course_code, _, _ in course_tuples):
        raise InvalidFingerprintData("Every fingerprint course needs a courseCode.")

    sorted_courses = sorted(course_tuples, key=lambda item: (item[0], item[1], item[2]))
    normalized_courses = [
        FingerprintCourseData(courseCode=code, section=section, status=status)
        for code, section, status in sorted_courses
    ]
    normalized_data = FingerprintData(
        studentId=student_id,
        academicYear=academic_year,
        semester=semester,
        courses=normalized_courses,
    )

    header = f"{student_id}|{academic_year}|{semester}|"
    course_lines = [
        f"{course.courseCode}|{course.section}|{course.status}"
        for course in normalized_courses
    ]
    canonical_string = (
        header if not course_lines else header + "\n" + "|\n".join(course_lines)
    )
    fingerprint = hashlib.sha256(canonical_string.encode("utf-8")).hexdigest()
    term_key = (
        f"{student_id}_{academic_year.replace('/', '_')}_{_term_suffix(semester)}"
    )

    return ScheduleIdentity(
        fingerprint=fingerprint,
        term_key=term_key,
        canonical_string=canonical_string,
        fingerprint_data=normalized_data,
    )
