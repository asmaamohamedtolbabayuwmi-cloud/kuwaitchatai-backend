from app.services.query_router import (
    ku_web_search_tool,
    should_fallback_to_web,
    web_search_status,
)


def test_ku_web_search_is_domain_restricted():
    assert ku_web_search_tool() == {
        "type": "web_search",
        "filters": {"allowed_domains": ["ku.edu.kw"]},
    }


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
