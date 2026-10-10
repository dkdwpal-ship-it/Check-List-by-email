import json
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import todo_list  # noqa: E402


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
        if "종합해 하나의 업무 리스트" in body["messages"][0]["content"]:   # 종합 요청
            rows = re.findall(r"^\[(T\d+)\] ([^|]+?) \|", user, re.M)
            by = {}
            for tid, title in rows:
                if "보고서" not in title:              # 일부러 하나 빠뜨림 → '기타'로 들어가야 함
                    by.setdefault(title, []).append(tid)
            items = [{"title": t, "due": None, "priority": "high", "status": "doing", "tasks": ids, "note": f"출처 {len(ids)}곳 종합"} for t, ids in by.items()]
            items.append({"title": "지어낸 일", "due": None, "priority": "low", "status": "todo", "tasks": ["T99"], "note": ""})
            out = {"summary": "종합 요약", "groups": [{"topic": "고객사", "items": items}]}
            return self._send({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(out, ensure_ascii=False)}}]})
        today = date.today()
        mon = today - timedelta(days=today.weekday())
        tasks, mails = [], []
        for mid, subject in re.findall(r"^\[(M\d+)\].*\n제목: (.*)", user, re.M):
            if "누락" in subject:   # 모델이 응답에서 빠뜨리는 경우 흉내
                continue
            mails.append({"id": mid, "summary": f"요약: {subject}", "keywords": ["견적"]})
            if "예산" in subject:
                tasks.append({"title": "내년 예산안 제출하기", "due": None, "priority": "high", "done": True, "mail": mid})
            if "견적" in subject:
                tasks.append({"title": "견적 회신하기", "due": str(mon + timedelta(days=9)), "priority": "high", "done": False, "mail": mid})
            if "보고" in subject:
                tasks.append({"title": "보고서 제출하기", "due": str(today), "priority": "medium", "done": False, "mail": mid})
            if "결산" in subject:   # 지난 주 기한, 메일에서 완료 확인됨
                tasks.append({"title": "월 결산 자료 보내기", "due": str(mon - timedelta(days=4)), "priority": "medium", "done": True, "mail": mid})
            if "회의록" in subject:  # 지난 주 기한, 완료 확인 안 됨
                tasks.append({"title": "회의록 공유하기", "due": str(mon - timedelta(days=3)), "priority": "low", "done": False, "mail": mid})
            if "연간 계획" in subject:   # 다음 달 기한
                nm = (today.replace(day=1) + timedelta(days=32)).replace(day=1) + timedelta(days=4)
                tasks.append({"title": "연간 계획 제출하기", "due": str(nm), "priority": "high", "done": False, "mail": mid})
            if "워크숍" in subject:
                tasks.append({"title": "워크숍 장소 예약하기", "due": None, "priority": "low", "done": False, "mail": mid})
            if "점검" in subject:
                tasks.append({"title": "설비 점검 보고하기", "due": None, "priority": "medium", "done": False, "mail": mid})
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
    server, app = todo_list.serve(0, base_url=f"http://127.0.0.1:{llm.server_port}/v1", cache_path=tmp_path / "cache.json")
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
    far = upload(base, "예전.eml", eml("작년 예산 메일", now - timedelta(days=400)))[1]
    assert far["status"] == "ok"                                       # 날짜 제한 없음 (범위 밖이라 분석만 안 함)
    assert upload(base, "nodate.eml", eml("날짜 없는 메일", None))[1]["status"] == "nodate"
    assert upload(base, "dup.eml", eml("A사 견적 요청", now - timedelta(days=2)))[1].get("duplicate")
    assert upload(base, "x.msg", b"x")[0] == 415

    s = run(base, me="김대리", weeks=4)
    assert s["state"] == "done", s
    r = s["result"]
    assert set(r) >= {"combined", "mails", "skipped", "weeks", "today"} and "season" not in r and "deadline" not in r
    mails = {m["subject"]: m for m in r["mails"]}
    assert set(mails) == {"A사 견적 요청", "주간 보고 요청", "시스템 점검 알림"}
    assert [t["title"] for t in mails["A사 견적 요청"]["task_list"]] == ["견적 회신하기"] and mails["시스템 점검 알림"]["noise"]
    sk = {x["subject"]: x["type"] for x in r["skipped"]}
    assert sk == {"작년 예산 메일": "range", "시스템 점검 알림": "noise", "날짜 없는 메일": "nodate"}
    c = r["combined"]
    assert {i["title"] for g in c["groups"] for i in g["items"]} == {"견적 회신하기", "보고서 제출하기"}
    # LLM 요청: 메일 분석 + 종합 1번뿐 (기한 기준·작년 이맘때 요약 없음)
    kinds = [c["messages"][0]["content"][:12] for c in FakeVLLM.chats]
    assert sum(k.startswith("업무 메일에서") for k in kinds) >= 1 and sum("종합해" in c["messages"][0]["content"] for c in FakeVLLM.chats) == 1
    assert len(FakeVLLM.chats) == sum(k.startswith("업무 메일에서") for k in kinds) + 1
    sent = "".join(c["messages"][-1]["content"] for c in FakeVLLM.chats)
    assert "시스템 점검 알림" not in sent and "작년 예산 메일" not in sent  # 자동 알림·범위 밖은 LLM 에 보내지 않음
    assert "이전 메일 내용" not in sent                           # 회신 인용 본문 제거
    assert all(a is None for a in FakeVLLM.auth)                  # API 키 없음 → Authorization 헤더 없음
    assert all(c["chat_template_kwargs"] == {"enable_thinking": False} for c in FakeVLLM.chats)
    assert FakeVLLM.chats[0]["model"] == "thinkingcap"

    # 같은 메일로 다시 분석하면 저장된 결과를 써서 LLM 호출 없음
    n = len(FakeVLLM.chats)
    s2 = run(base, me="김대리", weeks=4)
    assert len(FakeVLLM.chats) == n and s2["result"]["combined"] == c and s2["result"]["mails"] == r["mails"]
    # 범위 '전체'면 오래된 메일도 분석
    s3 = run(base, me="김대리", weeks=0)
    assert "작년 예산 메일" in {m["subject"] for m in s3["result"]["mails"]}
    # 원문 보기: 인용 본문 포함
    _, m = call(base, "GET", f"/api/mails/{mails['A사 견적 요청']['id']}")
    assert m["subject"] == "A사 견적 요청" and "이전 메일 내용" in m["body"]


def test_rules_and_security(web):
    base, _ = web
    assert call(base, "POST", "/api/analyze", json.dumps({"weeks": -1}).encode())[0] == 400    # 범위 오류
    assert call(base, "POST", "/api/analyze", b"{}")[0] == 400                                  # 메일 없음
    upload(base, "올해.eml", eml("올해 메일", datetime.now() - timedelta(days=200)))
    code, r = call(base, "POST", "/api/analyze", b"{}")
    assert code == 400 and "최근 4주" in r["error"] and "전체" in r["error"]
    assert call(base, "POST", "/api/clear", b"", {"Origin": "https://evil.example"})[0] == 403  # 다른 사이트 요청 차단
    assert call(base, "GET", "/api/mails/../../etc")[0] == 404
    code, cfg = call(base, "GET", "/api/config")
    assert cfg["model"] == "thinkingcap" and "periods" not in cfg and cfg["weeks"] == 4
    assert call(base, "GET", "/api/check")[1]["ok"]


def test_default_llm_settings():
    assert todo_list.BASE_URL == "http://75.12.15.121:8000/v1" or "LLM_BASE_URL" in __import__("os").environ
    assert todo_list.MODEL == "thinkingcap" or "LLM_MODEL" in __import__("os").environ
    assert todo_list.API_KEY == "" or "LLM_API_KEY" in __import__("os").environ


def test_no_date_limit_and_all_range(web):
    base, _ = web
    now = datetime.now()
    r = upload(base, "아주예전.eml", eml("주간 보고 요청 (5년 전)", now - timedelta(days=5 * 365)))[1]
    assert r["status"] == "ok"                                         # 5년 전 메일도 받음
    s = run(base, me="김대리", weeks=0)                                 # 범위 '전체'
    assert s["state"] == "done", s
    assert [m["id"] for m in s["result"]["mails"]] == [r["id"]] and not s["result"]["skipped"]


def test_korean_and_received_dates():
    assert todo_list.parse_date("2026년 3월 4일 화요일 오후 2:30") == datetime(2026, 3, 4, 14, 30)
    data = b"From: a@b.c\nSubject: s\nReceived: from x by y; Tue, 4 Mar 2026 09:00:00 +0900\n\nbody\n"
    assert todo_list.read_eml(data)["date"] == datetime(2026, 3, 4, 9, 0)


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
    res = subprocess.run([sys.executable, "-I", "-S", str(ROOT / "todo_list.py"), "--help"], capture_output=True, text=True, encoding="utf-8")
    assert res.returncode == 0 and "--port" in res.stdout
