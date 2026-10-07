"""Command-line entry point: python -m email_task_agent <eml 폴더> [옵션]"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .agent import EmailTaskAgent
from . import eml_parser
from .eml_parser import MAX_MAIL_AGE_YEARS, LoadReport, load_emails, oldest_allowed
from .llm import DEFAULT_BASE_URL, DEFAULT_MODEL, LLMClient, LLMError
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
    p.add_argument("--max-tokens", type=int, default=8192, help="LLM 응답 최대 토큰")
    p.add_argument("--batch-chars", type=int, default=24000, help="LLM 1회 호출에 넣을 메일 텍스트 최대 글자수")
    p.add_argument("--max-body-chars", type=int, default=6000, help="메일 1건당 본문 최대 글자수")
    p.add_argument("--no-json-schema", action="store_true", help="vLLM guided decoding(json_schema)을 쓰지 않음")
    p.add_argument("--include-done", action="store_true", help="완료된 업무도 표시")
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
    if not args.source:
        parser.error("메일 파일 또는 폴더 경로를 지정하세요.")
    real_now = eml_parser.now()
    today = real_now.date()
    if args.date:
        try:
            today = date.fromisoformat(args.date)
        except ValueError:
            log(f"날짜 형식이 잘못되었습니다: {args.date!r} (예: 2026-10-06)")
            return 2
        if today < real_now.date():
            # 과거 기준일을 허용하면 오늘 기준 2년이 넘은 메일을 읽을 수 있으므로 금지
            log(f"기준일(--date)은 오늘({real_now.date()}) 이전으로 지정할 수 없습니다.")
            return 2

    src = Path(args.source)
    if not src.exists():
        log(f"경로를 찾을 수 없습니다: {src}")
        return 2

    lookback, lookback_label = args.lookback
    if args.lookback_weeks is not None:
        try:
            lookback = _check_limit(timedelta(weeks=args.lookback_weeks))
        except argparse.ArgumentTypeError as exc:
            log(str(exc))
            return 2
        lookback_label = f"{args.lookback_weeks}주"
    until = datetime.combine(today, datetime.max.time())
    # 2년 상한은 load_emails에서도 강제되지만, 표시되는 분석 기간도 맞춰 둠
    since = max(datetime.combine(today - lookback, datetime.min.time()), oldest_allowed(real_now))
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

    out = to_json(checklist) if args.format == "json" else to_markdown(checklist, len(records))
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        log(f"저장 완료: {args.output}")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
