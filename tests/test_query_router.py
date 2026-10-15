from app.services.query_router import (
    QueryIntent,
    classify_query,
    is_time_sensitive_query,
    is_user_schedule_query,
    ku_web_search_tool,
    should_fallback_to_web,
    should_use_file_search,
    web_search_status,
)


def test_ku_web_search_has_no_domain_filter():
    assert ku_web_search_tool() == {"type": "web_search"}


def test_web_search_status_matches_question_language():
    assert "سأبحث" in web_search_status("ما شروط القبول؟")
    assert "I'll search" in web_search_status("What are the admission rules?")


def test_fallback_when_file_search_cites_an_insufficient_answer():
    answer = (
        "عذراً، لا أملك معلومات كافية حول صلاحية الهوية الجامعية "
        "وتجديدها في الوثائق الرسمية المتاحة حالياً."
    )
    citations = [{"type": "file_citation", "file_id": "file-1"}]

    assert should_fallback_to_web(answer, citations) is True


def test_no_fallback_for_a_source_backed_answer():
    answer = "يمكن تجديد الهوية الجامعية من خلال بوابة الطالب."
    citations = [{"type": "file_citation", "file_id": "file-1"}]

    assert should_fallback_to_web(answer, citations) is False


def test_fallback_when_file_search_returns_no_citations():
    assert should_fallback_to_web("No sourced answer", []) is True


def test_personal_schedule_queries_bypass_university_document_search():
    assert is_user_schedule_query("قارن بين جداولي في آخر أربع فصول") is True
    assert is_user_schedule_query("إيه المواد اللي كنت مسجلها السنة اللي فاتت؟") is True
    assert is_user_schedule_query("انا ال grade بتاعي اي") is True
    assert is_user_schedule_query("كم معدلي العام؟") is True
    assert is_user_schedule_query("What is my academic level?") is True
    assert is_user_schedule_query("What courses were in my schedule?") is True
    assert is_user_schedule_query("ما شروط التسجيل في جامعة الكويت؟") is False


def test_intent_categories_cover_routing_boundaries():
    assert classify_query("ما شروط القبول في جامعة الكويت؟") is QueryIntent.OFFICIAL_UNIVERSITY
    assert classify_query("متى موعد السحب والإضافة؟") is QueryIntent.TIME_SENSITIVE_UNIVERSITY
    assert classify_query("متى يبدأ التسجيل؟") is QueryIntent.TIME_SENSITIVE_UNIVERSITY
    assert classify_query("When is the registration deadline?") is QueryIntent.TIME_SENSITIVE_UNIVERSITY
    assert classify_query("شنو عندي محاضرات باجر؟") is QueryIntent.PERSONAL_SCHEDULE
    assert classify_query("اشرح لي مفهوم التكامل مع مثال") is QueryIntent.GENERAL_EDUCATIONAL
    assert classify_query("Write a Python function to sort a list") is QueryIntent.GENERAL_EDUCATIONAL
    assert classify_query("هلا") is QueryIntent.CASUAL_UNIVERSITY
    assert classify_query("من فاز بمباراة كرة القدم؟") is QueryIntent.OUT_OF_SCOPE


def test_kuwait_university_colleges_and_majors_are_official_intent():
    queries = (
        "شنو تخصصات جامعة الكويت؟",
        "وش الكليات الموجودة بالجامعة؟",
        "What majors are offered at Kuwait University?",
        "KU colleges and departments",
    )
    assert all(classify_query(query) is QueryIntent.OFFICIAL_UNIVERSITY for query in queries)


def test_student_affairs_locations_and_services_are_official_intent():
    queries = (
        "وين عمادة شؤون الطلبة؟",
        "شنو الخدمات اللي تقدمها شؤون الطلبة؟",
        "Where is the Deanship of Student Affairs?",
        "اشرح لي خدمات شؤون الطلبة",
    )
    assert all(classify_query(query) is QueryIntent.OFFICIAL_UNIVERSITY for query in queries)


def test_enrollment_certificates_are_official_intent_in_kuwaiti_dialect():
    queries = (
        "شلون أطلع شهادة قيد؟",
        "أبي إفادة لمن يهمه الأمر",
        "How can I get an enrollment certificate?",
        "وين أطلع إثبات قيد طالب؟",
    )
    assert all(classify_query(query) is QueryIntent.OFFICIAL_UNIVERSITY for query in queries)


def test_faculty_and_administrative_staff_are_official_intent():
    queries = (
        "منو عميد كلية العلوم؟",
        "من هو رئيس قسم علوم الحاسوب؟",
        "Who is the dean of the College of Engineering?",
        "How do I contact the administrative staff at Kuwait University?",
    )
    assert all(classify_query(query) is QueryIntent.OFFICIAL_UNIVERSITY for query in queries)


def test_university_mention_does_not_override_clear_learning_intent():
    query = "اشرح لي مفهوم التكامل بمثال لطالب في جامعة الكويت"
    assert classify_query(query) is QueryIntent.GENERAL_EDUCATIONAL


def test_clear_academic_questions_do_not_use_retrieval():
    queries = (
        "اشرح لي العرض والطلب مع مثال",
        "حل معادلة تفاضلية خطوة بخطوة",
        "Write a Python function and explain the algorithm",
        "Give me a short accounting revision quiz",
    )
    assert all(classify_query(query) is QueryIntent.GENERAL_EDUCATIONAL for query in queries)
    assert all(should_use_file_search(query) is False for query in queries)


def test_explicit_sports_entertainment_and_shopping_are_out_of_scope():
    queries = (
        "من فاز بمباراة كرة القدم؟",
        "شنو أفضل مسلسل على نتفلكس؟",
        "أبي أشتري آيفون بأفضل سعر",
        "Recommend a movie for tonight",
        "Where can I buy a cheap laptop?",
    )
    assert all(classify_query(query) is QueryIntent.OUT_OF_SCOPE for query in queries)
    assert all(should_use_file_search(query) is False for query in queries)


def test_unknown_wording_is_uncertain_and_uses_university_retrieval():
    queries = (
        "متى تفتح مكتبة جابر؟",
        "قول لي عن مركز نظم المعلومات",
        "Is the north building open on Thursday?",
    )
    assert all(classify_query(query) is QueryIntent.UNCERTAIN for query in queries)
    assert all(should_use_file_search(query) is True for query in queries)


def test_greetings_with_punctuation_remain_casual():
    assert classify_query("هلا!") is QueryIntent.CASUAL_UNIVERSITY
    assert classify_query("Hello!!") is QueryIntent.CASUAL_UNIVERSITY


def test_latest_official_announcements_route_directly_to_current_web_search():
    queries = (
        "ما آخر إعلان نشرته جامعة الكويت على حساباتها الرسمية في إنستغرام أو X؟",
        "شنو أحدث تحديث من جامعة الكويت؟",
        "What is the latest announcement from Kuwait University on Instagram?",
        "Show me KU's most recent official X post",
    )
    assert all(is_time_sensitive_query(query) is True for query in queries)
    assert all(
        classify_query(query) is QueryIntent.TIME_SENSITIVE_UNIVERSITY
        for query in queries
    )
    assert all(should_use_file_search(query) is False for query in queries)


def test_current_deadlines_and_recent_updates_are_time_sensitive():
    queries = (
        "شنو آخر موعد للتسجيل الحالي؟",
        "هل نزل تحديث جديد عن فترة السحب والإضافة؟",
        "What is the current admission deadline?",
        "Any recent Kuwait University registration updates?",
    )
    assert all(
        classify_query(query) is QueryIntent.TIME_SENSITIVE_UNIVERSITY
        for query in queries
    )


def test_latest_word_does_not_override_personal_or_educational_precedence():
    assert classify_query("شنو آخر جدول رفعته؟") is QueryIntent.PERSONAL_SCHEDULE
    assert classify_query("اشرح آخر فصل في ملاحظات الفيزياء") is QueryIntent.GENERAL_EDUCATIONAL
