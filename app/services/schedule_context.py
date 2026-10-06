"""Build a compact, prompt-safe view of a user's stored schedules."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any


def _json_dumps(payload: dict) -> str:
    """Serialize JSON while preventing values from closing prompt delimiters."""
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _created_at_timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _serialize_datetime(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _compact(value: Any) -> Any:
    """Remove null/empty values without altering meaningful schedule content."""
    if isinstance(value, dict):
        compacted = {
            key: _compact(item)
            for key, item in value.items()
            if item is not None and item != ""
        }
        return {
            key: item
            for key, item in compacted.items()
            if item not in ({}, [])
        }
    if isinstance(value, list):
        return [item for item in (_compact(item) for item in value) if item not in ({}, [])]
    return value


def _term_key(schedule: dict) -> str:
    explicit = str(schedule.get("termKey") or "").strip()
    if explicit:
        return explicit

    term = schedule.get("term") or {}
    year = str(term.get("academicYear") or "unknown-year").strip()
    semester = str(term.get("semester") or "unknown-semester").strip()
    return f"{year}|{semester}"


def build_schedule_context(schedules: list[dict] | None) -> str:
    """Return compact JSON for the system prompt.

    ``None`` means Firestore could not be read. An empty list means the user has
    not uploaded a schedule yet. Every stored version is retained so the model
    can compare terms or explain changes within one term.
    """
    if schedules is None:
        payload = {
            "availability": "unavailable",
            "scheduleCount": 0,
            "termCount": 0,
            "schedules": [],
        }
        return _json_dumps(payload)

    ordered = sorted(
        schedules,
        key=lambda item: (_created_at_timestamp(item.get("createdAt")), _term_key(item)),
    )
    versions_per_term: dict[str, list[dict]] = {}
    for schedule in ordered:
        versions_per_term.setdefault(_term_key(schedule), []).append(schedule)

    prepared: list[dict] = []
    for term_key, versions in versions_per_term.items():
        version_count = len(versions)
        for index, schedule in enumerate(versions, start=1):
            prepared.append(
                _compact(
                    {
                        "termKey": term_key,
                        "versionNumber": index,
                        "versionCountForTerm": version_count,
                        "isLatestForTerm": index == version_count,
                        "createdAt": _serialize_datetime(schedule.get("createdAt")),
                        "student": schedule.get("student") or {},
                        "term": schedule.get("term") or {},
                        "credits": schedule.get("credits") or {},
                        "courses": schedule.get("courses") or [],
                    }
                )
            )

    prepared.sort(
        key=lambda item: (
            _created_at_timestamp(item.get("createdAt")),
            item.get("termKey", ""),
        ),
        reverse=True,
    )
    payload = {
        "availability": "available" if prepared else "empty",
        "scheduleCount": len(prepared),
        "termCount": len(versions_per_term),
        "schedules": prepared,
    }
    return _json_dumps(payload)
