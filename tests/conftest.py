import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def sample_mails(tmp_path_factory) -> Path:
    """예시 메일 생성 (날짜는 실행 주 기준 상대값)."""
    sys.path.insert(0, str(ROOT / "samples"))
    import make_samples

    return make_samples.main(tmp_path_factory.mktemp("mails"))
