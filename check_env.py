"""실행 환경 점검 — VS Code 에서 이 파일을 열고 ▶ 를 누르거나 `python check_env.py`.

대시보드가 실행되지 않을 때 원인을 한 번에 확인합니다: Python 버전, 패키지, 프로젝트 파일, 포트, LLM 서버.
결과 화면을 그대로 복사해 전달하면 문제를 빠르게 찾을 수 있습니다. (메일 내용은 출력하지 않습니다)
"""

import importlib.util
import os
import platform
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
OK, BAD, WARN = "✅", "❌", "⚠️"
problems = 0


def line(mark, title, detail="", fix=""):
    global problems
    if mark == BAD:
        problems += 1
    print(f"{mark} {title}" + (f" — {detail}" if detail else ""))
    if fix:
        print(f"     ↳ 해결: {fix}")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("=" * 60)
    print(" 메일 업무 대시보드 — 실행 환경 점검")
    print("=" * 60)
    print(f"OS: {platform.platform()}")
    print(f"Python: {sys.version.split()[0]}  ({sys.executable})")
    print(f"프로젝트 폴더: {ROOT}")
    print(f"현재 작업 폴더: {Path.cwd()}")
    print("-" * 60)

    # 1. Python
    if sys.version_info >= (3, 10):
        line(OK, "Python 버전", sys.version.split()[0])
    else:
        line(BAD, "Python 버전", f"{sys.version.split()[0]} (3.10 이상 필요)",
             "python.org 에서 3.10 이상 설치 후 VS Code 왼쪽 아래(또는 Ctrl+Shift+P → Python: Select Interpreter)에서 선택")
    if "WindowsApps" in sys.executable:
        line(WARN, "Microsoft Store 용 Python 사용 중", "회사 PC 에서는 권한 문제가 생길 수 있음",
             "python.org 설치본을 권장")

    # 2. 프로젝트 파일
    needed = ["run_web.py", "requirements.txt", "email_task_agent/web.py", "email_task_agent/static/index.html"]
    missing_files = [f for f in needed if not (ROOT / f).is_file()]
    if missing_files:
        line(BAD, "프로젝트 파일", "없음: " + ", ".join(missing_files),
             "GitHub 에서 코드를 다시 받으세요 (Code → Download ZIP → 압축 풀기 → VS Code 에서 '폴더 열기')")
    else:
        line(OK, "프로젝트 파일", "모두 있음")

    # 3. 패키지
    pkgs_ok = True
    try:
        import run_web  # 실행기와 같은 기준으로 확인
        missing = run_web.missing_packages()
    except Exception as exc:  # run_web 자체를 못 읽는 경우
        missing = [f"(확인 실패: {exc})"]
    if missing:
        pkgs_ok = False
        line(BAD, "필요한 패키지", ", ".join(missing),
             f'"{sys.executable}" -m pip install --upgrade -r requirements.txt   (또는 run_web.py 실행 시 Y 입력)')
    else:
        from importlib.metadata import version
        line(OK, "필요한 패키지", ", ".join(f"{p} {version(p)}" for p in ("openai", "pydantic", "olefile")))

    # 4. 포트
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", 8765))
            line(OK, "포트 8765", "사용 가능")
        except OSError:
            line(WARN, "포트 8765", "이미 사용 중 (이전 실행이 켜져 있을 수 있음) — 실행 시 다음 포트로 자동 변경됨")

    # 5. 프록시 설정 (참고)
    proxy_vars = {k: v for k, v in os.environ.items() if k.lower() in ("http_proxy", "https_proxy", "no_proxy", "all_proxy")}
    if proxy_vars:
        line(OK, "프록시 환경변수", ", ".join(proxy_vars) + " (LLM 서버는 프록시를 거치지 않고 직접 연결)")

    # 6. 모듈 import + LLM 서버
    if pkgs_ok and not missing_files:
        try:
            from email_task_agent.llm import LLMClient
            from email_task_agent import web  # noqa: F401  (웹 서버 코드 import 확인)
            line(OK, "프로그램 코드 불러오기", "정상")
        except Exception as exc:
            line(BAD, "프로그램 코드 불러오기", f"{type(exc).__name__}: {exc}", "이 메시지를 그대로 전달해 주세요")
            return finish()
        print("-" * 60)
        print("LLM 서버 연결 확인 중... (최대 20초)")
        try:
            ok, lines = LLMClient(max_retries=0, timeout=20).check_connection()
        except Exception as exc:
            ok, lines = False, [f"{type(exc).__name__}: {exc}"]
        for text in lines:
            print("   " + text)
        if ok:
            line(OK, "LLM 서버")
        else:
            line(WARN, "LLM 서버", "연결 안 됨 — 대시보드는 열리지만 '체크리스트 만들기'에서 실패합니다",
                 "사내망(VPN) 연결, 서버 주소(환경변수 LLM_BASE_URL)를 확인하세요")
    return finish()


def finish() -> int:
    print("=" * 60)
    if problems:
        print(f"{BAD} 해결이 필요한 항목 {problems}개 — 위의 '해결' 안내를 따라 주세요.")
    else:
        print(f"{OK} 실행 준비 완료 — run_web.py 를 실행하세요 (VS Code: F5 → '메일 업무 대시보드 (웹)')")
    print("=" * 60)
    return 1 if problems else 0


if __name__ == "__main__":
    code = main()
    if os.name == "nt" and sys.stdin and sys.stdin.isatty() and "TERM_PROGRAM" not in os.environ:
        input("Enter 를 누르면 창이 닫힙니다...")  # 탐색기에서 더블클릭한 경우 결과를 볼 수 있게
    raise SystemExit(code)
