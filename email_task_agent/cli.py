"""Command-line entry point: python -m email_task_agent <eml 폴더> [옵션]"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .agent import EmailTaskAgent
from .eml_parser import LoadReport, load_emails
from .llm import DEFAULT_BASE_URL, DEFAULT_MODEL, LLMClient, LLMError
from .render import to_json, to_markdown


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="email_task_agent",
        description="저장된 .eml/.msg 메일에서 이번 주/다음 주 할 일 체크리스트를 만듭니다.",
    )
    p.add_argument("source", help=".eml/.msg 파일 또는 메일 파일이 들어있는 폴더 (하위 폴더 포함)")
    p.add_argument("--me", default="", help="본인 이름/메일 (예: '홍길동 <gildong@corp.com>')")
    p.add_argument("--date", help="기준일 YYYY-MM-DD (기본: 오늘)")
    p.add_argument("--lookback-weeks", type=int, default=8, help="기준일로부터 몇 주 전 메일까지 볼지 (기본 8)")
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
    return p


def _utf8_console() -> None:
    # 한국어 Windows 콘솔(cp949)은 이모지/일부 문자를 출력하지 못해 UnicodeEncodeError로 중단됨
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    args = build_parser().parse_args(argv)
    today = date.fromisoformat(args.date) if args.date else datetime.now().date()
    log = lambda msg: print(msg, file=sys.stderr)  # noqa: E731

    src = Path(args.source)
    if not src.exists():
        log(f"경로를 찾을 수 없습니다: {src}")
        return 2

    since = datetime.combine(today - timedelta(weeks=args.lookback_weeks), datetime.min.time())
    until = datetime.combine(today, datetime.max.time())
    report = LoadReport()
    records = load_emails(src, since=since, until=until, strip_quotes=not args.keep_quotes, report=report)
    log(f"분석 기간: {since.date()} ~ {today}")
    log(report.summary(since, until))
    for hint in report.hints(args.lookback_weeks):
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
        llm, me=args.me, batch_chars=args.batch_chars, max_body_chars=args.max_body_chars, log=log
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
