"""Read Outlook .msg files (OLE compound files, MS-OXMSG) without extra native dependencies.

Outlook desktop saves mail as .msg when dragged to a folder or via "Save As", so supporting it
matters as much as .eml. Only the properties needed for task extraction are read.
"""

from __future__ import annotations

import email
import struct
from datetime import datetime, timedelta, timezone
from email import policy
from email.utils import getaddresses, parsedate_to_datetime

import olefile

# MAPI property ids
PR_SUBJECT = 0x0037
PR_TRANSPORT_HEADERS = 0x007D
PR_SENDER_NAME = 0x0C1A
PR_SENDER_EMAIL = 0x0C1F
PR_SENDER_SMTP = 0x5D01
PR_DISPLAY_CC = 0x0E03
PR_DISPLAY_TO = 0x0E04
PR_BODY = 0x1000
PR_HTML = 0x1013
PR_INTERNET_MESSAGE_ID = 0x1035
PR_CLIENT_SUBMIT_TIME = 0x0039
PR_MESSAGE_DELIVERY_TIME = 0x0E06
PR_CREATION_TIME = 0x3007
PR_MESSAGE_CODEPAGE = 0x3FFD
PR_INTERNET_CPID = 0x3FDE
PR_RECIPIENT_TYPE = 0x0C15
PR_DISPLAY_NAME = 0x3001
PR_EMAIL_ADDRESS = 0x3003
PR_SMTP_ADDRESS = 0x39FE
PR_ATTACH_LONG_FILENAME = 0x3707
PR_ATTACH_FILENAME = 0x3704

PT_UNICODE = 0x001F
PT_STRING8 = 0x001E
PT_BINARY = 0x0102
PT_SYSTIME = 0x0040
PT_LONG = 0x0003

_CODEPAGES = {949: "cp949", 51949: "euc-kr", 65001: "utf-8", 1252: "cp1252", 932: "cp932", 936: "gbk", 50225: "iso2022_kr"}


class MsgFile:
    def __init__(self, path):
        self.ole = olefile.OleFileIO(str(path))
        self.codepage = None
        props = self._fixed_props("", 32)
        cp = props.get(PR_INTERNET_CPID) or props.get(PR_MESSAGE_CODEPAGE)
        if isinstance(cp, int):
            self.codepage = _CODEPAGES.get(cp, f"cp{cp}")

    def close(self):
        self.ole.close()

    # ---- raw property access -------------------------------------------------
    def _stream(self, storage: str, prop: int, ptype: int) -> bytes | None:
        name = f"{storage}__substg1.0_{prop:04X}{ptype:04X}"
        return self.ole.openstream(name).read() if self.ole.exists(name) else None

    def _decode8(self, raw: bytes) -> str:
        raw = raw.rstrip(b"\x00")
        for enc in (self.codepage, "utf-8", "cp949", "latin-1"):
            if not enc:
                continue
            try:
                return raw.decode(enc)
            except (LookupError, UnicodeDecodeError):
                continue
        return raw.decode("utf-8", errors="replace")

    def string(self, prop: int, storage: str = "") -> str:
        raw = self._stream(storage, prop, PT_UNICODE)
        if raw is not None:
            return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
        raw = self._stream(storage, prop, PT_STRING8)
        return self._decode8(raw) if raw is not None else ""

    def binary(self, prop: int, storage: str = "") -> bytes | None:
        return self._stream(storage, prop, PT_BINARY)

    def _fixed_props(self, storage: str, header_size: int) -> dict[int, object]:
        """Fixed-size values (times, integers) live in the __properties_version1.0 stream."""
        name = f"{storage}__properties_version1.0"
        if not self.ole.exists(name):
            return {}
        data = self.ole.openstream(name).read()
        out: dict[int, object] = {}
        for off in range(header_size, len(data) - 15, 16):
            ptype, pid = struct.unpack_from("<HH", data, off)
            value = data[off + 8 : off + 16]
            if ptype == PT_SYSTIME:
                ft = struct.unpack("<Q", value)[0]
                if ft:
                    out[pid] = datetime(1601, 1, 1) + timedelta(microseconds=ft // 10)
            elif ptype == PT_LONG:
                out[pid] = struct.unpack_from("<i", value)[0]
        return out

    # ---- message level -------------------------------------------------------
    def headers(self) -> email.message.EmailMessage | None:
        raw = self.string(PR_TRANSPORT_HEADERS)
        if not raw.strip():
            return None
        return email.message_from_string(raw.strip() + "\n\n", policy=policy.default)

    def recipients(self) -> tuple[list[str], list[str]]:
        to, cc = [], []
        for entry in self.ole.listdir(streams=False, storages=True):
            if len(entry) != 1 or not entry[0].startswith("__recip_version1.0_"):
                continue
            storage = entry[0] + "/"
            name = self.string(PR_DISPLAY_NAME, storage)
            addr = self.string(PR_SMTP_ADDRESS, storage) or self.string(PR_EMAIL_ADDRESS, storage)
            if "@" not in addr:  # Exchange X.500 address
                addr = ""
            label = f"{name} <{addr}>" if name and addr and name != addr else (addr or name)
            rtype = self._fixed_props(storage, 8).get(PR_RECIPIENT_TYPE, 1)
            (cc if rtype == 2 else to if rtype == 1 else []).append(label)
        return to, cc

    def attachment_names(self) -> list[str]:
        names = []
        for entry in self.ole.listdir(streams=False, storages=True):
            if len(entry) == 1 and entry[0].startswith("__attach_version1.0_"):
                storage = entry[0] + "/"
                n = self.string(PR_ATTACH_LONG_FILENAME, storage) or self.string(PR_ATTACH_FILENAME, storage)
                if n:
                    names.append(n)
        return names

    def date(self) -> datetime | None:
        hdr = self.headers()
        if hdr is not None and hdr["Date"]:
            try:
                # 발신자 현지 시각 유지 (eml_parser와 동일한 기준)
                return parsedate_to_datetime(str(hdr["Date"])).replace(tzinfo=None)
            except (TypeError, ValueError):
                pass
        props = self._fixed_props("", 32)
        for pid in (PR_CLIENT_SUBMIT_TIME, PR_MESSAGE_DELIVERY_TIME, PR_CREATION_TIME):
            if pid in props:
                # .msg 내부 시각은 UTC → PC 현지 시각으로 변환
                utc = props[pid].replace(tzinfo=timezone.utc)
                return utc.astimezone().replace(tzinfo=None)
        return None


def read_msg(path) -> dict:
    """Return the fields EmailRecord needs: subject, sender, to, cc, date, message_id, body, html, attachments."""
    m = MsgFile(path)
    try:
        hdr = m.headers()
        sender_addr = m.string(PR_SENDER_SMTP) or m.string(PR_SENDER_EMAIL)
        if "@" not in sender_addr:
            sender_addr = ""
        sender_name = m.string(PR_SENDER_NAME)
        sender = f"{sender_name} <{sender_addr}>" if sender_name and sender_addr else (sender_addr or sender_name)
        if not sender and hdr is not None and hdr["From"]:
            sender = str(hdr["From"])

        to, cc = m.recipients()
        if not to and hdr is not None:
            to = [f"{n} <{a}>" if n else a for n, a in getaddresses([str(v) for v in hdr.get_all("To", [])])]
        if not to:
            to = [s.strip() for s in m.string(PR_DISPLAY_TO).split(";") if s.strip()]
        if not cc:
            cc = [s.strip() for s in m.string(PR_DISPLAY_CC).split(";") if s.strip()]

        html_raw = m.binary(PR_HTML)
        html = ""
        if html_raw:
            html = m._decode8(html_raw)
        elif m.string(PR_HTML):
            html = m.string(PR_HTML)

        message_id = m.string(PR_INTERNET_MESSAGE_ID)
        if not message_id and hdr is not None and hdr["Message-ID"]:
            message_id = str(hdr["Message-ID"])

        return {
            "subject": m.string(PR_SUBJECT),
            "sender": sender,
            "to": to,
            "cc": cc,
            "date": m.date(),
            "message_id": message_id.strip(),
            "body": m.string(PR_BODY),
            "html": html,
            "attachments": m.attachment_names(),
        }
    finally:
        m.close()
