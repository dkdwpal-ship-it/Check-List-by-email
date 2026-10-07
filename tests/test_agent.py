import json
import os
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from email_task_agent import EmailTaskAgent, LLMClient, build_checklist, load_emails, parse_eml
from email_task_agent.agent import expand_recurrence, prune_stale, week_range
from email_task_agent.cli import parse_lookback
from email_task_agent import eml_parser
from email_task_agent.eml_parser import LoadReport
from email_task_agent.llm import extract_json
from email_task_agent.models import Recurrence, Task
from email_task_agent.render import to_markdown

ROOT = Path(__file__).resolve().parents[1]
MAILS = Path()  # 세션 시작 시 임시 폴더에 생성 (메일 날짜는 실행 주 기준 상대값)
TODAY = date(2026, 10, 6)  # 화요일 — 순수 계산(주차/반복/정리) 테스트용 고정 기준일
REAL_TODAY = date.today()
MONDAY = REAL_TODAY - timedelta(days=REAL_TODAY.weekday())


@pytest.fixture(scope="session", autouse=True)
def samples(tmp_path_factory):
    global MAILS
    sys.path.insert(0, str(ROOT / "samples"))
    import make_samples

    MAILS = make_samples.main(tmp_path_factory.mktemp("mails"))


# ---------- parser ----------

def test_parse_html_and_euckr():
    html_mail = parse_eml(MAILS / "02.eml")
    assert "다음주 목요일까지" in html_mail.body and "<b>" not in html_mail.body
    euckr = parse_eml(MAILS / "03.eml")
    next_wed = MONDAY + timedelta(days=9)
    assert f"{next_wed.month}월 {next_wed.day}일까지" in euckr.body
    assert "Original Message" not in euckr.body  # 인용 본문 제거
    assert euckr.date.date() == MONDAY


@pytest.mark.parametrize("variant", [
    "bom", "leading_blank", "crlf", "utf16", "mbox_from", "korean_date", "korean_date2", "iso_date",
    "received_only",
])
def test_parse_real_world_eml_variants(tmp_path, variant):
    import re
    src = (MAILS / "02.eml").read_bytes()
    date_line = re.search(rb"^Date: .*$", src, re.M).group(0)
    sent = parse_eml(MAILS / "02.eml").date
    ko = f"{sent.year}년 {sent.month}월 {sent.day}일 금요일 오전 {sent.hour}:{sent.minute:02d}".encode()
    data = {
        "bom": b"\xef\xbb\xbf" + src,
        "leading_blank": b"\r\n\r\n" + src,
        "crlf": src.replace(b"\n", b"\r\n"),
        "utf16": src.decode().encode("utf-16"),
        "mbox_from": b"From MAILER-DAEMON Fri Oct  2 10:12:00 2026\n" + src,
        "korean_date": src.replace(date_line, b"Date: " + ko),
        "korean_date2": src.replace(date_line, f"Date: {sent:%Y. %m. %d.} (금) {sent:%H:%M}".encode()),
        "iso_date": src.replace(date_line, f"Date: {sent:%Y-%m-%d %H:%M:%S}".encode()),
        "received_only": src.replace(date_line, b"Received: from mx by mail; " + date_line[6:]),
    }[variant]
    (tmp_path / "m.eml").write_bytes(data)
    rec = parse_eml(tmp_path / "m.eml")
    assert rec.subject == "Q3 실적 보고서 작성 요청"
    assert "다음주 목요일까지" in rec.body
    assert rec.date is not None and rec.date.replace(second=0) == sent.replace(second=0)


def test_load_emails_window_and_dedupe(tmp_path):
    for f in MAILS.glob("*.eml"):
        (tmp_path / f.name).write_bytes(f.read_bytes())
    (tmp_path / "dup.eml").write_bytes((MAILS / "01.eml").read_bytes())
    recs = load_emails(tmp_path, since=datetime.now() - timedelta(days=20))
    assert len(recs) == 5  # 3주 전 메일은 기간 밖, 중복 제거
    assert [r.date for r in recs] == sorted(r.date for r in recs)


def _msg(path, subject, when):
    from tests.msg_writer import build_msg

    build_msg(path, subject, "예산안 검토 후 10월 13일까지 회신 부탁드립니다.\r\n", "박팀장", "park@corp.example",
              [("김대리", "me@corp.example")], when, cc=[("이부장", "lee@corp.example")])


def test_load_msg_and_uppercase_extensions(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "A.EML").write_bytes((MAILS / "02.eml").read_bytes())
    _msg(tmp_path / "B.MSG", "[요청] 예산안 검토", datetime.now().replace(microsecond=0) - timedelta(days=1))
    (tmp_path / "archive.pst").write_bytes(b"x")
    report = LoadReport()
    recs = load_emails(tmp_path, report=report)
    assert sorted(r.subject for r in recs) == ["Q3 실적 보고서 작성 요청", "[요청] 예산안 검토"]
    msg = next(r for r in recs if r.path.endswith("B.MSG"))
    assert msg.sender == "박팀장 <park@corp.example>"
    assert msg.to == ["김대리 <me@corp.example>"] and msg.cc == ["이부장 <lee@corp.example>"]
    assert "10월 13일까지" in msg.body and msg.date is not None
    assert report.found == 2 and report.unsupported == {".pst": 1}


def test_load_report_explains_missing_mails(tmp_path):
    for f in MAILS.glob("*.eml"):
        (tmp_path / f.name).write_bytes(f.read_bytes())
    (tmp_path / "broken.msg").write_bytes(b"not an ole file")
    report = LoadReport()
    recs = load_emails(tmp_path, since=datetime.now() + timedelta(days=1), report=report)
    assert recs == [] and len(report.too_old) == 6 and len(report.failed) == 1
    text = report.summary() + " ".join(report.hints("8주"))
    assert "기간 이전이라 제외: 6건" in text and "--lookback" in text and "broken.msg" in text


def test_cli_reports_zero_mails(tmp_path):
    (tmp_path / "x.pst").write_bytes(b"x")
    res = subprocess.run([sys.executable, "-m", "email_task_agent", str(tmp_path), "--list-emails"],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                         env={**os.environ, "PYTHONIOENCODING": "cp949"})
    assert res.returncode == 3
    assert ".pst" in res.stderr and "끌어다 놓아" in res.stderr


def _eml(path, subject, sent):
    from email.message import EmailMessage
    from email.utils import format_datetime

    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "a@corp.example", "me@corp.example", subject
    m["Message-ID"] = f"<{subject.encode().hex()}@t>"
    if sent is not None:  # None: Date 헤더 없음, str: 잘못된 Date 값
        m["Date"] = sent if isinstance(sent, str) else format_datetime(sent)
    m.set_content("본문")
    path.write_bytes(bytes(m))


def _cli(*args):
    return subprocess.run([sys.executable, "-m", "email_task_agent", *map(str, args)],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


def test_cli_default_lookback_is_two_years(tmp_path):
    _eml(tmp_path / "a.eml", "23개월전", datetime.now() - timedelta(days=700))
    _eml(tmp_path / "b.eml", "25개월전", datetime.now() - timedelta(days=760))
    res = _cli(tmp_path, "--list-emails")
    assert res.returncode == 0, res.stderr
    assert "23개월전" in res.stdout and "25개월전" not in res.stdout
    assert "최근 2년" in res.stderr and "2년이 지나 읽지 않음: 1건" in res.stderr


def test_mails_older_than_two_years_are_never_loaded(tmp_path, monkeypatch):
    monkeypatch.setattr(eml_parser, "now", lambda: datetime(2026, 10, 6, 12, 0))
    _eml(tmp_path / "a.eml", "recent", datetime(2025, 1, 1, 9, 0))
    _eml(tmp_path / "b.eml", "old", datetime(2024, 10, 5, 23, 0))  # 기준일 2년 전 하루 전
    report = LoadReport()
    # since/until 을 아무리 과거로 줘도 '현재' 기준 2년 상한이 우선
    recs = load_emails(tmp_path, since=datetime(2000, 1, 1), until=datetime(2025, 6, 1), report=report)
    assert [r.subject for r in recs] == ["recent"] and len(report.over_limit) == 1
    _eml(tmp_path / "c.eml", "undated", None)
    _eml(tmp_path / "d.eml", "baddate", "garbage")
    report = LoadReport()
    recs = load_emails(tmp_path, report=report)
    assert [r.subject for r in recs] == ["recent"]
    assert sorted(Path(p).name for p in report.no_date) == ["c.eml", "d.eml"]
    assert "날짜 정보가 없어 제외: 2건" in report.summary()
    for opt in (["--lookback", "3y"], ["--lookback", "25m"], ["--lookback-weeks", "200"]):
        res = _cli(tmp_path, "--list-emails", *opt)
        assert res.returncode == 2 and "최대 2년" in res.stderr


def test_cli_rejects_past_reference_date():
    past = _cli(MAILS, "--list-emails", "--date", REAL_TODAY - timedelta(days=1))
    assert past.returncode == 2 and "이전으로 지정할 수 없습니다" in past.stderr
    assert _cli(MAILS, "--list-emails", "--date", "2026/10/06").returncode == 2
    for d in (REAL_TODAY, REAL_TODAY + timedelta(days=7)):  # 오늘·미래 기준일은 허용
        assert _cli(MAILS, "--list-emails", "--date", d).returncode == 0


def test_parse_lookback():
    from datetime import timedelta
    assert parse_lookback("2y") == (timedelta(days=730), "2년")
    assert parse_lookback("6개월") == (timedelta(days=180), "6개월")
    assert parse_lookback("8") == (timedelta(weeks=8), "8주")
    with pytest.raises(Exception):
        parse_lookback("abc")


# ---------- planning ----------

def test_week_range():
    assert week_range(TODAY) == (date(2026, 10, 5), date(2026, 10, 11))


def test_expand_recurrence():
    weekly_fri = Recurrence(freq="weekly", weekday=4)
    assert expand_recurrence(weekly_fri, date(2026, 10, 5), date(2026, 10, 18)) == [
        date(2026, 10, 9), date(2026, 10, 16)]
    biweekly = Recurrence(freq="biweekly", weekday=0, anchor_date="2026-09-28")
    assert expand_recurrence(biweekly, date(2026, 10, 5), date(2026, 10, 18)) == [date(2026, 10, 12)]
    monthly = Recurrence(freq="monthly", day_of_month=31)
    assert expand_recurrence(monthly, date(2026, 9, 1), date(2026, 10, 31)) == [
        date(2026, 9, 30), date(2026, 10, 31)]


def test_build_checklist_buckets():
    tasks = [
        Task(title="지난 일", due_date="2026-09-30"),
        Task(title="어제 마감", due_date="2026-10-05"),
        Task(title="이번주", due_date="2026-10-08", priority="high"),
        Task(title="다음주", due_date="2026-10-15"),
        Task(title="차차주", due_date="2026-10-20"),
        Task(title="완료됨", due_date="2026-10-07", status="done"),
        Task(title="기한없음"),
        Task(title="주간보고", recurrence=Recurrence(freq="weekly", weekday=4)),
    ]
    cl = build_checklist(tasks, TODAY)
    titles = lambda items: [i.task.title for i in items]  # noqa: E731
    assert titles(cl.overdue) == ["지난 일", "어제 마감"]
    assert titles(cl.this_week_items) == ["이번주", "주간보고"]
    assert titles(cl.next_week_items) == ["다음주", "주간보고"]
    assert titles(cl.undated) == ["기한없음"]
    md = to_markdown(cl, 3)
    assert "이번 주 할 일 (2)" in md and "🔁 반복" in md


def test_prune_stale_drops_old_items():
    tasks = [
        Task(title="1년 전 마감", due_date="2025-10-01"),
        Task(title="한달 전 마감", due_date="2026-09-01"),
        Task(title="기한 없음 옛날", last_mail_date="2025-03-02"),
        Task(title="기한 없음 최근", last_mail_date="2026-09-20"),
        Task(title="끊긴 주간보고", last_mail_date="2025-06-06", recurrence=Recurrence(freq="weekly", weekday=4)),
        Task(title="월간정산", last_mail_date="2026-07-31", recurrence=Recurrence(freq="monthly", day_of_month=31)),
        Task(title="날짜정보 없음"),
    ]
    assert [t.title for t in prune_stale(tasks, TODAY, 8)] == [
        "한달 전 마감", "기한 없음 최근", "월간정산", "날짜정보 없음"]


def test_extract_json_handles_think_and_fences():
    assert extract_json('<think>{"x": 0} 고민</think>\n```json\n{"tasks": []}\n```') == {"tasks": []}
    assert extract_json('추론...</think>{"tasks": [1]}') == {"tasks": [1]}


# ---------- end-to-end against a mock vLLM server ----------

class _MockVLLM(BaseHTTPRequestHandler):
    requests: list = []
    reject_schema = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        if self.reject_schema and "response_format" in body:
            return self._send(400, {"error": {"message": "guided decoding not supported"}})
        user = body["messages"][-1]["content"]
        import re as _re
        today = date.fromisoformat(_re.search(r"오늘 날짜: (\S+)", body["messages"][0]["content"]).group(1))
        mon = today - timedelta(days=today.weekday())
        if "업무 목록을 정리" in user:
            tasks = json.loads(user.split("\n\n", 1)[1])["tasks"]
            payload = {"tasks": list({t["title"]: t for t in tasks}.values())}
        else:
            payload = {"tasks": []}
            if "Q3 실적" in user:
                payload["tasks"].append({"title": "Q3 실적 보고서 초안 송부", "due_date": str(mon + timedelta(days=3)),
                                         "priority": "high", "source_subjects": ["Q3 실적 보고서 작성 요청"]})
            if "주간보고" in user:
                payload["tasks"].append({"title": "주간보고 업로드",
                                         "recurrence": {"freq": "weekly", "weekday": 4}})
            if "단가표" in user:
                payload["tasks"].append({"title": "단가표 수정본 회신", "due_date": str(mon + timedelta(days=9))})
        content = "<think>분석 중</think>" + json.dumps(payload, ensure_ascii=False)
        self._send(200, {
            "id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
        })

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def mock_server():
    _MockVLLM.requests = []
    server = HTTPServer(("127.0.0.1", 0), _MockVLLM)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


@pytest.mark.parametrize("reject_schema", [False, True])
def test_end_to_end_with_batches(mock_server, reject_schema):
    _MockVLLM.reject_schema = reject_schema
    llm = LLMClient(base_url=mock_server, model="thinkingcap", max_retries=0)
    agent = EmailTaskAgent(llm, me="김대리", batch_chars=400, log=lambda m: None)
    cl, tasks = agent.run(load_emails(MAILS), today=REAL_TODAY)

    assert _MockVLLM.requests[0]["model"] == "thinkingcap"
    assert any("업무 목록을 정리" in r["messages"][-1]["content"] for r in _MockVLLM.requests)  # 병합 단계
    assert [i.task.title for i in cl.this_week_items] == ["Q3 실적 보고서 초안 송부", "주간보고 업로드"]
    assert [i.task.title for i in cl.next_week_items] == ["단가표 수정본 회신", "주간보고 업로드"]
    _MockVLLM.reject_schema = False


def test_chunked_merge_for_large_candidate_lists(mock_server):
    llm = LLMClient(base_url=mock_server, model="thinkingcap", max_retries=0)
    agent = EmailTaskAgent(llm, merge_chars=1500, log=lambda m: None)
    tasks = [Task(title=f"업무 {i % 6}", due_date="2026-10-08") for i in range(30)]
    merged = agent.merge_tasks(tasks, TODAY)
    merge_calls = [r for r in _MockVLLM.requests if "업무 목록을 정리" in r["messages"][-1]["content"]]
    assert len(merge_calls) > 1  # 한 번에 넣기엔 커서 여러 묶음으로 나눠 병합
    assert sorted(t.title for t in merged) == [f"업무 {i}" for i in range(6)]


def test_cli_writes_markdown(mock_server, tmp_path):
    out = tmp_path / "checklist.md"
    res = _cli(MAILS, "--base-url", mock_server, "-o", out)
    assert res.returncode == 0, res.stderr
    text = out.read_text(encoding="utf-8")
    assert "Q3 실적 보고서 초안 송부" in text and "다음 주 할 일" in text
