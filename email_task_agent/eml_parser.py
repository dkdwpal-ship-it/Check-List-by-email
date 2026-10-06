"""Parse saved .eml files into plain, LLM-friendly records."""

from __future__ import annotations

import email
import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

_QUOTE_MARKERS = (
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.I | re.M),
    re.compile(r"^-{2,}\s*원본 메시지\s*-{2,}", re.M),
    re.compile(r"^On .+wrote:\s*$", re.M),
    re.compile(r"^\d{4}[./-]\s?\d{1,2}[./-]\s?\d{1,2}.*작성:\s*$", re.M),
    re.compile(r"^From:\s.+\n(?:Sent|Date):\s", re.M),
    re.compile(r"^보낸 사람:\s", re.M),
)


@dataclass
class EmailRecord:
    path: str
    message_id: str
    subject: str
    sender: str
    to: list[str]
    cc: list[str]
    date: datetime | None
    body: str
    attachments: list[str] = field(default_factory=list)

    @property
    def date_str(self) -> str:
        return self.date.strftime("%Y-%m-%d (%a) %H:%M") if self.date else "날짜 미상"

    def to_prompt_text(self, max_body_chars: int = 6000) -> str:
        body = self.body
        if len(body) > max_body_chars:
            body = body[:max_body_chars] + "\n...(본문 생략)..."
        lines = [
            f"[메일 ID] {self.message_id}",
            f"[날짜] {self.date_str}",
            f"[보낸사람] {self.sender}",
            f"[받는사람] {', '.join(self.to) or '-'}",
        ]
        if self.cc:
            lines.append(f"[참조] {', '.join(self.cc)}")
        lines.append(f"[제목] {self.subject}")
        if self.attachments:
            lines.append(f"[첨부] {', '.join(self.attachments)}")
        lines.append("[본문]")
        lines.append(body.strip())
        return "\n".join(lines)


def _html_to_text(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style|head).*?</\1>", "", raw)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h\d)>", "\n", text)
    text = re.sub(r"(?i)<td[^>]*>", " | ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _strip_quoted(text: str) -> str:
    """Drop the quoted reply chain so each mail contributes only its own content."""
    cut = len(text)
    for pattern in _QUOTE_MARKERS:
        m = pattern.search(text)
        if m and m.start() > 0:
            cut = min(cut, m.start())
    kept = text[:cut]
    kept = "\n".join(line for line in kept.splitlines() if not line.lstrip().startswith(">"))
    return kept.strip() or text.strip()


def _decode_part(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, AssertionError):
        payload = part.get_payload(decode=True) or b""
        for enc in (part.get_content_charset(), "utf-8", "cp949", "euc-kr", "latin-1"):
            if not enc:
                continue
            try:
                return payload.decode(enc)
            except (LookupError, UnicodeDecodeError):
                continue
        return payload.decode("utf-8", errors="replace")


def _extract_body(msg: EmailMessage) -> str:
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        return _decode_part(plain)
    rich = msg.get_body(preferencelist=("html",))
    if rich is not None:
        return _html_to_text(_decode_part(rich))
    return ""


def _addresses(msg: EmailMessage, header: str) -> list[str]:
    values = [str(v) for v in msg.get_all(header, [])]
    out = []
    for name, addr in getaddresses(values):
        if name and addr:
            out.append(f"{name} <{addr}>")
        elif addr:
            out.append(addr)
    return out


def parse_eml(path: str | Path, strip_quotes: bool = True) -> EmailRecord:
    path = Path(path)
    with path.open("rb") as fh:
        msg = email.message_from_binary_file(fh, policy=policy.default)

    date = None
    if msg["Date"]:
        try:
            date = parsedate_to_datetime(str(msg["Date"]))
            # 발신자 현지 시각을 유지: '내일', '다음주 금요일' 같은 표현은 발신자 기준이므로
            date = date.replace(tzinfo=None)
        except (TypeError, ValueError):
            date = None

    body = _extract_body(msg)
    if strip_quotes:
        body = _strip_quoted(body)

    attachments = [
        part.get_filename() for part in msg.iter_attachments() if part.get_filename()
    ]
    senders = _addresses(msg, "From")
    return EmailRecord(
        path=str(path),
        message_id=str(msg["Message-ID"] or path.name).strip(),
        subject=str(msg["Subject"] or "(제목 없음)").strip(),
        sender=senders[0] if senders else "",
        to=_addresses(msg, "To"),
        cc=_addresses(msg, "Cc"),
        date=date,
        body=body,
        attachments=attachments,
    )


def load_emails(
    source: str | Path,
    since: datetime | None = None,
    until: datetime | None = None,
    strip_quotes: bool = True,
) -> list[EmailRecord]:
    """Load every .eml under `source` (file or directory), deduplicated and sorted by date."""
    source = Path(source)
    paths = [source] if source.is_file() else sorted(source.rglob("*.eml"))
    seen: set[str] = set()
    records: list[EmailRecord] = []
    for p in paths:
        try:
            rec = parse_eml(p, strip_quotes=strip_quotes)
        except Exception as exc:  # a single corrupt file shouldn't stop the run
            print(f"[경고] {p} 파싱 실패: {exc}")
            continue
        if rec.message_id in seen:
            continue
        seen.add(rec.message_id)
        if rec.date and since and rec.date < since:
            continue
        if rec.date and until and rec.date > until:
            continue
        records.append(rec)
    records.sort(key=lambda r: r.date or datetime.min)
    return records
