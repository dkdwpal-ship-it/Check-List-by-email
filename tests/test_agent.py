import json
import subprocess
import sys
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from email_task_agent import EmailTaskAgent, LLMClient, build_checklist, load_emails, parse_eml
from email_task_agent.agent import expand_recurrence, week_range
from email_task_agent.llm import extract_json
from email_task_agent.models import Recurrence, Task
from email_task_agent.render import to_markdown

ROOT = Path(__file__).resolve().parents[1]
MAILS = ROOT / "samples" / "mails"
TODAY = date(2026, 10, 6)  # 화요일


@pytest.fixture(scope="session", autouse=True)
def samples():
    subprocess.run([sys.executable, str(ROOT / "samples" / "make_samples.py")], check=True)


# ---------- parser ----------

def test_parse_html_and_euckr():
    html_mail = parse_eml(MAILS / "02.eml")
    assert "다음주 목요일까지" in html_mail.body and "<b>" not in html_mail.body
    euckr = parse_eml(MAILS / "03.eml")
    assert "10월 14일까지" in euckr.body
    assert "Original Message" not in euckr.body  # 인용 본문 제거
    assert euckr.date.isoformat().startswith("2026-10-05T09:05")  # 발신자 현지 시각 유지


def test_load_emails_window_and_dedupe(tmp_path):
    for f in MAILS.glob("*.eml"):
        (tmp_path / f.name).write_bytes(f.read_bytes())
    (tmp_path / "dup.eml").write_bytes((MAILS / "01.eml").read_bytes())
    from datetime import datetime
    recs = load_emails(tmp_path, since=datetime(2026, 9, 20))
    assert len(recs) == 5  # 9/11 메일은 기간 밖, 중복 제거
    assert [r.date for r in recs] == sorted(r.date for r in recs)


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
        if "업무 목록을 정리" in user:
            tasks = json.loads(user.split("\n\n", 1)[1])["tasks"]
            payload = {"tasks": list({t["title"]: t for t in tasks}.values())}
        else:
            payload = {"tasks": []}
            if "Q3 실적" in user:
                payload["tasks"].append({"title": "Q3 실적 보고서 초안 송부", "due_date": "2026-10-08",
                                         "priority": "high", "source_subjects": ["Q3 실적 보고서 작성 요청"]})
            if "주간보고" in user:
                payload["tasks"].append({"title": "주간보고 업로드",
                                         "recurrence": {"freq": "weekly", "weekday": 4}})
            if "단가표" in user:
                payload["tasks"].append({"title": "단가표 수정본 회신", "due_date": "2026-10-14"})
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
    cl, tasks = agent.run(load_emails(MAILS), today=TODAY)

    assert _MockVLLM.requests[0]["model"] == "thinkingcap"
    assert any("업무 목록을 정리" in r["messages"][-1]["content"] for r in _MockVLLM.requests)  # 병합 단계
    assert [i.task.title for i in cl.this_week_items] == ["Q3 실적 보고서 초안 송부", "주간보고 업로드"]
    assert [i.task.title for i in cl.next_week_items] == ["단가표 수정본 회신", "주간보고 업로드"]
    _MockVLLM.reject_schema = False


def test_cli_writes_markdown(mock_server, tmp_path):
    out = tmp_path / "checklist.md"
    res = subprocess.run(
        [sys.executable, "-m", "email_task_agent", str(MAILS), "--base-url", mock_server,
         "--date", "2026-10-06", "-o", str(out)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    text = out.read_text(encoding="utf-8")
    assert "Q3 실적 보고서 초안 송부" in text and "다음 주 할 일" in text
