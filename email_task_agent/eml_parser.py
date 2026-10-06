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
        message_id=str(msg["Message-ID"] or path.resolve()).strip(),
        subject=str(msg["Subject"] or "(제목 없음)").strip(),
        sender=senders[0] if senders else "",
        to=_addresses(msg, "To"),
        cc=_addresses(msg, "Cc"),
        date=date,
        body=body,
        attachments=attachments,
    )


def parse_msg(path: str | Path, strip_quotes: bool = True) -> EmailRecord:
    from .msg_parser import read_msg  # olefile is only needed for .msg

    path = Path(path)
    d = read_msg(path)
    body = d["body"] or (_html_to_text(d["html"]) if d["html"] else "")
    if strip_quotes:
        body = _strip_quoted(body)
    return EmailRecord(
        path=str(path),
        message_id=d["message_id"] or str(path.resolve()),
        subject=d["subject"].strip() or "(제목 없음)",
        sender=d["sender"],
        to=d["to"],
        cc=d["cc"],
        date=d["date"],
        body=body,
        attachments=d["attachments"],
    )


SUPPORTED_EXTS = {".eml": parse_eml, ".msg": parse_msg}


@dataclass
class LoadReport:
    """Why each file under the source was or was not used — shown to the user when mails go missing."""

    found: int = 0
    loaded: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    duplicates: int = 0
    too_old: list[datetime] = field(default_factory=list)
    too_new: list[datetime] = field(default_factory=list)
    no_date: int = 0
    unsupported: dict[str, int] = field(default_factory=dict)

    def summary(self, since: datetime | None = None, until: datetime | None = None) -> str:
        lines = [f"메일 파일 {self.found}개 발견 → {self.loaded}건 사용"]
        if self.too_old:
            lines.append(
                f"  - 기간 이전이라 제외: {len(self.too_old)}건 (가장 최근 {max(self.too_old):%Y-%m-%d})"
                + (f", 분석 시작일 {since:%Y-%m-%d}" if since else "")
            )
        if self.too_new:
            lines.append(
                f"  - 기준일 이후라 제외: {len(self.too_new)}건 (가장 이른 {min(self.too_new):%Y-%m-%d})"
                + (f", 기준일 {until:%Y-%m-%d}" if until else "")
            )
        if self.duplicates:
            lines.append(f"  - 중복(같은 Message-ID) 제외: {self.duplicates}건")
        if self.no_date:
            lines.append(f"  - 날짜 정보 없음(포함함): {self.no_date}건")
        for path, err in self.failed[:10]:
            lines.append(f"  - 읽기 실패: {path} ({err})")
        if len(self.failed) > 10:
            lines.append(f"  - 읽기 실패 {len(self.failed) - 10}건 더 있음")
        if self.unsupported:
            exts = ", ".join(f"{e or '(확장자 없음)'} {n}개" for e, n in sorted(self.unsupported.items()))
            lines.append(f"  - 지원하지 않는 파일(무시): {exts}")
        return "\n".join(lines)

    def hints(self, lookback_label: str = "2년") -> list[str]:
        out = []
        if self.found == 0:
            out.append("폴더 안에 .eml/.msg 파일이 없습니다. 경로가 맞는지, 메일을 파일로 저장했는지 확인하세요.")
            if ".pst" in self.unsupported or ".ost" in self.unsupported:
                out.append(".pst/.ost(Outlook 데이터 파일)는 직접 읽을 수 없습니다. Outlook에서 메일을 선택해 폴더로 끌어다 놓아 .msg로 저장하세요.")
        if self.too_old and self.loaded == 0:
            out.append(
                f"모든 메일이 분석 기간(최근 {lookback_label})보다 오래되었습니다. "
                "--lookback 값을 늘리거나(예: --lookback 3y) --date 로 기준일을 메일 시점에 맞추세요."
            )
        if self.too_new and self.loaded == 0:
            out.append("메일이 기준일(--date)보다 이후입니다. --date 값을 확인하세요.")
        return out


def load_emails(
    source: str | Path,
    since: datetime | None = None,
    until: datetime | None = None,
    strip_quotes: bool = True,
    report: LoadReport | None = None,
) -> list[EmailRecord]:
    """Load every .eml/.msg under `source` (file or directory), deduplicated and sorted by date.

    Pass a LoadReport to learn which files were skipped and why.
    """
    source = Path(source)
    report = report if report is not None else LoadReport()
    candidates = [source] if source.is_file() else sorted(p for p in source.rglob("*") if p.is_file())
    seen: set[str] = set()
    records: list[EmailRecord] = []
    for p in candidates:
        ext = p.suffix.lower()  # Windows에서 저장한 .EML/.MSG 대문자 확장자도 인식
        parser = SUPPORTED_EXTS.get(ext)
        if parser is None:
            if not p.name.startswith("."):
                report.unsupported[ext] = report.unsupported.get(ext, 0) + 1
            continue
        report.found += 1
        try:
            rec = parser(p, strip_quotes=strip_quotes)
        except Exception as exc:  # a single corrupt file shouldn't stop the run
            report.failed.append((str(p), f"{type(exc).__name__}: {exc}"))
            continue
        key = rec.message_id if rec.message_id.startswith("<") else str(p.resolve())
        if key in seen:
            report.duplicates += 1
            continue
        seen.add(key)
        if rec.date is None:
            report.no_date += 1
        elif since and rec.date < since:
            report.too_old.append(rec.date)
            continue
        elif until and rec.date > until:
            report.too_new.append(rec.date)
            continue
        records.append(rec)
    records.sort(key=lambda r: r.date or datetime.min)
    report.loaded = len(records)
    return records
