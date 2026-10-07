"""
app/services/query_router.py

Isolated component to decide if a user query requires Vector Store retrieval.
Designed to be conservative (prioritizes false positives over false negatives)
and evolvable into a lightweight intent classifier without changing core logic.
"""

import re

KUWAIT_UNIVERSITY_DOMAIN = "ku.edu.kw"


def ku_web_search_tool() -> dict:
    """Return the Responses API tool restricted to Kuwait University."""
    return {
        "type": "web_search",
        "filters": {"allowed_domains": [KUWAIT_UNIVERSITY_DOMAIN]},
    }


def web_search_status(user_message: str) -> str:
    """Explain the fallback in the language used by the student."""
    if re.search(r"[\u0600-\u06ff]", user_message):
        return "لم أجد الإجابة في المصادر المسترجعة. سأبحث عنها الآن في موقع جامعة الكويت.\n\n"
    return (
        "I couldn't find the answer in the retrieved sources. "
        "I'll search the Kuwait University website now.\n\n"
    )


_INSUFFICIENT_ANSWER_PHRASES = (
    "لا أملك معلومات كافية",
    "لم أجد معلومات كافية",
    "لا تتوفر معلومات كافية",
    "غير متوفرة في الوثائق الرسمية",
    "غير موجودة في الوثائق الرسمية",
    "i don't have enough information",
    "i do not have enough information",
    "couldn't find enough information",
    "could not find enough information",
    "not available in the official documents",
    "not found in the official documents",
)


def should_fallback_to_web(answer: str, citations: list[dict]) -> bool:
    """Use KU web search when retrieval has no source-backed answer.

    File Search can attach a citation to an apology, so citation presence alone
    is not proof that the retrieved documents answered the question.
    """
    if not citations:
        return True

    normalized_answer = " ".join(answer.lower().split())
    return any(
        phrase in normalized_answer for phrase in _INSUFFICIENT_ANSWER_PHRASES
    )

def should_use_file_search(user_message: str) -> bool:
    """
    Determines if File Search is necessary for the given message.
    Returns True if retrieval might be needed, False if definitely not needed.
    """
    text = user_message.strip().lower()
    
    # 1. Conservative fallback: Any message longer than 3 words is assumed to need retrieval.
    # Students rarely ask complex academic questions in 3 words or less.
    words = text.split()
    if len(words) > 3:
        return True
        
    # 2. Minimal heuristic for very obvious conversational queries
    # Avoids a massive, unmaintainable regex list. Only captures the most absolute basics.
    casual_phrases = {
        "مرحبا", "أهلا", "هلا", "السلام عليكم", "شلونك", "كيفك",
        "شكرا", "يعطيك العافية", "مشكور", "شكراً",
        "hi", "hello", "thanks", "ok", "نعم", "لا"
    }
    
    # Check if the text matches or closely contains these phrases
    if text in casual_phrases or any(text == p for p in casual_phrases):
        return False
        
    # If in doubt, ALWAYS retrieve.
    return True


_PERSONAL_SCHEDULE_PATTERNS = (
    r"\bجدولي\b",
    r"\bجداولي\b",
    r"\bموادي\b",
    r"\bمقرراتي\b",
    r"\bمحاضراتي\b",
    r"\bشعبي\b",
    r"\bسكاشني\b",
    r"\bدكاترتي\b",
    r"\bاختباراتي\b",
    r"\bفاينلاتي\b",
    r"\bمحاضرتي\b",
    r"\bاختباري\b",
    r"\bفاينلي\b",
    r"\bمادتي\b",
    r"\bشعبتي\b",
    r"\bدكتوري\b",
    r"الجدول (?:اللي|الذي) رفعت",
    r"الجداول (?:اللي|التي) رفعت",
    r"الفصول (?:اللي|التي) رفعت",
    r"قارن .*?(?:جداول|فصول)",
    r"كنت (?:مسجل|ماخذ|آخذ)",
    r"المواد (?:المسجلة|المنسحبة) (?:عندي|لي)",
    r"(?:إيه|ايه|شنو|وش) عندي (?:اليوم|بكرة|غد|يوم)",
    r"عندي (?:إيه|ايه|شنو|وش) (?:اليوم|بكرة|غد|يوم)",
    r"ارفع (?:جدول|الجدول)",
    r"رفع (?:جدول|الجدول)",
    r"\bmy schedule\b",
    r"\bmy (?:courses|classes|sections|instructors|exams)\b",
    r"\bmy (?:grade|level|academic level|gpa|major|minor|college|credits|academic status)\b",
    r"\bwhat(?: is|'s) my (?:grade|level|academic level|gpa|major|minor|college|credits|academic status)\b",
    r"\bschedules? i (?:uploaded|added)\b",
    r"\bcourses? i (?:am|was) (?:taking|registered in)\b",
    r"(?:انا|أنا|اني|إني).{0,30}\b(?:grade|level|gpa)\b",
    r"\b(?:grade|level|gpa)\s+(?:بتاعي|بتاعتي|حقي|مالي)\b",
    r"\b(?:معدلي|مستواي|مرحلتي|تخصصي|كليتي|ساعاتي|إنذاراتي|انذاراتي)\b",
    r"\b(?:حالتي|وضعي) (?:الدراسية|الأكاديمية|الاكاديمية)\b",
)


def is_user_schedule_query(user_message: str) -> bool:
    """Return True for questions about the authenticated user's own schedules."""
    text = " ".join(user_message.strip().lower().split())
    return any(re.search(pattern, text) for pattern in _PERSONAL_SCHEDULE_PATTERNS)
