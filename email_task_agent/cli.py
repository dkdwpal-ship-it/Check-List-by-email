"""Command-line entry point: python -m email_task_agent <eml 폴더> [옵션]"""

from __future__ import annotations

if __package__ in (None, ""):
    # VS Code 의 ▶(Run Python File) 처럼 이 파일을 직접 실행한 경우: 패키지 경로를 잡아 상대 import 가 되게 함
    import pathlib
    import sys as _sys

    _sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    __package__ = "email_task_agent"  # noqa: A001
    import email_task_agent  # noqa: F401,E402

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .agent import EmailTaskAgent
from . import eml_parser
from .eml_parser import MAX_MAIL_AGE_YEARS, LoadReport, load_emails, oldest_allowed
from .llm import DEFAULT_BASE_URL, DEFAULT_MODEL, LLMClient, LLMError
from .digest import build_daily_digest, digest_to_markdown
from .topics import build_keyword_index, summarize_periods, topics_to_markdown
from .render import to_json, to_markdown


DEFAULT_LOOKBACK = "2y"
_UNIT_DAYS = {"y": 365, "m": 30, "w": 7, "d": 1}
_UNIT_KO = {"y": "년", "m": "개월", "w": "주", "d": "일"}


def parse_lookback(text: str) -> tuple[timedelta, str]:
    """'2y' / '6m' / '8w' / '30d' (또는 '2년', '6개월') → (기간, 표시용 문자열)."""
    t = text.strip().lower().replace("년", "y").replace("개월", "m").replace("주", "w").replace("일", "d")
    m = re.fullmatch(r"(\d+)\s*([ymwd]?)", t)
    if not m or int(m.group(1)) <= 0:
        raise argparse.ArgumentTypeError(f"기간 형식이 잘못되었습니다: {text!r} (예: 2y, 6m, 8w, 30d)")
    n, unit = int(m.group(1)), m.group(2) or "w"
    return _check_limit(timedelta(days=n * _UNIT_DAYS[unit])), f"{n}{_UNIT_KO[unit]}"


def _check_limit(lookback: timedelta) -> timedelta:
    if lookback.days > 366 * MAX_MAIL_AGE_YEARS:
        raise argparse.ArgumentTypeError(f"분석 기간은 최대 {MAX_MAIL_AGE_YEARS}년까지만 지정할 수 있습니다.")
    return lookback


@dataclass
class Window:
    today: date
    since: datetime
    until: datetime
    label: str


def resolve_window(date_text: str | None = None, lookback: tuple[timedelta, str] | str | None = None,
                   lookback_weeks: int | None = None) -> Window:
    """기준일·분석 기간을 검증해 계산. CLI 와 웹 화면이 같은 규칙을 쓰도록 공유.

    - 기준일은 오늘 이전으로 지정할 수 없음 (오늘 기준 2년 상한을 우회하지 못하게)
    - 분석 기간은 최대 2년
    잘못된 값이면 ValueError(사용자에게 보여줄 메시지).
    """
    real_now = eml_parser.now()
    today = real_now.date()
    if date_text:
        try:
            today = date.fromisoformat(date_text)
        except ValueError:
            raise ValueError(f"날짜 형식이 잘못되었습니다: {date_text!r} (예: 2026-10-06)") from None
        if today < real_now.date():
            raise ValueError(f"기준일은 오늘({real_now.date()}) 이전으로 지정할 수 없습니다.")
    try:
        if lookback_weeks is not None:
            period, label = _check_limit(timedelta(weeks=lookback_weeks)), f"{lookback_weeks}주"
        elif isinstance(lookback, tuple):
            period, label = lookback
        else:
            period, label = parse_lookback(lookback or DEFAULT_LOOKBACK)
    except argparse.ArgumentTypeError as exc:
        raise ValueError(str(exc)) from None
    until = datetime.combine(today, datetime.max.time())
    # 2년 상한은 load_emails에서도 강제되지만, 표시되는 분석 기간도 맞춰 둠
    since = max(datetime.combine(today - period, datetime.min.time()), oldest_allowed(real_now))
    return Window(today, since, until, label)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="email_task_agent",
        description="저장된 .eml/.msg 메일에서 이번 주/다음 주 할 일 체크리스트를 만듭니다.",
    )
    p.add_argument("source", nargs="?", help=".eml/.msg 파일 또는 메일 파일이 들어있는 폴더 (하위 폴더 포함)")
    p.add_argument("--me", default="", help="본인 이름/메일 (예: '홍길동 <gildong@corp.com>')")
    p.add_argument("--date", help="기준일 YYYY-MM-DD (기본: 오늘, 오늘 이전 날짜는 지정 불가)")
    p.add_argument(
        "--lookback", type=parse_lookback, default=parse_lookback(DEFAULT_LOOKBACK),
        help=f"기준일로부터 얼마나 지난 메일까지 볼지. 예: 2y, 6m, 8w, 30d (기본·최대 {DEFAULT_LOOKBACK} = 2년)",
    )
    p.add_argument("--lookback-weeks", type=int, default=None, help=argparse.SUPPRESS)  # 이전 옵션 호환
    p.add_argument(
        "--stale-weeks", type=int, default=8,
        help="기한이 이 기간(주) 이상 지난 일회성 업무, 이 기간 동안 메일에 안 나온 반복 업무는 제외 (기본 8)",
    )
    p.add_argument("--base-url", default=None, help=f"vLLM 서버 주소 (기본 {DEFAULT_BASE_URL}, 환경변수 LLM_BASE_URL)")
    p.add_argument("--model", default=None, help=f"모델 이름 (기본 {DEFAULT_MODEL}, 환경변수 LLM_MODEL)")
    p.add_argument("--use-proxy", action="store_true",
                   help="LLM 서버 접속에 PC 의 프록시 설정 사용 (기본: 사용 안 함 = 직접 연결, 환경변수 LLM_USE_PROXY)")
    p.add_argument("--check-llm", action="store_true", help="LLM 서버 연결·모델 확인만 하고 종료")
    p.add_argument("--max-tokens", type=int, default=8192, help="LLM 응답 최대 토큰")
    p.add_argument("--batch-chars", type=int, default=24000, help="LLM 1회 호출에 넣을 메일 텍스트 최대 글자수")
    p.add_argument("--max-body-chars", type=int, default=6000, help="메일 1건당 본문 최대 글자수")
    p.add_argument("--no-json-schema", action="store_true", help="vLLM guided decoding(json_schema)을 쓰지 않음")
    p.add_argument("--include-done", action="store_true", help="완료된 업무도 표시")
    p.add_argument("--summary", action="store_true", help="체크리스트 뒤에 일자별 메일 요약도 출력")
    p.add_argument("--topics", action="store_true", help="체크리스트 뒤에 시기별(월별) 키워드 정리도 출력")
    p.add_argument("--keep-quotes", action="store_true", help="회신 메일의 인용 본문을 제거하지 않음")
    p.add_argument("--format", choices=["md", "json"], default="md", help="출력 형식")
    p.add_argument("-o", "--output", help="결과를 저장할 파일 경로 (기본: 화면 출력)")
    p.add_argument("--list-emails", action="store_true", help="LLM 호출 없이 읽어들인 메일 목록만 출력")
    p.add_argument("--inspect", metavar="FILE", help="메일 파일 하나의 헤더와 날짜 인식 결과를 출력 (본문은 출력하지 않음)")
    return p


def _utf8_console() -> None:
    # 한국어 Windows 콘솔(cp949)은 이모지/일부 문자를 출력하지 못해 UnicodeEncodeError로 중단됨
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def inspect_file(path: Path) -> int:
    """날짜를 못 찾는 메일을 진단하기 위한 출력. 본문 내용은 보여주지 않음."""
    import email
    from email import policy

    from .eml_parser import SUPPORTED_EXTS, _normalize_raw, _raw_headers

    if not path.is_file():
        print(f"파일을 찾을 수 없습니다: {path}")
        return 2
    raw = path.read_bytes()
    print(f"파일: {path}  ({len(raw):,} bytes)")
    print(f"앞부분 바이트: {raw[:16]!r}")
    if path.suffix.lower() == ".eml":
        msg = email.message_from_bytes(_normalize_raw(raw), policy=policy.default)
        names = list(dict.fromkeys(msg.keys()))
        print(f"헤더 {len(names)}종: {', '.join(names[:30]) or '(없음)'}")
        for h in ("Date", "Sent", "Delivery-Date", "Received"):
            for v in _raw_headers(msg, h)[:3]:
                print(f"  {h}: {v[:120]}")
    parser = SUPPORTED_EXTS.get(path.suffix.lower())
    if parser is None:
        print(f"지원하지 않는 확장자입니다: {path.suffix or '(없음)'} (.eml/.msg 만 지원)")
        return 2
    try:
        rec = parser(path, strip_quotes=False)
    except Exception as exc:
        print(f"읽기 실패: {type(exc).__name__}: {exc}")
        return 1
    print(f"제목: {rec.subject}")
    if rec.date:
        print(f"인식한 날짜: {rec.date:%Y-%m-%d %H:%M} (출처: {rec.date_source})")
    else:
        print(f"인식한 날짜: 없음 — {rec.date_problem}")
    return 0 if rec.date else 3


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    log = lambda msg: print(msg, file=sys.stderr)  # noqa: E731
    if args.inspect:
        return inspect_file(Path(args.inspect))
    if args.check_llm:
        ok, lines = LLMClient(base_url=args.base_url, model=args.model, max_retries=0,
                              use_proxy=args.use_proxy or None).check_connection()
        print("\n".join(lines))
        return 0 if ok else 1
    if not args.source:
        parser.error("메일 파일 또는 폴더 경로를 지정하세요.")
    try:
        window = resolve_window(args.date, args.lookback, args.lookback_weeks)
    except ValueError as exc:
        log(str(exc).replace("기준일은", "기준일(--date)은"))
        return 2
    today, since, until, lookback_label = window.today, window.since, window.until, window.label

    src = Path(args.source)
    if not src.exists():
        log(f"경로를 찾을 수 없습니다: {src}")
        return 2

    report = LoadReport()
    records = load_emails(src, since=since, until=until, strip_quotes=not args.keep_quotes, report=report)
    log(f"분석 기간: {since.date()} ~ {today} (최근 {lookback_label})")
    log(report.summary(since, until))
    for hint in report.hints(lookback_label):
        log(f"[안내] {hint}")
    if not records:
        log("분석할 메일이 없어 종료합니다.")
        return 3

    if args.list_emails:
        for r in records:
            print(f"{r.date_str}\t{r.sender}\t{r.subject}")
        return 0

    llm = LLMClient(
        base_url=args.base_url,
        model=args.model,
        max_tokens=args.max_tokens,
        use_json_schema=not args.no_json_schema,
        use_proxy=args.use_proxy or None,
    )
    agent = EmailTaskAgent(
        llm, me=args.me, batch_chars=args.batch_chars, max_body_chars=args.max_body_chars,
        stale_weeks=args.stale_weeks, log=log,
    )
    try:
        checklist, _ = agent.run(records, today=today, include_done=args.include_done)
    except LLMError as exc:
        log(f"LLM 호출 실패: {exc}")
        return 1
    except Exception as exc:  # 연결 실패 등
        log(f"오류: {type(exc).__name__}: {exc}")
        return 1

    digest = build_daily_digest(records, agent.summaries) if args.summary else None
    index = overviews = None
    if args.topics:
        index = build_keyword_index(records, agent.summaries)
        overviews, topic_warnings = summarize_periods(llm, index, log=log)
        for w in topic_warnings:
            log(f"[안내] {w}")
    if args.format == "json":
        out = to_json(checklist)
        if digest is not None or index is not None:
            extra = {"checklist": json.loads(out)}
            if digest is not None:
                extra["digest"] = digest
            if index is not None:
                extra["topics"] = {**index, "overviews": overviews}
            out = json.dumps(extra, ensure_ascii=False, indent=2)
    else:
        out = to_markdown(checklist, len(records))
        if digest is not None:
            out += "\n" + digest_to_markdown(digest)
        if index is not None:
            out += "\n" + topics_to_markdown(index, overviews)
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        log(f"저장 완료: {args.output}")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
