from datetime import datetime

from email_task_agent.eml_parser import EmailRecord
from email_task_agent.models import MailSummary
from email_task_agent.topics import (build_keyword_index, normalize_keyword, subject_keywords, summarize_periods,
                                     topics_to_markdown)


def _rec(subject, when):
    return EmailRecord(path="x", message_id=subject, subject=subject, sender="a@b.c", to=[], cc=[],
                       date=when, body="")


def test_normalize_groups_spelling_variants():
    assert normalize_keyword("Q3 실적 보고") == normalize_keyword("q3실적보고") == normalize_keyword("Ｑ３ 실적-보고")


def test_subject_fallback_keywords():
    assert subject_keywords("RE: FW: [주간보고] 9월 2주차 주간보고 취합 요청") == ["주간보고", "9월", "2주차", "취합"]


def test_keyword_index_merges_variants_and_falls_back_to_subject():
    recs = [_rec("견적서 검토", datetime(2026, 8, 3)), _rec("단가표 수정", datetime(2026, 9, 1)),
            _rec("[ERP 개편] 일정 공유", datetime(2026, 9, 2))]
    summaries = {
        "M1": MailSummary(mail_id="M1", summary="s", keywords=["A사 견적", "메일"]),      # '메일' 은 일반어라 제외
        "M2": MailSummary(mail_id="M2", summary="s", keywords=["A사견적", "단가표"]),
        # M3 은 요약 누락 → 제목에서 키워드
    }
    idx = build_keyword_index(recs, summaries)
    keys = [m["keys"] for m in idx["mails"]]
    assert keys[0] == keys[1][:1] == [normalize_keyword("A사 견적")]   # 표기 달라도 같은 키워드
    assert idx["mails"][2]["keyword_source"] == "subject" and idx["keywords"][keys[2][0]] == "ERP 개편"
    md = topics_to_markdown(idx, {"2026-09": "9월 흐름"})
    assert md.index("2026-09") < md.index("2026-08") and "> 9월 흐름" in md


def test_overview_failure_is_not_fatal():
    from email_task_agent.llm import LLMError

    class Broken:
        def chat_structured(self, *a, **k):
            raise LLMError("서버 오류")

    idx = build_keyword_index([_rec("견적서 검토", datetime(2026, 8, 3))], {})
    overviews, warnings = summarize_periods(Broken(), idx, log=lambda m: None)
    assert overviews == {} and warnings and "키워드는 정상 표시" in warnings[0]
