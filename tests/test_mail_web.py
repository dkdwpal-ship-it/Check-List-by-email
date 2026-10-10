import json
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mail_web  # noqa: E402


class FakeVLLM(BaseHTTPRequestHandler):
    """사내 vLLM 흉내. 요청 기록, 메일 ID 마다 할 일·요약 반환."""
    chats: list = []
    auth: list = []

    def log_message(self, *a):
        pass

    def _send(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send({"data": [{"id": "thinkingcap"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).chats.append(body)
        type(self).auth.append(self.headers.get("Authorization"))
        user = body["messages"][-1]["content"]
        today = datetime.now().date()
        mon = today - timedelta(days=today.weekday())
        tasks, mails = [], []
        for mid, subject in re.findall(r"^\[(M\d+)\].*\n제목: (.*)", user, re.M):
            mails.append({"id": mid, "summary": f"요약: {subject}", "keywords": ["견적"]})
            if "견적" in subject:
                tasks.append({"title": "견적 회신하기", "due": str(mon + timedelta(days=9)), "priority": "high", "done": False, "mail": mid})
            if "보고" in subject:
                tasks.append({"title": "보고서 제출하기", "due": str(today), "priority": "medium", "done": False, "mail": mid})
        self._send({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"tasks": tasks, "mails": mails}, ensure_ascii=False)}}]})


def eml(subject, when, sender="박팀장 <park@corp.example>", extra=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "김대리 <me@corp.example>", subject
    m["Message-ID"] = f"<{abs(hash((subject, str(when))))}@t>"
    if when is not None:
        m["Date"] = format_datetime(when)
    for k, v in (extra or {}).items():
        m[k] = v
    m.set_content(f"{subject} 부탁드립니다.\n\n-----Original Message-----\n이전 메일 내용")
    return bytes(m)


@pytest.fixture
def web(tmp_path, monkeypatch):
    FakeVLLM.chats, FakeVLLM.auth = [], []
    llm = ThreadingHTTPServer(("127.0.0.1", 0), FakeVLLM)
    threading.Thread(target=llm.serve_forever, daemon=True).start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # PC 에 프록시가 있어도 LLM 은 직접 연결해야 함
    for k in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(k, raising=False)
    server, app = mail_web.serve(0, base_url=f"http://127.0.0.1:{llm.server_port}/v1", cache_path=tmp_path / "cache.json")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", app
    server.shutdown()
    llm.shutdown()
    shutil.rmtree(app.store.dir, ignore_errors=True)


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 테스트 클라이언트는 프록시 없이


def call(base, method, path, body=None, headers=None):
    req = urllib.request.Request(base + path, data=body, method=method, headers=headers or {})
    try:
        with _DIRECT.open(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def upload(base, name, data):
    return call(base, "POST", "/api/upload", data, {"X-File-Name": quote(name)})


def run(base, **opts):
    code, r = call(base, "POST", "/api/analyze", json.dumps(opts).encode())
    assert code == 200, r
    for _ in range(100):
        _, s = call(base, "GET", f"/api/jobs/{r['job']}")
        if s["state"] != "running":
            return s
        time.sleep(0.05)
    raise AssertionError("timeout")


def test_upload_analyze_and_cache(web):
    base, app = web
    now = datetime.now()
    assert upload(base, "견적.eml", eml("A사 견적 요청", now - timedelta(days=2)))[1]["status"] == "ok"
    assert upload(base, "보고.eml", eml("주간 보고 요청", now - timedelta(days=1)))[1]["status"] == "ok"
    noti = upload(base, "noti.eml", eml("시스템 점검 알림", now - timedelta(days=1), sender="no-reply@sys.example"))[1]
    assert noti["status"] == "ok" and "자동 알림" in noti["reason"]
    assert upload(base, "nodate.eml", eml("날짜 없는 메일", None))[1]["status"] == "nodate"
    assert upload(base, "old.eml", eml("3년 전 메일", now - timedelta(days=1100)))[1]["status"] == "old"
    assert upload(base, "dup.eml", eml("A사 견적 요청", now - timedelta(days=2)))[1].get("duplicate")
    assert upload(base, "x.msg", b"x")[0] == 415

    s = run(base, me="김대리", weeks=4)
    assert s["state"] == "done", s
    b = s["result"]["buckets"]
    assert [t["title"] for t in b["next"]] == ["견적 회신하기"] and b["next"][0]["priority"] == "high"
    assert [t["title"] for t in b["today"]] == ["보고서 제출하기"]
    mails = s["result"]["mails"]
    assert len(mails) == 3 and any(m["noise"] for m in mails)
    sent = "".join(c["messages"][-1]["content"] for c in FakeVLLM.chats)
    assert "시스템 점검 알림" not in sent                        # 자동 알림은 LLM 에 보내지 않음
    assert "이전 메일 내용" not in sent                           # 회신 인용 본문 제거
    assert all(a is None for a in FakeVLLM.auth)                  # API 키 없음 → Authorization 헤더 없음
    assert all(c["chat_template_kwargs"] == {"enable_thinking": False} for c in FakeVLLM.chats)
    assert FakeVLLM.chats[0]["model"] == "thinkingcap"

    # 같은 메일로 다시 분석하면 저장된 결과를 써서 LLM 호출 없음
    n = len(FakeVLLM.chats)
    s2 = run(base, me="김대리", weeks=4)
    assert len(FakeVLLM.chats) == n and s2["result"]["buckets"] == s["result"]["buckets"]
    # 원문 보기: 인용 본문 포함
    _, m = call(base, "GET", f"/api/mails/{b['next'][0]['mail']}")
    assert m["subject"] == "A사 견적 요청" and "이전 메일 내용" in m["body"]


def test_rules_and_security(web):
    base, _ = web
    assert call(base, "POST", "/api/analyze", json.dumps({"weeks": 105}).encode())[0] == 400   # 최대 2년
    assert call(base, "POST", "/api/analyze", json.dumps({"weeks": 4}).encode())[0] == 400     # 메일 없음
    assert call(base, "POST", "/api/clear", b"", {"Origin": "https://evil.example"})[0] == 403  # 다른 사이트 요청 차단
    assert call(base, "GET", "/api/mails/../../etc")[0] == 404
    code, cfg = call(base, "GET", "/api/config")
    assert cfg["model"] == "thinkingcap" and cfg["weeks"] == 4 and cfg["max_weeks"] == 104
    assert call(base, "GET", "/api/check")[1]["ok"]


def test_default_llm_settings():
    assert mail_web.BASE_URL == "http://75.12.15.121:8000/v1" or "LLM_BASE_URL" in __import__("os").environ
    assert mail_web.MODEL == "thinkingcap" or "LLM_MODEL" in __import__("os").environ
    assert mail_web.API_KEY == "" or "LLM_API_KEY" in __import__("os").environ


def test_two_years_ago_handles_leap_day():
    assert mail_web.two_years_ago(datetime(2028, 2, 29, 9)) == datetime(2026, 2, 28)


def test_korean_and_received_dates():
    assert mail_web.parse_date("2026년 3월 4일 화요일 오후 2:30") == datetime(2026, 3, 4, 14, 30)
    data = b"From: a@b.c\nSubject: s\nReceived: from x by y; Tue, 4 Mar 2026 09:00:00 +0900\n\nbody\n"
    assert mail_web.read_eml(data)["date"] == datetime(2026, 3, 4, 9, 0)


def test_page_scripts_are_valid():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 없음")
    page = (ROOT / "index.html").read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert len(blocks) == 2 and "https://" not in page.replace("http://www.w3.org", "")
    for code in blocks:
        res = subprocess.run([node, "--check", "-"], input=code, capture_output=True, text=True)
        assert res.returncode == 0, res.stderr


def test_starts_without_any_packages():
    res = subprocess.run([sys.executable, "-I", "-S", str(ROOT / "mail_web.py"), "--help"], capture_output=True, text=True, encoding="utf-8")
    assert res.returncode == 0 and "--port" in res.stdout
