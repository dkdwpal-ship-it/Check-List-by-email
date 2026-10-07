"""실제 사내 vLLM·VS Code 환경에서 '대시보드가 제대로 동작하지 않던' 문제들의 회귀 테스트."""
import json
import re
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from email_task_agent import EmailTaskAgent, LLMClient, load_emails
from email_task_agent.llm import parse_context_error

ROOT = Path(__file__).resolve().parents[1]


class _RealisticVLLM(BaseHTTPRequestHandler):
    """vLLM 흉내: max_model_len 초과 시 400, 메일이 많으면 응답이 max_tokens 에서 잘림."""
    max_len = 10500
    trunc_over = 3
    requests: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        prompt = sum(len(m["content"]) for m in body["messages"]) // 2
        if prompt + body["max_tokens"] > self.max_len:
            return self._send(400, {"object": "error", "code": 400, "message": (
                f"This model's maximum context length is {self.max_len} tokens. However, you requested "
                f"{prompt + body['max_tokens']} tokens ({prompt} in the messages, {body['max_tokens']} in the completion).")})
        user = body["messages"][-1]["content"]
        ids = re.findall(r"\[메일 ID\] (M\d+)", user)
        if "업무 목록을 정리" in user:
            content, finish = json.dumps({"tasks": json.loads(user.split("\n\n", 1)[1])["tasks"]}), "stop"
        elif len(ids) > self.trunc_over:
            content, finish = '<think>생각</think>{"tasks": [], "summaries": [{"mail_id": "M1", "su', "length"
        else:
            content = json.dumps({"tasks": [{"title": f"업무 {i}"} for i in ids[:1]],
                                  "summaries": [{"mail_id": i, "summary": f"{i} 요약"} for i in ids]})
            finish = "stop"
        self._send(200, {"id": "x", "object": "chat.completion", "created": 0, "model": "thinkingcap",
                         "choices": [{"index": 0, "finish_reason": finish,
                                      "message": {"role": "assistant", "content": content}}]})


@pytest.fixture
def vllm():
    _RealisticVLLM.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RealisticVLLM)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


def test_parse_context_error():
    msg = ("This model's maximum context length is 8192 tokens. However, you requested 12000 tokens "
           "(3808 in the messages, 8192 in the completion).")
    assert parse_context_error(msg) == (8192, 3808)


def test_small_context_and_truncation_recover_automatically(vllm, sample_mails):
    """max_model_len 이 작고 응답이 잘려도 전체 작업이 실패하거나 빈 결과가 되지 않아야 함."""
    records = load_emails(sample_mails)
    logs = []
    agent = EmailTaskAgent(LLMClient(base_url=vllm, max_retries=0), log=logs.append)
    _, tasks = agent.run(records)
    assert len(agent.summaries) == len(records) == 6        # 모든 메일 요약 확보
    assert tasks and not agent.warnings
    assert any(r["max_tokens"] < 8192 for r in _RealisticVLLM.requests)  # 응답 길이를 줄여 재시도
    assert any("나눠 다시 요청" in line for line in logs)                 # 잘린 응답 → 묶음 분할


def test_unprocessable_mail_is_skipped_not_fatal(vllm, sample_mails):
    _RealisticVLLM.max_len, _RealisticVLLM.trunc_over = 2400, 3   # 시스템 프롬프트만으로도 거의 꽉 참
    try:
        records = load_emails(sample_mails)
        agent = EmailTaskAgent(LLMClient(base_url=vllm, max_retries=0), log=lambda m: None)
        agent.run(records)  # 예외 없이 끝나야 함
        assert any("분석하지 못한 메일" in w for w in agent.warnings)
    finally:
        _RealisticVLLM.max_len, _RealisticVLLM.trunc_over = 10500, 3


@pytest.mark.parametrize("cmd", [
    ["email_task_agent/web.py", "--help"],          # VS Code ▶ 로 파일 직접 실행
    ["email_task_agent/cli.py", "--help"],
    ["run_web.py", "--help"],
])
def test_scripts_run_directly_from_any_folder(cmd, tmp_path):
    res = subprocess.run([sys.executable, str(ROOT / cmd[0]), *cmd[1:]], cwd=tmp_path,
                         capture_output=True, text=True, encoding="utf-8")
    assert res.returncode == 0, res.stderr
    assert "usage" in res.stdout


def test_web_moves_to_next_port_when_busy(tmp_path):
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    port = busy.getsockname()[1]
    try:
        proc = subprocess.Popen([sys.executable, str(ROOT / "run_web.py"), "--no-browser", "--port", str(port)],
                                cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        lines = [proc.stdout.readline() for _ in range(2)]
        proc.terminate()
        proc.wait(timeout=10)
    finally:
        busy.close()
    assert "사용 중" in lines[0] and "http://127.0.0.1:" in lines[1]
