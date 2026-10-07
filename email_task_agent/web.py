"""Local web UI: drag .eml/.msg files into the browser and get this/next week's checklist.

    python -m email_task_agent.web            # http://127.0.0.1:8765

Uses only the standard library HTTP server and loads nothing from the internet, so it works inside
closed company networks. Uploaded mails are kept in a temporary folder only while the server runs.
"""

from __future__ import annotations

import argparse
import atexit
import json
import re
import shutil
import sys
import tempfile
import threading
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from .agent import EmailTaskAgent
from .cli import resolve_window
from .eml_parser import SUPPORTED_EXTS, LoadReport, load_emails
from .llm import DEFAULT_BASE_URL, DEFAULT_MODEL, LLMClient, LLMError
from .render import to_json, to_markdown

STATIC = Path(__file__).parent / "static"
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_FILES_PER_SESSION = 5000


@dataclass
class Session:
    id: str
    dir: Path
    files: dict[str, str] = field(default_factory=dict)  # file_id → 원래 파일명(상대 경로)
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class Job:
    id: str
    state: str = "running"  # running / done / error
    logs: list[str] = field(default_factory=list)
    error: str = ""
    result: dict | None = None


class App:
    def __init__(self, llm_options: dict, agent_options: dict | None = None):
        self.root = Path(tempfile.mkdtemp(prefix="email_task_agent_"))
        self.sessions: dict[str, Session] = {}
        self.jobs: dict[str, Job] = {}
        self.llm_options = llm_options
        self.agent_options = agent_options or {}
        atexit.register(self.cleanup)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    # ---- sessions / files ---------------------------------------------------
    def new_session(self) -> Session:
        sid = uuid.uuid4().hex
        sess = Session(sid, self.root / sid)
        sess.dir.mkdir()
        self.sessions[sid] = sess
        return sess

    def add_file(self, sess: Session, name: str, data: bytes) -> str:
        with sess.lock:
            if len(sess.files) >= MAX_FILES_PER_SESSION:
                raise ValueError(f"파일은 최대 {MAX_FILES_PER_SESSION}개까지 올릴 수 있습니다.")
            fid = uuid.uuid4().hex[:12]
            # 경로 구분자·특수문자를 제거한 파일명만 사용 (경로 조작 방지), 확장자는 유지
            base = re.sub(r"[^\w.\- ()\[\]가-힣]", "_", Path(name.replace("\\", "/")).name)[-120:] or "mail"
            (sess.dir / f"{fid}__{base}").write_bytes(data)
            sess.files[fid] = name
            return fid

    def remove_file(self, sess: Session, fid: str) -> None:
        with sess.lock:
            for p in sess.dir.glob(f"{fid}__*"):
                p.unlink(missing_ok=True)
            sess.files.pop(fid, None)

    def clear(self, sess: Session) -> None:
        with sess.lock:
            shutil.rmtree(sess.dir, ignore_errors=True)
            sess.dir.mkdir()
            sess.files.clear()

    def scan(self, sess: Session, options: dict):
        window = resolve_window(options.get("date") or None, options.get("lookback") or None)
        report = LoadReport()
        records = load_emails(sess.dir, since=window.since, until=window.until, report=report)
        files = []
        for st in report.files.values():
            fid = Path(st.path).name.split("__", 1)[0]
            item = asdict(st)
            item.pop("path")
            item.update(id=fid, name=sess.files.get(fid, Path(st.path).name))
            files.append(item)
        files.sort(key=lambda f: (f["status"] != "ok", f["date"] or "9999", f["name"]))
        return window, report, records, files

    # ---- checklist job ------------------------------------------------------
    def start_job(self, sess: Session, options: dict) -> Job:
        window, report, records, _ = self.scan(sess, options)
        if not records:
            raise ValueError("분석할 수 있는 메일이 없습니다. 파일 목록의 제외 사유를 확인하세요.")
        job = Job(uuid.uuid4().hex)
        self.jobs[job.id] = job

        def work():
            try:
                llm = LLMClient(**self.llm_options)
                agent = EmailTaskAgent(llm, me=options.get("me", ""), log=job.logs.append, **self.agent_options)
                job.logs.append(f"메일 {len(records)}건 분석 시작 (LLM: {llm.model})")
                checklist, _ = agent.run(records, today=window.today, include_done=bool(options.get("include_done")))
                job.result = {
                    "checklist": json.loads(to_json(checklist)),
                    "markdown": to_markdown(checklist, len(records)),
                }
                job.state = "done"
            except LLMError as exc:
                job.error, job.state = f"LLM 호출 실패: {exc}", "error"
            except Exception as exc:  # 연결 실패 등
                traceback.print_exc()
                job.error, job.state = f"오류: {type(exc).__name__}: {exc}", "error"

        threading.Thread(target=work, daemon=True).start()
        return job


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "EmailTaskAgent"

        def log_message(self, fmt, *args):  # 요청 로그에 파일명 등이 남지 않도록 간단히
            sys.stderr.write(f"[web] {self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}\n")

        # ---- helpers ----
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _error(self, msg: str, code: int = 400) -> None:
            self._json({"error": msg}, code)

        def _body(self, limit: int = 1024 * 1024) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length > limit:
                raise ValueError(f"파일이 너무 큽니다 (최대 {limit // 1024 // 1024}MB).")
            return self.rfile.read(length)

        def _session(self, sid: str) -> Session | None:
            sess = app.sessions.get(sid)
            if sess is None:
                self._error("세션이 만료되었습니다. 페이지를 새로고침하세요.", 404)
            return sess

        def _same_origin(self) -> bool:
            # 다른 사이트의 페이지가 이 로컬 서버로 요청을 보내지 못하게 함
            origin = self.headers.get("Origin")
            return origin is None or origin.split("://", 1)[-1] == self.headers.get("Host")

        # ---- routes ----
        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            m = re.fullmatch(r"/api/jobs/(\w+)", path)
            if m:
                job = app.jobs.get(m.group(1))
                if job is None:
                    return self._error("작업을 찾을 수 없습니다.", 404)
                return self._json({"state": job.state, "logs": job.logs, "error": job.error, "result": job.result})
            if path == "/api/config":
                return self._json({
                    "model": app.llm_options.get("model") or DEFAULT_MODEL,
                    "base_url": app.llm_options.get("base_url") or DEFAULT_BASE_URL,
                    "extensions": sorted(SUPPORTED_EXTS),
                    "max_file_mb": MAX_FILE_BYTES // 1024 // 1024,
                })
            self._error("Not found", 404)

        def do_POST(self):
            if not self._same_origin():
                return self._error("허용되지 않은 요청입니다.", 403)
            path = self.path.split("?")[0]
            try:
                if path == "/api/sessions":
                    return self._json({"id": app.new_session().id})
                m = re.fullmatch(r"/api/sessions/(\w+)/(files|scan|run)", path)
                if not m:
                    return self._error("Not found", 404)
                sess = self._session(m.group(1))
                if sess is None:
                    return
                if m.group(2) == "files":
                    name = unquote(self.headers.get("X-File-Name", "mail.eml"))
                    if Path(name).suffix.lower() not in SUPPORTED_EXTS:
                        return self._error("지원하지 않는 형식입니다 (.eml/.msg 만 가능).", 415)
                    fid = app.add_file(sess, name, self._body(MAX_FILE_BYTES))
                    return self._json({"id": fid, "name": name})
                options = json.loads(self._body() or b"{}")
                if m.group(2) == "scan":
                    window, report, records, files = app.scan(sess, options)
                    return self._json({
                        "window": {"today": str(window.today), "since": str(window.since.date()), "label": window.label},
                        "used": len(records),
                        "files": files,
                        "summary": report.summary(window.since, window.until),
                        "hints": report.hints(window.label),
                    })
                job = app.start_job(sess, options)
                return self._json({"job": job.id})
            except ValueError as exc:
                return self._error(str(exc))

        def do_DELETE(self):
            if not self._same_origin():
                return self._error("허용되지 않은 요청입니다.", 403)
            m = re.fullmatch(r"/api/sessions/(\w+)(?:/files/(\w+))?", self.path.split("?")[0])
            if not m:
                return self._error("Not found", 404)
            sess = self._session(m.group(1))
            if sess is None:
                return
            if m.group(2):
                app.remove_file(sess, m.group(2))
            else:
                app.clear(sess)
            self._json({"ok": True})

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, llm_options: dict | None = None,
          agent_options: dict | None = None) -> tuple[ThreadingHTTPServer, App]:
    app = App(llm_options or {}, agent_options)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    return server, app


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="email_task_agent.web", description="메일 업로드 웹 화면")
    p.add_argument("--host", default="127.0.0.1", help="기본 127.0.0.1 (내 PC 에서만 접속). 0.0.0.0 이면 사내망 공유")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--base-url", default=None, help=f"vLLM 서버 주소 (기본 {DEFAULT_BASE_URL})")
    p.add_argument("--model", default=None, help=f"모델 이름 (기본 {DEFAULT_MODEL})")
    p.add_argument("--max-tokens", type=int, default=8192)
    p.add_argument("--batch-chars", type=int, default=24000)
    p.add_argument("--no-browser", action="store_true", help="시작할 때 브라우저를 자동으로 열지 않음")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    server, app = serve(
        args.host, args.port,
        llm_options={"base_url": args.base_url, "model": args.model, "max_tokens": args.max_tokens},
        agent_options={"batch_chars": args.batch_chars},
    )
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{server.server_port}"
    print(f"메일 업로드 화면: {url}  (종료: Ctrl+C)")
    if not args.no_browser:
        import webbrowser

        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    if args.host not in ("127.0.0.1", "localhost"):
        print("[주의] 다른 PC 에서도 접속할 수 있습니다. 업로드한 메일은 서버 PC 의 임시 폴더에 저장됩니다.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
