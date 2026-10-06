"""Generate demo .eml files (run: python samples/make_samples.py [출력 폴더]).

Mail dates are relative to the current week, so the demo always has something due
this week / next week without needing --date.
"""

import sys
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

KST = timezone(timedelta(hours=9))
ME = "김대리 <me@corp.example>"


def _md(d: date) -> str:
    return f"{d.month}월 {d.day}일"


def mails(today: date):
    mon = today - timedelta(days=today.weekday())  # 이번 주 월요일
    at = lambda d, hm: datetime.combine(d, datetime.strptime(hm, "%H:%M").time())  # noqa: E731
    return [
        # 3주 전 금요일: 반복 업무 패턴
        (at(mon - timedelta(days=24), "17:30"), "박팀장 <park@corp.example>", ME, "[주간보고] 주간보고 취합",
         "김대리님, 이번 주 주간보고 금요일 오후 5시까지 공유폴더에 올려주세요.\n매주 금요일 동일하게 부탁드립니다.",
         "plain", "utf-8"),
        # 지난주 금요일: "다음주 목요일" = 이번 주 목요일
        (at(mon - timedelta(days=3), "10:12"), "이부장 <lee@corp.example>", ME, "Q3 실적 보고서 작성 요청",
         "<p>김대리,</p><p>Q3 실적 보고서 초안을 <b>다음주 목요일까지</b> 메일로 보내주세요.</p>"
         "<p>임원 보고 일정이 잡혀 있어 일정 엄수 바랍니다.</p>", "html", "utf-8"),
        # 이번 주 월요일: 다음 주 수요일 마감 (EUC-KR, 인용 본문 포함)
        (at(mon, "09:05"), "최과장 <choi@partner.example>", ME, "RE: 견적서 검토 요청",
         f"김대리님 안녕하세요.\n보내주신 견적서 관련해서 단가표 수정본을 {_md(mon + timedelta(days=9))}까지 회신 부탁드립니다."
         "\n\n-----Original Message-----\nFrom: 김대리\n견적서 송부드립니다.", "plain", "euc-kr"),
        # 내가 약속한 일
        (at(mon, "14:40"), ME, "정사원 <jung@corp.example>", "신규 입사자 온보딩 자료",
         "정사원님, 온보딩 자료는 제가 이번주 수요일까지 정리해서 공유드리겠습니다.", "plain", "utf-8"),
        # 단순 공지
        (at(mon - timedelta(days=7), "11:00"), "인사팀 <hr@corp.example>", "전직원 <all@corp.example>",
         "[공지] 사내 체육대회 안내", f"{_md(mon + timedelta(days=17))} 사내 체육대회가 진행됩니다. 참고 바랍니다.",
         "plain", "utf-8"),
        # 기한 지난 업무
        (at(mon - timedelta(days=6), "16:20"), "박팀장 <park@corp.example>", ME, "보안교육 이수 요청",
         f"필수 보안교육 {_md(mon - timedelta(days=5))}까지 이수 바랍니다.", "plain", "utf-8"),
    ]


def main(out: Path = Path(__file__).parent / "mails", today: date | None = None) -> Path:
    today = today or date.today()
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    count = 0
    for i, (dt, frm, to, subj, body, kind, charset) in enumerate(mails(today), 1):
        if dt > now and today == date.today():
            dt = now  # 오늘보다 미래 시각의 메일은 만들지 않음
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = frm, to, subj
        msg["Date"] = format_datetime(dt.replace(tzinfo=KST))
        msg["Message-ID"] = f"<sample-{i}@corp.example>"
        if kind == "html":
            msg.set_content(body, subtype="html", charset=charset)
        else:
            msg.set_content(body, charset=charset)
        (out / f"{i:02d}.eml").write_bytes(bytes(msg))
        count += 1
    print(f"{count}개 생성: {out} (기준 주: {today - timedelta(days=today.weekday())} 월요일)")
    return out


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "mails")
