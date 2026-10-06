"""Render a Checklist as Markdown or JSON."""

from __future__ import annotations

import json
from datetime import date

from .agent import WEEKDAYS_KO
from .models import Checklist, ChecklistItem

_PRIO_ICON = {"high": "🔴", "medium": "🟡", "low": "⚪"}


def _d(d: date) -> str:
    return f"{d.month}/{d.day}({WEEKDAYS_KO[d.weekday()]})"


def _line(item: ChecklistItem) -> str:
    t = item.task
    box = "[x]" if t.status == "done" and not item.occurrence else "[ ]"
    parts = [f"- {box} {_PRIO_ICON[t.priority]} **{t.title}**"]
    if item.date:
        parts.append(f"— 기한 {_d(item.date)}")
    if item.occurrence:
        parts.append("🔁 반복")
    lines = [" ".join(parts)]
    meta = []
    if t.requester:
        meta.append(f"요청: {t.requester}")
    if t.due_text and not item.occurrence:
        meta.append(f"원문 기한: “{t.due_text}”")
    if t.source_subjects:
        meta.append("메일: " + " / ".join(dict.fromkeys(t.source_subjects)))
    if meta:
        lines.append("  - " + " · ".join(meta))
    if t.description:
        lines.append(f"  - {t.description}")
    return "\n".join(lines)


def _section(title: str, items: list[ChecklistItem], empty: str) -> str:
    body = "\n".join(_line(i) for i in items) if items else f"_{empty}_"
    return f"## {title} ({len(items)})\n\n{body}\n"


def to_markdown(cl: Checklist, email_count: int | None = None) -> str:
    tw, nw = cl.this_week, cl.next_week
    head = [
        "# 📋 메일 기반 주간 업무 체크리스트",
        "",
        f"- 기준일: {cl.reference_date.isoformat()} ({WEEKDAYS_KO[cl.reference_date.weekday()]})",
        f"- 이번 주: {_d(tw[0])} ~ {_d(tw[1])}",
        f"- 다음 주: {_d(nw[0])} ~ {_d(nw[1])}",
    ]
    if email_count is not None:
        head.append(f"- 분석한 메일: {email_count}건")
    sections = [
        "\n".join(head) + "\n",
        _section("⚠️ 기한 지남 (미완료)", cl.overdue, "없음"),
        _section("이번 주 할 일", cl.this_week_items, "이번 주 마감 업무 없음"),
        _section("다음 주 할 일", cl.next_week_items, "다음 주 마감 업무 없음"),
        _section("기한 미정 (확인 필요)", cl.undated, "없음"),
        "> 🔴 높음 · 🟡 보통 · ⚪ 낮음 — 메일 내용을 LLM이 해석한 결과이므로 원문 메일로 확인하세요.\n",
    ]
    return "\n".join(sections)


def to_json(cl: Checklist) -> str:
    return json.dumps(json.loads(cl.model_dump_json()), ensure_ascii=False, indent=2)
