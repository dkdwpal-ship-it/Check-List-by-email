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


_KO_DATE = re.compile(
    r"(\d{4})\s*(?:년|[./-])\s*(\d{1,2})\s*(?:월|[./-])\s*(\d{1,2})\s*일?\.?"  # 2026년 10월 2일 / 2026. 10. 2.
    r"(?:\s*\(?[월화수목금토일]\)?(?:요일)?)?"                                   # (금) / 금요일
    r"(?:\s*(오전|오후|AM|PM|am|pm)?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?"        # 오전 10:12 / 10:12:00
)


def parse_mail_date(value: str | None) -> datetime | None:
    """RFC 2822 날짜 외에 ISO('2026-10-02 10:12'), 한국어('2026년 10월 2일 금요일 오후 2:30') 형식도 해석."""
    if not value:
        return None
    value = str(value).strip()
    try:
        # 발신자 현지 시각을 유지: '내일', '다음주 금요일' 같은 표현은 발신자 기준이므로
        return parsedate_to_datetime(value).replace(tzinfo=None)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    m = _KO_DATE.search(value)
    if not m:
        return None
    y, mo, d, ampm, hh, mm, ss = m.groups()
    hour = int(hh or 0)
    if ampm in ("오후", "PM", "pm") and hour < 12:
        hour += 12
    elif ampm in ("오전", "AM", "am") and hour == 12:
        hour = 0
    try:
        return datetime(int(y), int(mo), int(d), hour, int(mm or 0), int(ss or 0))
    except ValueError:
        return None


def _message_date(msg: EmailMessage) -> datetime | None:
    """Date 헤더 → 대체 날짜 헤더 → Received 헤더(가장 처음 받은 서버) 순으로 발송 시각을 찾음."""
    for header in ("Date", "Sent", "X-Original-Date", "Resent-Date", "Delivery-Date"):
        for value in msg.get_all(header, []):
            found = parse_mail_date(str(value))
            if found:
                return found
    for value in reversed(msg.get_all("Received", [])):  # 맨 아래 Received 가 발송 시점에 가장 가까움
        found = parse_mail_date(str(value).rsplit(";", 1)[-1])
        if found:
            return found
    return None


def _normalize_raw(raw: bytes) -> bytes:
    """메모장/일부 내보내기 도구가 붙이는 BOM, UTF-16 인코딩, 앞쪽 빈 줄을 정리해 헤더가 읽히게 함."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raw = raw.decode("utf-16").encode("utf-8")
    elif len(raw) > 4 and raw[1:4:2] == b"\x00\x00" and raw[0] != 0:  # BOM 없는 UTF-16LE
        raw = raw.decode("utf-16-le", errors="replace").encode("utf-8")
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.lstrip(b"\r\n\t ")


def parse_eml(path: str | Path, strip_quotes: bool = True) -> EmailRecord:
    path = Path(path)
    raw = _normalize_raw(path.read_bytes())
    msg = email.message_from_bytes(raw, policy=policy.default)
    date = _message_date(msg)

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

MAX_MAIL_AGE_YEARS = 2  # 오늘로부터 2년이 넘은 메일은 어떤 옵션으로도 읽지 않음


def now() -> datetime:
    """현재 시각. 2년 상한의 기준이며 인자로 바꿀 수 없음 (테스트에서만 monkeypatch)."""
    return datetime.now()


def oldest_allowed(reference: datetime) -> datetime:
    """기준일로부터 정확히 2년 전 0시 (2월 29일은 2월 28일로 처리)."""
    year = reference.year - MAX_MAIL_AGE_YEARS
    try:
        floor = reference.replace(year=year)
    except ValueError:
        floor = reference.replace(year=year, day=28)
    return floor.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass
class LoadReport:
    """Why each file under the source was or was not used — shown to the user when mails go missing."""

    found: int = 0
    loaded: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    duplicates: int = 0
    too_old: list[datetime] = field(default_factory=list)
    over_limit: list[datetime] = field(default_factory=list)  # 2년 초과 (읽기 금지)
    too_new: list[datetime] = field(default_factory=list)
    no_date: list[str] = field(default_factory=list)  # 발송 날짜를 알 수 없어 제외한 파일
    unsupported: dict[str, int] = field(default_factory=dict)

    def summary(self, since: datetime | None = None, until: datetime | None = None) -> str:
        lines = [f"메일 파일 {self.found}개 발견 → {self.loaded}건 사용"]
        if self.too_old:
            lines.append(
                f"  - 기간 이전이라 제외: {len(self.too_old)}건 (가장 최근 {max(self.too_old):%Y-%m-%d})"
                + (f", 분석 시작일 {since:%Y-%m-%d}" if since else "")
            )
        if self.over_limit:
            lines.append(
                f"  - {MAX_MAIL_AGE_YEARS}년이 지나 읽지 않음: {len(self.over_limit)}건 "
                f"(가장 최근 {max(self.over_limit):%Y-%m-%d})"
            )
        if self.too_new:
            lines.append(
                f"  - 기준일 이후라 제외: {len(self.too_new)}건 (가장 이른 {min(self.too_new):%Y-%m-%d})"
                + (f", 기준일 {until:%Y-%m-%d}" if until else "")
            )
        if self.duplicates:
            lines.append(f"  - 중복(같은 Message-ID) 제외: {self.duplicates}건")
        if self.no_date:
            names = ", ".join(Path(p).name for p in self.no_date[:5])
            more = f" 외 {len(self.no_date) - 5}건" if len(self.no_date) > 5 else ""
            lines.append(f"  - 날짜 정보가 없어 제외: {len(self.no_date)}건 ({names}{more})")
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
                "--lookback 값을 늘려 주세요(최대 2y)."
            )
        if self.over_limit and not self.too_old and self.loaded == 0:
            out.append(f"모든 메일이 {MAX_MAIL_AGE_YEARS}년 이상 지난 메일입니다. {MAX_MAIL_AGE_YEARS}년이 지난 메일은 분석하지 않습니다.")
        if self.no_date and self.loaded == 0:
            out.append("발송 날짜(Date 헤더)가 없거나 잘못된 메일은 2년 이내인지 확인할 수 없어 분석하지 않습니다.")
        if self.too_new and self.loaded == 0:
            out.append("메일 날짜가 기준일보다 미래입니다. 메일의 발송 날짜나 PC 날짜 설정을 확인하세요.")
        return out


def load_emails(
    source: str | Path,
    since: datetime | None = None,
    until: datetime | None = None,
    strip_quotes: bool = True,
    report: LoadReport | None = None,
) -> list[EmailRecord]:
    """Load every .eml/.msg under `source` (file or directory), deduplicated and sorted by date.

    Mails older than MAX_MAIL_AGE_YEARS before the current time are never loaded, whatever `since`/`until` are,
    and neither are mails without a usable send date, since their age can't be checked.
    Pass a LoadReport to learn which files were skipped and why.
    """
    floor = oldest_allowed(now())
    if since is None or since < floor:
        since = floor
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
            # 나이를 확인할 수 없으므로 2년 상한을 지키기 위해 제외
            report.no_date.append(str(p))
            continue
        if rec.date < floor:
            report.over_limit.append(rec.date)
            continue
        elif rec.date < since:
            report.too_old.append(rec.date)
            continue
        elif until and rec.date > until:
            report.too_new.append(rec.date)
            continue
        records.append(rec)
    records.sort(key=lambda r: r.date)
    report.loaded = len(records)
    return records
