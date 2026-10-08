"""메일 업무 대시보드 실행기 — VS Code 에서 바로 실행하세요.

    · 이 파일을 열고 오른쪽 위 ▶ (Run Python File)
    · 또는 F5 (실행 및 디버그 → '메일 업무 대시보드 (웹)')
    · 또는 터미널에서: python run_web.py

어느 폴더에서 실행해도 동작합니다. 필요한 패키지가 없으면 지금 쓰는 Python 에 설치할지 물어봅니다.
옵션은 그대로 전달됩니다: python run_web.py --port 8800 --batch-chars 12000
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

REQUIRED = {"openai": "openai", "pydantic": "pydantic", "olefile": "olefile"}
MIN_VERSIONS = {"openai": (1, 58), "pydantic": (2, 6)}  # requirements.txt 와 동일하게 유지


def _version(pkg: str) -> tuple[int, ...]:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return tuple(int(x) for x in version(pkg).split(".")[:2] if x.isdigit())
    except (PackageNotFoundError, ValueError):
        return (0,)


def missing_packages() -> list[str]:
    """설치 안 됐거나 너무 오래된 패키지 (예: openai 1.5x 는 최신 httpx 와 함께 쓰면 실행 오류)."""
    out = []
    for mod, pkg in REQUIRED.items():
        if importlib.util.find_spec(mod) is None:
            out.append(pkg)
        elif pkg in MIN_VERSIONS and _version(pkg) < MIN_VERSIONS[pkg]:
            out.append(f"{pkg}(업데이트 필요: {'.'.join(map(str, _version(pkg)))} → {'.'.join(map(str, MIN_VERSIONS[pkg]))} 이상)")
    return out


def install(packages: list[str]) -> bool:
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "-r", str(ROOT / "requirements.txt")]
    print("설치 중:", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    if subprocess.run(cmd).returncode != 0:
        print("\n설치에 실패했습니다. 사내망이라면 pip 프록시/사내 저장소 설정이 필요할 수 있습니다.")
        print('  예) python -m pip install -r requirements.txt --proxy http://프록시주소:포트')
        print('  또는 --index-url 로 사내 PyPI 미러를 지정하세요.')
        return False
    importlib.invalidate_caches()
    return not missing_packages()


def check_requirements(auto_yes: bool = False) -> bool:
    if sys.version_info < (3, 10):
        print(f"Python 3.10 이상이 필요합니다. 현재: {sys.version.split()[0]} ({sys.executable})")
        print("VS Code 왼쪽 아래 Python 버전(또는 Ctrl+Shift+P → 'Python: Select Interpreter')에서 3.10 이상을 선택하세요.")
        return False
    missing = missing_packages()
    if not missing:
        return True
    print(f"필요한 패키지가 없거나 오래되었습니다: {', '.join(missing)}")
    print(f"현재 Python: {sys.executable}")
    answer = "y" if auto_yes else ""
    if not auto_yes and sys.stdin and sys.stdin.isatty():
        try:
            answer = input("지금 설치할까요? [Y/n] ").strip().lower() or "y"
        except EOFError:
            answer = "n"
    if answer in ("y", "yes", "ㅛ", "예", "네"):
        return install(missing)
    print("아래 명령으로 설치한 뒤 다시 실행하세요 (VS Code 가 선택한 Python 과 같은 Python 에 설치해야 합니다):")
    print(f'  "{sys.executable}" -m pip install --upgrade -r "{ROOT / "requirements.txt"}"')
    return False


if __name__ == "__main__":
    args = sys.argv[1:]
    auto_yes = "--install" in args
    args = [a for a in args if a != "--install"]
    if not check_requirements(auto_yes):
        raise SystemExit(1)
    from email_task_agent.web import main

    raise SystemExit(main(args))
