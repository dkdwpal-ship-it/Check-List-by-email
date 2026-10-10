"""라이트 버전(lite/mail_lite.py) 테스트 — 외부 패키지 없이 동작하는지, 규칙(2년·날짜 없음)과 분할 처리."""
import json
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LITE = ROOT / "lite" / "mail_lite.py"
sys.path.insert(0, str(LITE.parent))
import mail_lite  # noqa: E402


class _VLLM(BaseHTTPRequestHandler):
    max_mails = 99      # 이보다 많은 메일이 오면 응답이 잘렸다고(finish_reason=length) 흉내
    requests: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        ids = re.findall(r"^\[(M\d+)\]", body["messages"][-1]["content"], re.M)
        today = datetime.now().date()
        if len(ids) > self.max_mails:
            content, finish = '{"tasks": [', "length"
        else:
            content = json.dumps({
                "tasks": [{"title": f"{i} 회신하기", "due": str(today + timedelta(days=1)), "priority": "high",
                           "done": False, "mail": i} for i in ids],
                "mails": [{"id": i, "summary": f"{i} 요약", "keywords": ["견적"]} for i in ids]}, ensure_ascii=False)
            finish = "stop"
        data = json.dumps({"choices": [{"finish_reason": finish, "message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def server(monkeypatch):
    _VLLM.requests, _VLLM.max_mails = [], 99
    s = ThreadingHTTPServer(("127.0.0.1", 0), _VLLM)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # 프록시가 설정돼 있어도 직접 연결해야 함
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    yield f"http://127.0.0.1:{s.server_port}/v1"
    s.shutdown()


def _mail(folder, name, subject, when, sender="박팀장 <park@corp.example>"):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "me@corp.example", subject
    m["Message-ID"] = f"<{name}@t>"
    if when is not None:
        m["Date"] = format_datetime(when)
    m.set_content(f"{subject} 확인 부탁드립니다.\n\n-----Original Message-----\n이전 메일")
    (folder / name).write_bytes(bytes(m))


def test_lite_uses_only_standard_library():
    src = LITE.read_text(encoding="utf-8")
    imports = set(re.findall(r"^\s*(?:import|from) (\w+)", src, re.M))
    assert imports <= {"__future__", "argparse", "email", "html", "json", "os", "re", "sys", "time", "urllib",
                       "webbrowser", "concurrent", "datetime", "pathlib", "tkinter", "olefile", "struct"}


def test_lite_end_to_end(server, tmp_path):
    now = datetime.now()
    for i in range(6):
        _mail(tmp_path, f"m{i}.eml", f"견적 요청 {i}", now - timedelta(days=i + 1))
    _mail(tmp_path, "old.eml", "3년 전", now - timedelta(days=3 * 365))
    _mail(tmp_path, "nodate.eml", "헤더에날짜없는메일", None)
    _mail(tmp_path, "noti.eml", "시스템 알림", now - timedelta(days=1), sender="no-reply@sys.example")
    _VLLM.max_mails = 2   # 한 번에 3건 이상이면 잘림 → 자동으로 나눠 요청해야 함
    out = tmp_path / "out.html"
    rc = mail_lite.main([str(tmp_path), "--base-url", server, "--no-open", "-o", str(out), "--weeks", "104"])
    assert rc == 0
    html = out.read_text(encoding="utf-8")
    sent = [m for r in _VLLM.requests for m in re.findall(r"^\[(M\d+)\]", r["messages"][-1]["content"], re.M)]
    assert "3년 전" not in html and "헤더에날짜없는메일" not in html   # 2년 초과·날짜 없음 제외
    assert "날짜 없음 1" in html and "기간 밖 1" in html             # 제외 건수는 표시
    assert "시스템 알림" in html and "자동 알림" in html              # 알림 메일은 목록에만, LLM 에는 안 보냄
    assert len(set(sent)) == 6 and all(len(re.findall(r"^\[M", r["messages"][-1]["content"], re.M)) <= 6 for r in _VLLM.requests)
    assert html.count("회신하기") == 6 and "M1 요약" in html and "Original Message" in html  # 원문은 인용 포함
    assert all(r.get("chat_template_kwargs") == {"enable_thinking": False} for r in _VLLM.requests)


def test_lite_rejects_more_than_two_years(tmp_path):
    assert mail_lite.main([str(tmp_path), "--weeks", "105", "--no-open"]) == 2


def test_lite_runs_as_script_without_packages(tmp_path):
    # 외부 패키지를 못 찾게 한 깨끗한 Python(-I -S)에서도 시작 가능해야 함
    res = subprocess.run([sys.executable, "-I", "-S", str(LITE), "--help"], capture_output=True, text=True, encoding="utf-8")
    assert res.returncode == 0 and "--weeks" in res.stdout
