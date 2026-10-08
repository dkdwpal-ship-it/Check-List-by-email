"""일자별 메일 요약: 메일별 LLM 요약을 날짜 단위로 묶어 대시보드/Markdown 으로 제공."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from .agent import WEEKDAYS_KO, mail_ref
from .eml_parser import EmailRecord
from .models import MailSummary


def build_daily_digest(records: list[EmailRecord], summaries: dict[str, MailSummary]) -> list[dict]:
    """최신 날짜가 먼저 오도록 [{date, weekday, count, action_count, high_count, mails: [...]}] 반환.

    records 는 agent.run 에 넘긴 순서 그대로여야 함 (요약은 mail_ref(인덱스)로 연결됨).
    요약이 누락된 메일도 제목·발신자와 함께 표시한다.
    """
    days: dict[str, list[dict]] = defaultdict(list)
    for i, rec in enumerate(records):
        if rec.date is None:
            continue
        s = summaries.get(mail_ref(i))
        days[rec.date.strftime("%Y-%m-%d")].append({
            "ref": mail_ref(i),  # 원문 메일 보기용 ID
            "time": rec.date.strftime("%H:%M"),
            "sender": rec.sender,
            "to": rec.to,
            "subject": rec.subject,
            "attachments": rec.attachments,
            "summary": s.summary if s else "",
            "key_points": s.key_points if s else [],
            "category": s.category if s else "",
            "needs_action": bool(s and s.needs_action),
            "importance": s.importance if s else "medium",
            "summarized": s is not None,
        })
    out = []
    for day in sorted(days, reverse=True):
        mails = sorted(days[day], key=lambda m: m["time"])
        out.append({
            "date": day,
            "weekday": WEEKDAYS_KO[date.fromisoformat(day).weekday()],
            "count": len(mails),
            "action_count": sum(m["needs_action"] for m in mails),
            "high_count": sum(m["importance"] == "high" for m in mails),
            "mails": mails,
        })
    return out


def digest_to_markdown(digest: list[dict]) -> str:
    lines = ["## 📅 일자별 메일 요약", ""]
    if not digest:
        return "\n".join(lines + ["_요약할 메일이 없습니다._", ""])
    for day in digest:
        extra = f" · 할 일 {day['action_count']}건" if day["action_count"] else ""
        lines.append(f"### {day['date']} ({day['weekday']}) — 메일 {day['count']}건{extra}")
        lines.append("")
        for m in day["mails"]:
            tags = []
            if m["importance"] == "high":
                tags.append("🔴")
            if m["needs_action"]:
                tags.append("✅할 일")
            if m["category"]:
                tags.append(f"[{m['category']}]")
            lines.append(f"- **{m['time']} {m['subject']}** — {m['sender']} {' '.join(tags)}".rstrip())
            if m["summary"]:
                lines.append(f"  - {m['summary']}")
            for p in m["key_points"]:
                lines.append(f"    - {p}")
        lines.append("")
    return "\n".join(lines)
