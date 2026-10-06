"""Generate demo .eml files in samples/mails (run: python samples/make_samples.py)."""

from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone, timedelta
from pathlib import Path

KST = timezone(timedelta(hours=9))
OUT = Path(__file__).parent / "mails"
ME = "김대리 <me@corp.example>"

MAILS = [
    ("2026-09-11 17:30", "박팀장 <park@corp.example>", ME, "[주간보고] 9월 2주차 주간보고 취합",
     "김대리님, 이번 주 주간보고 금요일 오후 5시까지 공유폴더에 올려주세요.\n매주 금요일 동일하게 부탁드립니다.", "plain", "utf-8"),
    ("2026-10-02 10:12", "이부장 <lee@corp.example>", ME, "Q3 실적 보고서 작성 요청",
     "<p>김대리,</p><p>Q3 실적 보고서 초안을 <b>다음주 목요일까지</b> 메일로 보내주세요.</p><p>임원 보고 일정이 잡혀 있어 일정 엄수 바랍니다.</p>",
     "html", "utf-8"),
    ("2026-10-05 09:05", "최과장 <choi@partner.example>", ME, "RE: 견적서 검토 요청",
     "김대리님 안녕하세요.\n보내주신 견적서 관련해서 단가표 수정본을 10월 14일까지 회신 부탁드립니다.\n\n-----Original Message-----\nFrom: 김대리\n견적서 송부드립니다.",
     "plain", "euc-kr"),
    ("2026-10-05 14:40", ME, "정사원 <jung@corp.example>", "신규 입사자 온보딩 자료",
     "정사원님, 온보딩 자료는 제가 이번주 수요일까지 정리해서 공유드리겠습니다.", "plain", "utf-8"),
    ("2026-09-29 11:00", "인사팀 <hr@corp.example>", "전직원 <all@corp.example>", "[공지] 사내 체육대회 안내",
     "10월 23일 사내 체육대회가 진행됩니다. 참고 바랍니다.", "plain", "utf-8"),
    ("2026-09-30 16:20", "박팀장 <park@corp.example>", ME, "보안교육 이수 요청",
     "필수 보안교육 9월 30일까지 이수 바랍니다.", "plain", "utf-8"),
]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for i, (dt, frm, to, subj, body, kind, charset) in enumerate(MAILS, 1):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = frm, to, subj
        msg["Date"] = format_datetime(datetime.strptime(dt, "%Y-%m-%d %H:%M").replace(tzinfo=KST))
        msg["Message-ID"] = f"<sample-{i}@corp.example>"
        if kind == "html":
            msg.set_content(body, subtype="html", charset=charset)
        else:
            msg.set_content(body, charset=charset)
        (OUT / f"{i:02d}.eml").write_bytes(bytes(msg))
    print(f"{len(MAILS)}개 생성: {OUT}")


if __name__ == "__main__":
    main()
