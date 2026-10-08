"""분석 결과 묶음 생성 + 서버 없이 여는 대시보드 HTML 파일 만들기.

웹 화면(web.py)과 명령행(cli.py --html)이 같은 결과를 쓰도록 공유한다.
HTML 파일은 static/index.html 에 결과 데이터를 넣은 단일 파일이라, 더블클릭으로 열 수 있고 인터넷도 필요 없다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from .agent import EmailTaskAgent, mail_ref
from .digest import build_daily_digest, digest_to_markdown
from .eml_parser import SUPPORTED_EXTS, EmailRecord
from .llm import LLMClient
from .models import Checklist
from .render import to_json, to_markdown
from .topics import build_keyword_index, summarize_periods, topics_to_markdown

STATIC = Path(__file__).parent / "static" / "index.html"
MAX_EXPORT_BODY = 30000  # HTML 파일에 넣는 메일 1건당 본문 최대 글자수 (파일이 너무 커지지 않게)


def build_result(agent: EmailTaskAgent, checklist: Checklist, records: list[EmailRecord], llm: LLMClient,
                 log: Callable[[str], None] = print) -> dict:
    """대시보드 3개 탭(체크리스트·일자별 요약·시기별 키워드)에 필요한 결과 전체."""
    digest = build_daily_digest(records, agent.summaries)
    index = build_keyword_index(records, agent.summaries)
    overviews, topic_warnings = summarize_periods(llm, index, log=log)
    return {
        "checklist": json.loads(to_json(checklist)),
        "digest": digest,
        "topics": {**index, "overviews": overviews},
        "warnings": agent.warnings + topic_warnings,
        "markdown": "\n".join([to_markdown(checklist, len(records)), digest_to_markdown(digest),
                               topics_to_markdown(index, overviews)]),
    }


def original_mail(path: Path, max_body: int | None = None) -> dict:
    """원문 메일 보기용 데이터 (회신 인용 본문 포함, HTML 메일은 텍스트로 변환)."""
    rec = SUPPORTED_EXTS[path.suffix.lower()](path, strip_quotes=False)
    body = rec.body
    if max_body and len(body) > max_body:
        body = body[:max_body] + f"\n\n...(본문이 길어 {max_body:,}자까지만 표시)..."
    return {
        "file": path.name.split("__", 1)[-1], "subject": rec.subject, "sender": rec.sender, "to": rec.to, "cc": rec.cc,
        "date": rec.date.strftime("%Y-%m-%d %H:%M") if rec.date else "", "attachments": rec.attachments, "body": body,
    }


def dashboard_html(result: dict, records: list[EmailRecord], include_bodies: bool = True) -> str:
    """서버 없이 열 수 있는 단일 HTML. 원문 메일도 파일 안에 넣어 오프라인에서 볼 수 있게 함."""
    mails = {}
    if include_bodies:
        for i, rec in enumerate(records):
            try:
                mails[mail_ref(i)] = original_mail(Path(rec.path), MAX_EXPORT_BODY)
            except Exception:  # 원본 파일이 사라졌으면 분석 때 읽은 내용으로 대신
                mails[mail_ref(i)] = {"file": Path(rec.path).name.split("__", 1)[-1], "subject": rec.subject,
                                      "sender": rec.sender, "to": rec.to, "cc": rec.cc, "date": rec.date_str,
                                      "attachments": rec.attachments, "body": rec.body}
    payload = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "result": result, "mails": mails}
    data = json.dumps(payload, ensure_ascii=False)
    # <script> 안에 넣어도 안전하게: 태그 종료 문자열과 JS 줄바꿈 문자를 이스케이프
    data = data.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = STATIC.read_text(encoding="utf-8")
    marker = "<script>\n(() => {"
    assert marker in html, "index.html 구조가 바뀌었습니다"
    return html.replace(marker, f"<script>window.__EXPORT__ = {data};</script>\n{marker}", 1)
