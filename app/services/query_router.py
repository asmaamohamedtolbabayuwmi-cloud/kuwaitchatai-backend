"""Deterministic, low-latency intent routing for chat requests.

The router deliberately uses local rules instead of an LLM classifier. This
avoids an extra network call, token cost, and failure mode. Clear non-retrieval
intents stay local; unknown intent falls back conservatively to retrieval.
"""

from __future__ import annotations

import re
from enum import Enum


class QueryIntent(str, Enum):
    TIME_SENSITIVE_UNIVERSITY = "time_sensitive_university"
    OFFICIAL_UNIVERSITY = "official_university"
    UNCERTAIN = "uncertain"
    PERSONAL_SCHEDULE = "personal_schedule"
    GENERAL_EDUCATIONAL = "general_educational"
    CASUAL_UNIVERSITY = "casual_university"
    OUT_OF_SCOPE = "out_of_scope"


def ku_web_search_tool() -> dict:
    """Return web search without a domain allow-list.

    Relevance and source-authority requirements are enforced by the web prompt.
    This permits official social accounts, faculty pages, external university
    services, and corroborating news sources.
    """
    return {"type": "web_search"}


def web_search_status(user_message: str) -> str:
    """Explain the fallback in the language used by the student."""
    if re.search(r"[\u0600-\u06ff]", user_message):
        return "لم أجد إجابة موثقة داخلياً. سأبحث في مصادر الويب ذات الصلة بجامعة الكويت.\n\n"
    return (
        "I couldn't find a sourced answer internally. "
        "I'll search relevant Kuwait University web sources now.\n\n"
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
    """Use web search when File Search has no source-backed answer."""
    if not citations:
        return True
    normalized_answer = " ".join(answer.lower().split())
    return any(
        phrase in normalized_answer for phrase in _INSUFFICIENT_ANSWER_PHRASES
    )


_PERSONAL_SCHEDULE_PATTERNS = (
    r"\bجدولي\b", r"\bجداولي\b", r"\bموادي\b", r"\bمقرراتي\b",
    r"\bمحاضراتي\b", r"\bشعبي\b", r"\bسكاشني\b", r"\bدكاترتي\b",
    r"\bاختباراتي\b", r"\bفاينلاتي\b", r"\bمحاضرتي\b",
    r"\bاختباري\b", r"\bفاينلي\b", r"\bمادتي\b", r"\bشعبتي\b",
    r"\bدكتوري\b", r"الجدول (?:اللي|الذي) رفعت",
    r"(?:آخر|اخر|أحدث|احدث) جدول (?:رفعته|رفعتيه|رفعته أنا|رفعته انا)",
    r"الجداول (?:اللي|التي) رفعت", r"الفصول (?:اللي|التي) رفعت",
    r"قارن .*?(?:جداول|فصول)", r"كنت (?:مسجل|ماخذ|آخذ)",
    r"المواد (?:المسجلة|المنسحبة) (?:عندي|لي)",
    r"(?:إيه|ايه|شنو|وش) عندي (?:اليوم|بكرة|غد|يوم)",
    r"عندي (?:إيه|ايه|شنو|وش) (?:اليوم|بكرة|غد|يوم)",
    r"(?:شنو|وش|إيه|ايه) عندي (?:محاضرات|مواد|اختبارات).{0,20}(?:اليوم|باجر|بكرة|غد)",
    r"عندي (?:محاضرات|مواد|اختبارات).{0,20}(?:اليوم|باجر|بكرة|غد)",
    r"ارفع (?:جدول|الجدول)", r"رفع (?:جدول|الجدول)",
    r"\bmy schedule\b", r"\bmy (?:courses|classes|sections|instructors|exams)\b",
    r"\bmy (?:grade|level|academic level|gpa|major|minor|college|credits|academic status)\b",
    r"\bwhat(?: is|'s) my (?:grade|level|academic level|gpa|major|minor|college|credits|academic status)\b",
    r"\bschedules? i (?:uploaded|added)\b",
    r"\bcourses? i (?:am|was) (?:taking|registered in)\b",
    r"(?:انا|أنا|اني|إني).{0,30}\b(?:grade|level|gpa)\b",
    r"\b(?:grade|level|gpa)\s+(?:بتاعي|بتاعتي|حقي|مالي)\b",
    r"\b(?:معدلي|مستواي|مرحلتي|تخصصي|كليتي|ساعاتي|إنذاراتي|انذاراتي)\b",
    r"\b(?:حالتي|وضعي) (?:الدراسية|الأكاديمية|الاكاديمية)\b",
)

_CASUAL_EXACT = {
    "مرحبا", "مرحبا بك", "أهلا", "اهلا", "هلا", "السلام عليكم",
    "وعليكم السلام", "شلونك", "كيفك", "شخبارك", "شكرا", "شكراً",
    "يعطيك العافية", "مشكور", "تمام", "حسنا", "حسنًا", "اوكي",
    "hi", "hello", "hey", "thanks", "thank you", "ok", "okay",
    "مش فاهم", "مو فاهم", "ما فهمت", "وضح اكثر", "وضح أكثر",
    "i don't understand", "i do not understand", "can you explain that",
}

_CONVERSATIONAL_FOLLOW_UP_PATTERNS = (
    r"^(?:مش|مو|ماني|ما) فاهم(?:ة)?(?: (?:الكلام|الشرح|الاجابة|الاجابه|هذا|هذي|ده|دي))?$",
    r"^(?:وضح|وضّح|اشرح)(?:ها| لي| لي اكثر| لي أكثر| اكثر| أكثر)?$",
    r"^(?:i (?:do not|don't) understand|can you explain that|explain that again)(?: please)?$",
)

_TIME_SENSITIVE_MARKERS = (
    r"(?:آخر|اخر|أحدث|احدث|الأحدث|الاحدث|حالي|حالية|الحالي|الحالية|حاليًا|حاليا|الآن|الان|جديد|جديدة|حديث|حديثة|مؤخرًا|مؤخرا|اليوم|الحين|هالفترة|هالأسبوع|هذا الأسبوع|هذا الاسبوع)",
    r"(?:متى|موعد|تاريخ).{0,25}(?:ينتهي|يبدأ|يفتح|يقفل|التسجيل|القبول|السحب)",
    r"\b(?:latest|newest|most recent|recent|currently|current|today|now|this week|this semester)\b",
    r"\b(?:when|date|deadline).{0,30}(?:registration|admission|withdrawal|add.drop)\b",
)

_TIME_SENSITIVE_UNIVERSITY_TOPICS = (
    r"(?:(?:ال|لل)?تسجيل|(?:ال|لل)?قبول|السحب والإضافة|السحب والاضافة|وقف القيد|المنحة|البعثة|المكافأة|المكافاه|الرسوم|موعد نهائي)",
    r"(?:registration|admission|add.drop|withdrawal|scholarship|tuition|deadline)",
    r"(?:جامعة الكويت|الجامعة).{0,80}(?:إعلان|اعلان|خبر|تحديث|منشور|حساب|إنستغرام|انستغرام|تويتر)",
    r"(?:إعلان|اعلان|خبر|تحديث|منشور|حساب).{0,80}(?:جامعة الكويت|الجامعة|الحساب الرسمي|الحسابات الرسمية|إنستغرام|انستغرام|تويتر)",
    r"(?:kuwait university|\bku\b).{0,80}(?:announcement|news|update|post|official account|instagram|twitter|\bx\b)",
    r"(?:announcement|news|update|post|official account).{0,80}(?:kuwait university|\bku\b|instagram|twitter|\bx\b)",
)

_OFFICIAL_PATTERNS = (
    r"(?:شروط|موعد|مواعيد|فترة|قرار|لائحة|قانون|سياسة|إعلان|اعلان).{0,30}(?:القبول|التسجيل|السحب|الوقف|الإيقاف|الايقاف|المكافأة|المكافاه|البعثة|المنحة|الرسوم)",
    r"(?:متى|يفتح|يبدأ|ينتهي).{0,25}(?:القبول|التسجيل|السحب|الإضافة|الاضافة|المنحة|البعثة)",
    r"(?:كيف|شلون).{0,25}(?:أسجل|اسجل|أنسحب|انسحب|أوقف قيدي|اوقف قيدي)",
    r"(?:القبول|السحب والإضافة|السحب والاضافة|وقف القيد|إيقاف القيد|المنح|البعثات|المكافأة الاجتماعية|الرسوم الجامعية|لوائح الجامعة|قرارات الجامعة|إعلانات الجامعة|اعلانات الجامعة)",
    r"(?:رسوم|منحة|بعثة|مكافأة|مكافاه).{0,25}جامعة الكويت",
    r"(?:أين|اين|وين|موقع|معلومات عن).{0,25}جامعة الكويت",
    r"(?:when|what is|where is|how (?:do|can)).{0,30}(?:admission|registration|add.drop|withdrawal|suspension|scholarship|tuition|deadline)",
    r"(?:admission|registration|add.drop|withdrawal|suspension|scholarship|tuition|fees?|regulation|deadline|announcement).{0,40}(?:university|college|course|semester|student|ku)",
    r"(?:university|college|course|semester|student|ku).{0,40}(?:admission|registration|add.drop|withdrawal|suspension|scholarship|tuition|fees?|regulation|deadline|announcement)",
    # Colleges, majors, and academic programs offered by the university.
    r"(?:شنو|وش|ما|ما هي|ابي|أبي|ودي).{0,30}(?:تخصصات|كليات|أقسام|اقسام).{0,30}(?:جامعة الكويت|الجامعة|موجودة|متاحة)",
    r"(?:تخصصات|كليات|أقسام|اقسام).{0,30}(?:جامعة الكويت|الجامعة)",
    r"(?:جامعة الكويت|الجامعة).{0,30}(?:تخصصات|كليات|أقسام|اقسام|برامج أكاديمية|برامج اكاديمية)",
    r"(?:majors?|colleges?|departments?|degree programs?).{0,35}(?:kuwait university|university|ku|offered|available)",
    r"(?:kuwait university|\bku\b).{0,35}(?:majors?|colleges?|departments?|degree programs?)",
    # Student Affairs and student-facing administrative services.
    r"(?:عمادة )?شؤون الطلبة",
    r"(?:student affairs|deanship of student affairs)",
    r"(?:وين|أين|اين|موقع|مكان|خدمات|رقم|تواصل).{0,30}(?:العمادة|عمادة|مكتب|إدارة|ادارة).{0,20}(?:الطلبة|الطلاب)",
    # Enrollment certificates and official student-status documents.
    r"(?:شهادة|إفادة|افادة|إثبات|اثبات).{0,15}(?:قيد|طالب|استمرارية)",
    r"لمن يهمه الأمر",
    r"(?:enrollment|student status|proof of enrollment|proof of study).{0,20}(?:certificate|letter|document)?",
    # University ID issuance, validity, replacement, and renewal procedures.
    r"(?:الهوية|البطاقة) الجامعية",
    r"(?:university|student) id(?: card)?",
    # Faculty members and administrative staff in an institutional context.
    r"(?:عميد|وكيل|رئيس قسم|مدير|أعضاء هيئة التدريس|عضو هيئة التدريس).{0,35}(?:كلية|قسم|جامعة الكويت|الجامعة|عمادة|إدارة|ادارة)",
    r"(?:منو|من هو|من هي|شنو اسم|وش اسم).{0,20}(?:العميد|الوكيل|رئيس القسم|المدير)",
    r"(?:منو|من هو|من هي|شنو اسم|وش اسم).{0,20}رئيس قسم",
    r"(?:dean|department chair|faculty member|academic staff|administrative staff).{0,35}(?:college|department|kuwait university|university|office)",
)

_EDUCATIONAL_PATTERNS = (
    r"(?:اشرح|فسر|وضّح|وضح|لخص|علمني|ساعدني|حل|احسب|راجع|اختبرني|سو[ّي]? لي|اعطني|أعطني).{0,80}",
    r"(?:رياضيات|جبر|تفاضل|تكامل|إحصاء|احصاء|اقتصاد|محاسبة|برمجة|فيزياء|كيمياء|أحياء|احياء|إدارة أعمال|ادارة اعمال|تمويل|هندسة|خوارزمية|بايثون|جافا|قواعد البيانات|علم النفس|علم الاجتماع)",
    r"(?:explain|summari[sz]e|teach|solve|calculate|quiz|revision|study|homework|assignment|give me an example).{0,80}",
    r"\b(?:math|algebra|calculus|statistics|economics|accounting|programming|physics|chemistry|biology|engineering|algorithm|python|java|sql|database|psychology|sociology)\b",
    r"(?:معادلة|مسألة|واجب|ملخص محاضرة|أسئلة مراجعة|اسئلة مراجعة|اختبار قصير|كود برمجي)",
)

_OUT_OF_SCOPE_PATTERNS = (
    r"(?:مباراة|ماتش|كورة|كرة القدم|الدوري|دوري الأبطال|دوري الابطال|كأس العالم|كاس العالم|المنتخب).{0,40}(?:فاز|نتيجة|موعد|لاعب|فريق|نادي)?",
    r"(?:فيلم|مسلسل|أغنية|اغنية|مغني|مطرب|ممثل|نتفلكس|سينما).{0,40}(?:اقترح|أفضل|افضل|شاهد|جديد)?",
    r"(?:تسوق|اشتري|أشتري|أفضل سعر|افضل سعر|خصم|كوبون|عروض التسوق|مطعم)",
    r"(?:سعر سهم|عملة رقمية|طقس)",
    r"\b(?:football|soccer|nba|sports score|match score|movie|tv show|song|netflix|restaurant|shopping|buy a|best price|stock price|crypto|weather)\b",
)


def _matches_any(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _normalize(user_message: str) -> str:
    """Normalize spacing and harmless sentence punctuation for exact matches."""
    text = user_message.strip().casefold()
    text = re.sub(r"[!?؟،,.;:]+", " ", text)
    return " ".join(text.split())


def is_user_schedule_query(user_message: str) -> bool:
    """Return True for questions about the authenticated user's own data."""
    text = _normalize(user_message)
    return _matches_any(_PERSONAL_SCHEDULE_PATTERNS, text)


def is_time_sensitive_query(user_message: str) -> bool:
    """Return True when a university query requires current web evidence."""
    text = _normalize(user_message)
    return _matches_any(_TIME_SENSITIVE_MARKERS, text) and _matches_any(
        _TIME_SENSITIVE_UNIVERSITY_TOPICS,
        text,
    )


def classify_query(user_message: str) -> QueryIntent:
    """Classify a request without adding model latency or token cost."""
    text = _normalize(user_message)
    if is_user_schedule_query(text):
        return QueryIntent.PERSONAL_SCHEDULE
    if text in _CASUAL_EXACT or _matches_any(_CONVERSATIONAL_FOLLOW_UP_PATTERNS, text):
        return QueryIntent.CASUAL_UNIVERSITY
    if is_time_sensitive_query(text):
        return QueryIntent.TIME_SENSITIVE_UNIVERSITY
    if _matches_any(_OFFICIAL_PATTERNS, text):
        return QueryIntent.OFFICIAL_UNIVERSITY
    if _matches_any(_OUT_OF_SCOPE_PATTERNS, text):
        return QueryIntent.OUT_OF_SCOPE
    if _matches_any(_EDUCATIONAL_PATTERNS, text):
        return QueryIntent.GENERAL_EDUCATIONAL
    # A deterministic ruleset cannot safely prove that an unmatched question is
    # unrelated. Route it conservatively so legitimate university questions are
    # not rejected merely because their wording was not anticipated.
    return QueryIntent.UNCERTAIN


def should_use_file_search(user_message: str) -> bool:
    """Backward-compatible boolean facade for existing callers."""
    return classify_query(user_message) in {
        QueryIntent.OFFICIAL_UNIVERSITY,
        QueryIntent.UNCERTAIN,
    }
