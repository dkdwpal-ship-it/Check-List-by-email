"""메일 업무 대시보드 실행기.

VS Code 에서 이 파일을 열고 ▶(Run Python File) 을 누르거나, 터미널에서 `python run_web.py` 로 실행하세요.
어느 폴더에서 실행해도 동작하며, 필요한 패키지가 없으면 설치 명령을 알려줍니다.
옵션은 그대로 전달됩니다: python run_web.py --port 8800 --batch-chars 12000
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

REQUIRED = {"openai": "openai", "pydantic": "pydantic", "olefile": "olefile"}


def check_requirements() -> bool:
    if sys.version_info < (3, 10):
        print(f"Python 3.10 이상이 필요합니다. 현재: {sys.version.split()[0]} ({sys.executable})")
        print("VS Code 왼쪽 아래(또는 Ctrl+Shift+P → 'Python: Select Interpreter')에서 3.10 이상을 선택하세요.")
        return False
    missing = [pkg for mod, pkg in REQUIRED.items() if importlib.util.find_spec(mod) is None]
    if missing:
        print(f"필요한 패키지가 이 Python 에 설치되어 있지 않습니다: {', '.join(missing)}")
        print(f"현재 Python: {sys.executable}")
        print("아래 명령으로 설치한 뒤 다시 실행하세요 (VS Code 가 선택한 Python 과 같은 Python 에 설치해야 합니다):")
        print(f'  "{sys.executable}" -m pip install -r "{ROOT / "requirements.txt"}"')
        return False
    return True


if __name__ == "__main__":
    if not check_requirements():
        raise SystemExit(1)
    from email_task_agent.web import main

    raise SystemExit(main(sys.argv[1:]))
