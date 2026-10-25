"""Small deterministic guards for high-risk university-specific claims."""

from __future__ import annotations

import re


_UNIVERSITY_ID_PATTERNS = (
    r"(?:الهوية|البطاقة) الجامعية",
    r"(?:university|student) id(?: card)?",
)
_RENEWAL_PATTERNS = (
    r"(?:تجديد|تجدد|صلاحية|صالحة)",
    r"(?:renew|valid|validity|expir)",
)
_FREQUENCY_PATTERNS = (
    r"(?:(?:في|مع) بداية |مرة |مع |في )?كل (?:فصل|ترم|سنة|عام)(?: دراسي(?:ة)?)?",
    r"(?:لكل|كل) فصل(?: دراسي)?",
    r"(?:فصل|عام|سنة)(?: دراسي(?:ة)?)? واحد(?:ة)?",
    r"(?:سنويا|نصف سنوي|فصليا|فصلية)",
    r"(?:every|each|per) (?:academic )?(?:year|semester|term)",
    r"(?:annual|annually|semesterly)",
)


def is_university_id_query(text: str) -> bool:
    normalized = _normalize(text)
    return any(re.search(pattern, normalized) for pattern in _UNIVERSITY_ID_PATTERNS)


def missing_evidence_answer(user_message: str) -> str:
    """Return a deterministic no-evidence answer in the user's language."""
    if re.search(r"[\u0600-\u06ff]", user_message):
        return (
            "لم أجد دليلاً موثوقاً قابلاً للاستشهاد يدعم إجابة محددة في المصادر "
            "المتاحة، لذلك لن أخمّن."
        )
    return (
        "I could not find reliable, citable evidence for a specific answer in "
        "the available sources, so I will not guess."
    )


def _contains_renewal_frequency_claim(text: str) -> bool:
    normalized = _normalize(text)
    return (
        any(re.search(pattern, normalized) for pattern in _RENEWAL_PATTERNS)
        and any(re.search(pattern, normalized) for pattern in _FREQUENCY_PATTERNS)
    )


def _normalize(text: str) -> str:
    text = text.casefold().replace("ـ", "")
    text = re.sub(r"[\u064b-\u065f\u0670]", "", text)
    text = text.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا"}))
    return " ".join(text.split())


def _split_clauses(text: str) -> list[str]:
    """Split while retaining punctuation so filtered Markdown remains readable."""
    pieces = re.split(r"([.!?؟؛;،,\n]+\s*)", text)
    clauses: list[str] = []
    for index in range(0, len(pieces), 2):
        body = pieces[index]
        delimiter = pieces[index + 1] if index + 1 < len(pieces) else ""
        if body or delimiter:
            # A supported fact and an unsupported interval often share one
            # sentence joined by "و"/"and".  Split at conjunction boundaries
            # without changing text that is ultimately retained.
            joined = body + delimiter
            clauses.extend(
                part
                for part in re.split(
                    r"(?=\s+(?:و[\u0621-\u064a]|and\b))",
                    joined,
                    flags=re.IGNORECASE,
                )
                if part
            )
    return clauses


def _evidence_supports_frequency(evidence_texts: list[str] | None) -> bool:
    # Require renewal/validity and its interval in the same sentence.  Merely
    # finding both words somewhere in a long retrieved passage is not entailment.
    for evidence in evidence_texts or []:
        for sentence in re.split(r"(?<=[.!?؟؛;\n])\s*", evidence):
            if _contains_renewal_frequency_claim(sentence):
                return True
    return False


def _text_contains_frequency_claim(text: str) -> bool:
    return any(
        _contains_renewal_frequency_claim(sentence)
        for sentence in re.split(r"(?<=[.!?؟؛;\n])\s*", text)
        if sentence.strip()
    )


def sanitize_university_id_answer(
    user_message: str,
    answer: str,
    evidence_texts: list[str] | None = None,
) -> tuple[str, bool]:
    """Remove a renewal-frequency claim when retrieved text does not support it.

    A citation merely proves that a file or URL was used; it does not prove that
    every sentence is entailed by that source.  This narrow guard addresses the
    observed high-risk failure without pretending to be a general fact checker.
    """
    if not is_university_id_query(user_message) or not _text_contains_frequency_claim(answer):
        return answer, False

    if _evidence_supports_frequency(evidence_texts):
        return answer, False

    # Keep independently useful, sourced details while removing only sentences
    # that assert an unsupported renewal interval.
    clauses = _split_clauses(answer.strip())
    kept: list[str] = []
    removed = False
    for clause in clauses:
        normalized = _normalize(clause)
        has_frequency = any(re.search(pattern, normalized) for pattern in _FREQUENCY_PATTERNS)
        if _contains_renewal_frequency_claim(clause) or has_frequency:
            removed = True
            continue
        kept.append(clause)
    arabic = bool(re.search(r"[\u0600-\u06ff]", user_message))
    caveat = (
        "لم أجد في الأدلة المسترجعة ما يثبت فترة محددة لتجديد الهوية الجامعية، لذلك لن أفترض أنها سنوية أو فصلية."
        if arabic
        else "The retrieved evidence does not establish a university-ID renewal interval, so I will not assume it is annual or per semester."
    )
    sanitized = "".join(kept).strip().rstrip("،,؛;").rstrip()
    # Defensive fallback: if clause splitting ever fails to identify the risky
    # part, do not return the original contradiction alongside the caveat.
    if not removed:
        sanitized = ""
    if sanitized:
        if sanitized[-1] not in ".!?؟\n":
            sanitized += "."
        sanitized = f"{sanitized}\n\n{caveat}"
    else:
        sanitized = caveat
    return sanitized, True
