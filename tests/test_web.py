import json
import threading
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import HTTPServer
from urllib.parse import quote

import pytest

from email_task_agent.web import serve
from tests import test_agent
from tests.test_agent import _MockVLLM


@pytest.fixture
def web(mock_server):
    server, app = serve("127.0.0.1", 0, llm_options={"base_url": mock_server, "max_retries": 0})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", app
    server.shutdown()
    app.cleanup()


mock_server = test_agent.mock_server  # 같은 모의 vLLM 픽스처 재사용


def call(base, method, path, body=None, headers=None):
    req = urllib.request.Request(base + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def upload(base, sid, name, data):
    return call(base, "POST", f"/api/sessions/{sid}/files", data, {"X-File-Name": quote(name)})


def test_index_page_is_self_contained(web):
    base, _ = web
    with urllib.request.urlopen(base + "/") as res:
        html = res.read().decode()
    assert "끌어다 놓으세요" in html
    assert "https://" not in html.replace("http://www.w3.org", "")  # 사내망: 외부 리소스 없음


def test_upload_scan_and_run(web, sample_mails):
    base, app = web
    _, s = call(base, "POST", "/api/sessions")
    sid = s["id"]
    for p in sorted(sample_mails.glob("*.eml")):
        assert upload(base, sid, "받은메일/" + p.name, p.read_bytes())[0] == 200
    assert upload(base, sid, "날짜없음.eml", b"From: a@b.c\nSubject: x\n\nhi\n")[0] == 200
    assert upload(base, sid, "../../evil.eml", b"From: a@b.c\nSubject: y\n\nhi\n")[0] == 200
    assert upload(base, sid, "archive.pst", b"x")[0] == 415
    # 업로드 파일은 세션 폴더 안에만 저장 (경로 조작 불가)
    assert all(p.parent == app.sessions[sid].dir for p in app.root.rglob("*") if p.is_file())

    code, r = call(base, "POST", f"/api/sessions/{sid}/scan", b"{}")
    assert code == 200 and r["used"] == 6 and r["window"]["label"] == "2년"
    by_name = {f["name"]: f for f in r["files"]}
    assert by_name["날짜없음.eml"]["status"] == "no_date"
    assert by_name["받은메일/02.eml"]["status"] == "ok" and by_name["받은메일/02.eml"]["subject"]

    code, r = call(base, "POST", f"/api/sessions/{sid}/run", json.dumps({"me": "김대리"}).encode())
    assert code == 200
    for _ in range(100):
        code, job = call(base, "GET", f"/api/jobs/{r['job']}")
        if job["state"] != "running":
            break
        time.sleep(0.1)
    assert job["state"] == "done", job
    titles = [i["task"]["title"] for i in job["result"]["checklist"]["this_week_items"]]
    assert "Q3 실적 보고서 초안 송부" in titles
    assert "다음 주 할 일" in job["result"]["markdown"]
    digest = job["result"]["digest"]
    assert sum(d["count"] for d in digest) == 6
    assert all(m["summary"] for d in digest for m in d["mails"])
    assert "일자별 메일 요약" in job["result"]["markdown"]
    topics = job["result"]["topics"]
    assert len(topics["mails"]) == 6 and topics["keywords"]
    assert any(m["keyword_source"] == "llm" for m in topics["mails"])
    assert topics["overviews"] and all(v.endswith(")") for v in topics["overviews"].values())
    assert "시기별 키워드" in job["result"]["markdown"]

    # 원문 메일 보기: 요약/키워드 화면의 ref 로 원문(인용 본문 포함)과 원본 파일을 받을 수 있음
    ref = next(m["ref"] for m in topics["mails"] if "견적서" in m["subject"])
    code, mail = call(base, "GET", f"/api/jobs/{r['job']}/mails/{ref}")
    assert code == 200 and mail["subject"] == "RE: 견적서 검토 요청"
    assert "Original Message" in mail["body"]          # 분석 때 지운 인용 본문도 원문에는 있음
    assert mail["sender"].startswith("최과장") and mail["file"] == "03.eml"
    with urllib.request.urlopen(f"{base}/api/jobs/{r['job']}/mails/{ref}/raw") as res:
        assert res.read() == (sample_mails / "03.eml").read_bytes()
        assert "attachment" in res.headers["Content-Disposition"]
    assert call(base, "GET", f"/api/jobs/{r['job']}/mails/M999")[0] == 404
    assert call(base, "GET", f"/api/jobs/nojob/mails/{ref}")[0] == 404
    assert all(m.get("ref") for d in digest for m in d["mails"])

    fid = by_name["날짜없음.eml"]["id"]
    assert call(base, "DELETE", f"/api/sessions/{sid}/files/{fid}")[0] == 200
    assert "날짜없음.eml" not in {f["name"] for f in call(base, "POST", f"/api/sessions/{sid}/scan", b"{}")[1]["files"]}


def test_rules_match_cli(web):
    base, _ = web
    sid = call(base, "POST", "/api/sessions")[1]["id"]
    past = str(date.today() - timedelta(days=1))
    code, r = call(base, "POST", f"/api/sessions/{sid}/scan", json.dumps({"date": past}).encode())
    assert code == 400 and "이전으로 지정할 수 없습니다" in r["error"]
    code, r = call(base, "POST", f"/api/sessions/{sid}/scan", json.dumps({"lookback": "3y"}).encode())
    assert code == 400 and "최대 2년" in r["error"]
    code, r = call(base, "POST", f"/api/sessions/{sid}/run", b"{}")
    assert code == 400 and "분석할 수 있는 메일이 없습니다" in r["error"]


def test_rejects_cross_origin_requests(web):
    base, _ = web
    code, _ = call(base, "POST", "/api/sessions", headers={"Origin": "https://evil.example"})
    assert code == 403
    code, _ = call(base, "POST", "/api/sessions", headers={"Origin": base})
    assert code == 200


def test_page_script_has_no_syntax_errors(tmp_path):
    import re
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node 가 없어 JS 문법 검사를 건너뜀")
    html = (Path(__file__).resolve().parents[1] / "email_task_agent/static/index.html").read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(blocks) >= 2  # 구형 브라우저 안내(ES5) + 본 스크립트
    for i, code in enumerate(blocks):
        script = tmp_path / f"page{i}.js"
        script.write_text(code, encoding="utf-8")
        res = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
    # 구형 브라우저 안내 스크립트는 ES5 문법만 써야 IE 에서도 실행됨
    assert not re.search(r"=>|\bconst\b|\blet\b|`", blocks[0].split("new Function(")[0])
