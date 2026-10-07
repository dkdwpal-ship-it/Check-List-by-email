"""사내 프록시가 설정된 PC 에서 LLM 서버 403 이 나던 문제 회귀 테스트."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from email_task_agent.llm import LLMClient, LLMError
from email_task_agent.models import TaskList


class _Proxy(BaseHTTPRequestHandler):  # 모든 요청을 403 으로 막는 사내 프록시
    hits = 0

    def log_message(self, *a):
        pass

    def _deny(self):
        type(self).hits += 1
        body = b"<html><h1>403 Forbidden</h1>blocked by proxy</html>"
        self.send_response(403)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_CONNECT = _deny


class _VLLM(BaseHTTPRequestHandler):
    auth: list = []

    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).auth.append(self.headers.get("Authorization"))
        self._send({"object": "list", "data": [{"id": "thinkingcap", "object": "model"}]})

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        type(self).auth.append(self.headers.get("Authorization"))
        self._send({"id": "x", "object": "chat.completion", "created": 0, "model": "thinkingcap",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": '{"tasks": []}'}}]})


@pytest.fixture
def corp_network(monkeypatch):
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), h) for h in (_Proxy, _VLLM)]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    proxy, vllm = (f"http://127.0.0.1:{s.server_port}" for s in servers)
    for name in ("NO_PROXY", "no_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
                 "LLM_USE_PROXY", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", proxy)  # PC 에 사내 프록시가 설정된 상태
    monkeypatch.setenv("http_proxy", proxy)
    _Proxy.hits, _VLLM.auth = 0, []
    yield vllm + "/v1"
    for s in servers:
        s.shutdown()


def test_default_connects_directly_without_auth_header(corp_network):
    llm = LLMClient(base_url=corp_network, max_retries=0)
    assert llm.chat_structured("s", "u", TaskList).tasks == []
    ok, lines = llm.check_connection()
    assert ok and "직접 연결" in "\n".join(lines)
    assert _Proxy.hits == 0
    assert _VLLM.auth and all(a is None for a in _VLLM.auth)  # 'Bearer EMPTY' 를 보내지 않음


def test_api_key_is_sent_when_configured(corp_network, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-internal")
    LLMClient(base_url=corp_network, max_retries=0).chat_structured("s", "u", TaskList)
    assert _VLLM.auth == ["Bearer sk-internal"]


def test_proxy_403_is_explained(corp_network):
    llm = LLMClient(base_url=corp_network, max_retries=0, use_proxy=True)
    with pytest.raises(LLMError) as exc:
        llm.chat_structured("s", "u", TaskList)
    msg = str(exc.value)
    assert _Proxy.hits == 1
    assert "403" in msg and "프록시" in msg and "blocked by proxy" in msg and "--check-llm" in msg
