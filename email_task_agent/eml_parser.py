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
    date_source: str = ""  # 날짜를 찾은 위치 (Date 헤더 / Received 헤더 / 본문)
    date_problem: str = ""  # 날짜를 못 찾은 이유

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


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE_PATTERNS = [
    # 20260304143000 (구분자 없는 14자리)
    (re.compile(r"\b((?:19|20)\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})?\b"), "ymd_compact"),
    # 2026년 3월 4일 / 2026-03-04 / 2026. 3. 4. / 2026/03/04
    (re.compile(r"((?:19|20)\d{2})\s*(?:년|[./-])\s*(\d{1,2})\s*(?:월|[./-])\s*(\d{1,2})"), "ymd"),
    # 화, 04 3월 2026 (한국어 Outlook 이 만드는 RFC 형식)
    (re.compile(r"(\d{1,2})\s+(\d{1,2})\s*월\s+((?:19|20)\d{2})"), "d_m_y"),
    # March 4, 2026 / Mar 4 2026
    (re.compile(_MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+((?:19|20)\d{2})", re.I), "mon_d_y"),
    # 4 March 2026
    (re.compile(r"(\d{1,2})\s+" + _MON + r",?\s+((?:19|20)\d{2})", re.I), "d_mon_y"),
    # 03/04/2026 (앞 숫자가 12보다 크면 일/월 순서로 해석)
    (re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-]((?:19|20)\d{2})\b"), "m_d_y"),
]
_TIME = re.compile(r"(오전|오후|AM|PM|am|pm|a\.m\.|p\.m\.)?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM|am|pm|a\.m\.|p\.m\.)?")


def _ymd(kind: str, g: tuple) -> tuple[int, int, int]:
    if kind in ("ymd", "ymd_compact"):
        return int(g[0]), int(g[1]), int(g[2])
    if kind == "d_m_y":
        return int(g[2]), int(g[1]), int(g[0])
    if kind == "mon_d_y":
        return int(g[2]), _MONTHS[g[0].lower()[:3]], int(g[1])
    if kind == "d_mon_y":
        return int(g[2]), _MONTHS[g[1].lower()[:3]], int(g[0])
    a, b, y = int(g[0]), int(g[1]), int(g[2])  # m_d_y
    return (y, b, a) if a > 12 else (y, a, b)


def parse_mail_date(value: str | None) -> datetime | None:
    """메일 날짜 문자열 해석. RFC 2822 외에 ISO, 한국어('2026년 3월 4일 화요일 오후 2:30'),
    한국어 Outlook('화, 04 3월 2026 14:30:00 +0900'), 미국식('03/04/2026 2:30 PM'), 14자리 숫자 등을 지원.
    시간대 정보는 버리고 발신자 현지 시각을 그대로 사용 ('내일', '다음주 금요일' 같은 표현이 발신자 기준이므로)."""
    if not value:
        return None
    value = " ".join(str(value).split())
    has_ampm = re.search(r"(오전|오후|\b[AaPp]\.?[Mm]\.?\b)", value)
    if not has_ampm:
        try:
            return parsedate_to_datetime(value).replace(tzinfo=None)
        except (TypeError, ValueError, IndexError):
            pass
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    for pattern, kind in _DATE_PATTERNS:
        m = pattern.search(value)
        if not m:
            continue
        try:
            y, mo, d = _ymd(kind, m.groups())
            if kind == "ymd_compact":
                hh, mm, ss = int(m.group(4)), int(m.group(5)), int(m.group(6) or 0)
                return datetime(y, mo, d, hh, mm, ss)
            hour = minute = second = 0
            t = _TIME.search(value, m.end()) or _TIME.search(value)
            if t:
                ampm = (t.group(1) or t.group(5) or "").lower().replace(".", "")
                hour, minute, second = int(t.group(2)), int(t.group(3)), int(t.group(4) or 0)
                if ampm in ("오후", "pm") and hour < 12:
                    hour += 12
                elif ampm in ("오전", "am") and hour == 12:
                    hour = 0
            return datetime(y, mo, d, hour, minute, second)
        except (ValueError, KeyError):
            continue
    return None


# 헤더에 날짜가 없을 때 본문 앞부분의 '보낸 날짜: ...' 같은 줄에서 찾음 (그룹웨어/전달 메일 형식)
_BODY_DATE_LINE = re.compile(
    r"^\s*(?:Date|Sent|보낸\s*날짜|날짜|발송\s*일시?|보낸\s*시간|작성\s*일시?|수신\s*일시?|받은\s*날짜)\s*[:：]\s*(.+)$",
    re.M | re.I,
)


def _raw_headers(msg: EmailMessage, name: str) -> list[str]:
    """원본 헤더 문자열. policy.default 는 해석 못한 Date 헤더를 빈 문자열로 바꾸므로 raw 값을 직접 읽음."""
    name = name.lower()
    return [_decode_raw_header(v) for k, v in msg.raw_items() if k.lower() == name]


def _decode_raw_header(value) -> str:
    """raw 헤더의 8비트 문자(UTF-8/CP949)와 =?UTF-8?B?...?= 인코딩을 사람이 읽는 문자열로 복원."""
    from email.header import decode_header, make_header

    text = str(value)
    if any("\udc80" <= ch <= "\udcff" for ch in text):  # 바이너리 파싱 시 surrogateescape 된 바이트
        data = text.encode("ascii", "surrogateescape")
        for enc in ("utf-8", "cp949"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = data.decode("utf-8", errors="replace")
    if "=?" in text:
        try:
            text = str(make_header(decode_header(text)))
        except Exception:
            pass
    return " ".join(text.split())


def _message_date(msg: EmailMessage, body: str = "") -> tuple[datetime | None, str]:
    """(발송 시각, 출처). Date 헤더 → 대체 날짜 헤더 → Received 헤더(최초 수신 서버) → 본문 앞부분 순."""
    for header in ("Date", "Sent", "X-Original-Date", "Resent-Date", "Delivery-Date"):
        for value in _raw_headers(msg, header):
            found = parse_mail_date(value)
            if found:
                return found, f"{header} 헤더"
    for value in reversed(_raw_headers(msg, "Received")):  # 맨 아래 Received 가 발송 시점에 가장 가까움
        found = parse_mail_date(str(value).rsplit(";", 1)[-1])
        if found:
            return found, "Received 헤더"
    head = "\n".join(body.splitlines()[:40])
    for m in _BODY_DATE_LINE.finditer(head):
        found = parse_mail_date(m.group(1))
        if found:
            return found, "본문"
    return None, ""


def describe_missing_date(msg: EmailMessage) -> str:
    """날짜를 찾지 못한 이유 (리포트용)."""
    values = _raw_headers(msg, "Date")
    if values and values[0]:
        return f"Date 값을 해석할 수 없음: {values[0][:60]!r}"
    if values:
        return "Date 값이 비어 있음"
    if not msg.keys():
        return "메일 헤더가 없음 (메일 원본이 아닌 파일일 수 있음)"
    return "Date/Received 헤더 없음"


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
    body = _extract_body(msg)
    if not msg.keys():  # 헤더 없는 텍스트 파일: 전체를 본문으로
        body = raw.decode("utf-8", errors="replace")
    date, date_source = _message_date(msg, body)

    if strip_quotes:
        body = _strip_quoted(body)

    attachments = [
        part.get_filename() for part in msg.iter_attachments() if part.get_filename()
    ]
    senders = _addresses(msg, "From")
    subject = str(msg["Subject"] or "").strip()
    if not msg.keys():  # 헤더 없는 텍스트: 본문 앞부분의 '제목:', '보낸 사람:' 줄 사용
        head = "\n".join(body.splitlines()[:40])
        m = re.search(r"^\s*(?:제목|Subject)\s*[:：]\s*(.+)$", head, re.M | re.I)
        subject = m.group(1).strip() if m else subject
        m = re.search(r"^\s*(?:보낸\s*사람|From)\s*[:：]\s*(.+)$", head, re.M | re.I)
        senders = [m.group(1).strip()] if m else senders
    return EmailRecord(
        path=str(path),
        message_id=str(msg["Message-ID"] or path.resolve()).strip(),
        subject=subject or "(제목 없음)",
        sender=senders[0] if senders else "",
        to=_addresses(msg, "To"),
        cc=_addresses(msg, "Cc"),
        date=date,
        body=body,
        attachments=attachments,
        date_source=date_source,
        date_problem="" if date else describe_missing_date(msg),
    )


def parse_msg(path: str | Path, strip_quotes: bool = True) -> EmailRecord:
    from .msg_parser import read_msg  # olefile is only needed for .msg

    path = Path(path)
    d = read_msg(path)
    body = d["body"] or (_html_to_text(d["html"]) if d["html"] else "")
    date, date_source = d["date"], "메일 속성" if d["date"] else ""
    if date is None:
        date, date_source = _message_date(EmailMessage(), body)
    if strip_quotes:
        body = _strip_quoted(body)
    return EmailRecord(
        path=str(path),
        message_id=d["message_id"] or str(path.resolve()),
        subject=d["subject"].strip() or "(제목 없음)",
        sender=d["sender"],
        to=d["to"],
        cc=d["cc"],
        date=date,
        body=body,
        attachments=d["attachments"],
        date_source=date_source,
        date_problem="" if date else ".msg 에 발송/수신/작성 시각 정보가 없음",
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
class FileStatus:
    path: str
    status: str  # ok / failed / duplicate / no_date / over_limit / too_old / too_new / unsupported
    reason: str = ""
    subject: str = ""
    sender: str = ""
    date: str = ""


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
    no_date: list[tuple[str, str]] = field(default_factory=list)  # (파일, 이유) 발송 날짜를 알 수 없어 제외
    unsupported: dict[str, int] = field(default_factory=dict)
    # 파일별 결과: 경로 → FileStatus (웹 화면에서 파일마다 사용 여부·이유를 보여주기 위함)
    files: dict[str, "FileStatus"] = field(default_factory=dict)

    def _mark(self, path: Path, status: str, reason: str = "", rec: "EmailRecord | None" = None) -> None:
        self.files[str(path)] = FileStatus(
            path=str(path), status=status, reason=reason,
            subject=rec.subject if rec else "", sender=rec.sender if rec else "",
            date=rec.date.strftime("%Y-%m-%d %H:%M") if rec and rec.date else "",
        )

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
            lines.append(f"  - 날짜 정보가 없어 제외: {len(self.no_date)}건")
            for path, why in self.no_date[:5]:
                lines.append(f"      · {Path(path).name}: {why}")
            if len(self.no_date) > 5:
                lines.append(f"      · 외 {len(self.no_date) - 5}건")
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
            out.append(
                "발송 날짜를 찾지 못한 메일은 2년 이내인지 확인할 수 없어 분석하지 않습니다. "
                "--inspect <파일> 로 해당 메일의 헤더를 확인할 수 있습니다."
            )
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
                report._mark(p, "unsupported", f"지원하지 않는 형식 ({ext or '확장자 없음'}) — .eml/.msg 만 가능")
            continue
        report.found += 1
        try:
            rec = parser(p, strip_quotes=strip_quotes)
        except Exception as exc:  # a single corrupt file shouldn't stop the run
            report.failed.append((str(p), f"{type(exc).__name__}: {exc}"))
            report._mark(p, "failed", f"읽기 실패: {type(exc).__name__}: {exc}")
            continue
        key = rec.message_id if rec.message_id.startswith("<") else str(p.resolve())
        if key in seen:
            report.duplicates += 1
            report._mark(p, "duplicate", "같은 메일(Message-ID)이 이미 있음", rec)
            continue
        seen.add(key)
        if rec.date is None:
            # 나이를 확인할 수 없으므로 2년 상한을 지키기 위해 제외
            report.no_date.append((str(p), rec.date_problem or "날짜 정보 없음"))
            report._mark(p, "no_date", f"날짜 정보 없음 — {rec.date_problem or '발송 날짜를 찾지 못함'}", rec)
            continue
        if rec.date < floor:
            report.over_limit.append(rec.date)
            report._mark(p, "over_limit", f"{MAX_MAIL_AGE_YEARS}년이 지난 메일은 읽지 않음", rec)
            continue
        elif rec.date < since:
            report.too_old.append(rec.date)
            report._mark(p, "too_old", f"분석 기간({since:%Y-%m-%d} 이후) 이전 메일", rec)
            continue
        elif until and rec.date > until:
            report.too_new.append(rec.date)
            report._mark(p, "too_new", "기준일보다 미래 날짜의 메일", rec)
            continue
        report._mark(p, "ok", "", rec)
        records.append(rec)
    records.sort(key=lambda r: r.date)
    report.loaded = len(records)
    return records
