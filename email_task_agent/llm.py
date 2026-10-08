"""Thin client for the on-premise vLLM server (OpenAI-compatible API)."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Type, TypeVar

import urllib.request
from urllib.parse import urlparse

from openai import APIConnectionError, APIStatusError, BadRequestError, DefaultHttpxClient, OpenAI

try:  # openai 2.x 이상
    from openai import Omit
except ImportError:  # 사내 PC 에 많이 깔린 openai 1.x
    from openai._types import Omit
from pydantic import BaseModel, ValidationError

DEFAULT_BASE_URL = "http://75.12.15.121:8000/v1"
DEFAULT_MODEL = "thinkingcap"

T = TypeVar("T", bound=BaseModel)

_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)


class LLMError(RuntimeError):
    pass


class LLMTooLong(LLMError):
    """입력이 모델 컨텍스트를 넘었거나 응답이 max_tokens 에서 잘림 → 메일 묶음을 줄여서 다시 시도해야 함."""


_CONTEXT_ERR = re.compile(r"maximum context length|context length|too many tokens|max_model_len|prompt is too long", re.I)
_CTX_LIMIT = re.compile(r"maximum context length is (\d+)", re.I)
_CTX_PROMPT = re.compile(r"(\d+) in the messages|(\d+) input tokens|prompt contains (\d+)", re.I)
MIN_COMPLETION_TOKENS = 1024


def parse_context_error(message: str) -> tuple[int | None, int | None]:
    """vLLM 컨텍스트 초과 메시지에서 (모델 최대 길이, 입력 토큰 수) 추출."""
    limit = _CTX_LIMIT.search(message)
    prompt = _CTX_PROMPT.search(message)
    return (int(limit.group(1)) if limit else None,
            int(next(g for g in prompt.groups() if g)) if prompt else None)


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def system_proxy_for(url: str) -> str | None:
    """이 PC 의 프록시 설정(환경변수, Windows 인터넷 옵션)상 url 이 거쳐갈 프록시. 없으면 None."""
    host = urlparse(url).hostname or ""
    proxies = urllib.request.getproxies()
    if not proxies or urllib.request.proxy_bypass(host):
        return None
    return proxies.get(urlparse(url).scheme) or proxies.get("all")


def explain_status(code: int, body: str, base_url: str, via_proxy: str | None, model: str) -> str:
    """HTTP 오류를 사용자가 조치할 수 있는 안내문으로 변환."""
    body = " ".join(re.sub(r"<[^>]+>", " ", body or "").split())[:300]
    where = f"서버 {base_url}" + (f" (프록시 {via_proxy} 경유)" if via_proxy else " (프록시 없이 직접 연결)")
    lines = [f"LLM 서버 오류 {code} — {where}", f"서버 응답: {body or '(내용 없음)'}"]
    if code in (401, 403):
        if via_proxy:
            lines.append("· 사내 프록시가 요청을 막고 있을 가능성이 큽니다. --use-proxy / LLM_USE_PROXY 를 끄고 직접 연결하세요.")
        lines += [
            "· vLLM 앞에 인증 게이트웨이(nginx 등)가 있다면 API 키가 필요할 수 있습니다: 환경변수 LLM_API_KEY 설정",
            "· 서버가 접속 허용 IP를 제한하고 있다면 서버 관리자에게 내 PC IP 허용을 요청하세요.",
            "· 연결 진단: python -m email_task_agent --check-llm",
        ]
    elif code == 404:
        lines.append(f"· 주소 끝이 /v1 인지, 모델 이름 '{model}' 이 서버에 있는지 확인하세요 (--check-llm 으로 모델 목록 확인).")
    elif code == 407:
        lines.append("· 프록시 인증이 필요합니다. LLM 서버는 사내망이므로 프록시 없이 직접 연결하세요 (LLM_USE_PROXY 해제).")
    elif code >= 500:
        lines.append("· LLM 서버 내부 오류입니다. 잠시 후 다시 시도하거나 --batch-chars 를 줄여 보세요.")
    return "\n".join(lines)


def extract_json(text: str) -> Any:
    """Pull the JSON object out of a model reply (handles <think> blocks and ``` fences)."""
    text = _THINK_RE.sub("", text or "")
    # A reasoning model may emit only a closing tag when the opening one is in the template.
    if "</think>" in text:
        text = text.split("</think>")[-1]
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise LLMError(f"응답에서 JSON을 찾지 못했습니다: {text[:300]!r}")
    return json.loads(text[start : end + 1])


class LLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 8192,
        timeout: float = 600.0,
        max_retries: int = 2,
        use_json_schema: bool = True,
        use_proxy: bool | None = None,
        api_key: str | None = None,
    ):
        self.model = model or os.getenv("LLM_MODEL", DEFAULT_MODEL)
        self.base_url = (base_url or os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.use_json_schema = use_json_schema
        self.context_limit: int | None = None  # 서버 오류 메시지에서 알아낸 모델 최대 길이(토큰)
        # 사내 LLM 서버는 내부망이므로 기본적으로 PC 의 프록시 설정을 무시하고 직접 연결.
        # (Windows 인터넷 옵션/HTTP_PROXY 의 사내 프록시를 거치면 403 으로 막히는 경우가 많음)
        self.use_proxy = _env_flag("LLM_USE_PROXY") if use_proxy is None else use_proxy
        self.via_proxy = system_proxy_for(self.base_url) if self.use_proxy else None
        api_key = api_key if api_key is not None else os.getenv("LLM_API_KEY", "")
        self.client = OpenAI(
            base_url=self.base_url,
            api_key=api_key or "EMPTY",
            timeout=timeout,
            max_retries=max_retries,
            http_client=DefaultHttpxClient(trust_env=self.use_proxy, timeout=timeout),
        )
        # 키가 없으면 Authorization 헤더 자체를 보내지 않음 ('Bearer EMPTY' 를 거부하는 게이트웨이 대응).
        # SDK 는 요청별 헤더의 Omit 만 인정하므로 매 요청에 전달.
        self._headers = None if api_key else {"Authorization": Omit()}

    def _status_error(self, exc: APIStatusError) -> LLMError:
        try:
            body = exc.response.text
        except Exception:
            body = str(exc.message)
        return LLMError(explain_status(exc.status_code, body, self.base_url, self.via_proxy, self.model))

    def check_connection(self) -> tuple[bool, list[str]]:
        """서버 연결·모델 존재 여부 진단. (성공 여부, 출력할 줄 목록)"""
        lines = [f"LLM 서버: {self.base_url}", f"모델: {self.model}"]
        system_proxy = system_proxy_for(self.base_url)
        if self.via_proxy:
            lines.append(f"연결 방식: 프록시 {self.via_proxy} 경유 (LLM_USE_PROXY)")
        else:
            lines.append("연결 방식: 직접 연결 (PC 프록시 설정 무시)"
                         + (f" — 참고: 이 PC 에는 프록시 {system_proxy} 가 설정되어 있음" if system_proxy else ""))
        try:
            models = [m.id for m in self.client.models.list(extra_headers=self._headers).data]
        except APIStatusError as exc:
            return False, lines + [str(self._status_error(exc))]
        except APIConnectionError as exc:
            return False, lines + [
                f"연결 실패: {exc.__cause__ or exc}",
                "· 주소/포트가 맞는지, 사내망(VPN)에 연결되어 있는지 확인하세요.",
            ]
        lines.append(f"서버의 모델 목록: {', '.join(models) or '(없음)'}")
        if self.model not in models:
            return False, lines + [f"· 모델 '{self.model}' 이 서버에 없습니다. --model 로 위 목록의 이름을 지정하세요."]
        try:
            self.client.chat.completions.create(
                model=self.model, messages=[{"role": "user", "content": "ping"}], max_tokens=1,
                extra_headers=self._headers)
        except APIStatusError as exc:
            return False, lines + [str(self._status_error(exc))]
        except APIConnectionError as exc:
            return False, lines + [f"연결 실패: {exc.__cause__ or exc}"]
        return True, lines + ["✅ 연결 성공: 모델 응답 확인"]

    def _complete(self, messages: list[dict], schema_model: Type[BaseModel] | None) -> str:
        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        if schema_model is not None and self.use_json_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_model.__name__,
                    "schema": schema_model.model_json_schema(),
                },
            }
        if self.context_limit:  # 이전에 알아낸 모델 최대 길이를 넘지 않게 응답 길이 상한 조정
            kwargs["max_tokens"] = min(kwargs["max_tokens"], self.context_limit - MIN_COMPLETION_TOKENS // 4)
        try:
            resp = self.client.chat.completions.create(**kwargs, extra_headers=self._headers)
        except BadRequestError as exc:
            if _CONTEXT_ERR.search(str(exc)):
                limit, prompt = parse_context_error(str(exc))
                if limit:
                    self.context_limit = limit
                room = (limit - prompt - 32) if limit and prompt else 0
                if room >= MIN_COMPLETION_TOKENS and room < kwargs["max_tokens"]:
                    # 입력은 들어가지만 응답 자리(max_tokens)가 부족 → 응답 길이를 줄여 바로 재시도
                    kwargs["max_tokens"] = room
                    try:
                        resp = self.client.chat.completions.create(**kwargs, extra_headers=self._headers)
                    except BadRequestError as exc2:
                        if _CONTEXT_ERR.search(str(exc2)):
                            raise LLMTooLong(self._too_long_msg(limit, prompt)) from exc2
                        raise
                    return self._content(resp, kwargs["max_tokens"])
                raise LLMTooLong(self._too_long_msg(limit, prompt)) from exc
            if "response_format" not in kwargs:
                raise
            # 서버가 guided decoding(json_schema)을 지원하지 않으면 프롬프트 기반 JSON으로 재시도
            self.use_json_schema = False
            kwargs.pop("response_format")
            resp = self.client.chat.completions.create(**kwargs, extra_headers=self._headers)
        return self._content(resp, kwargs["max_tokens"])

    @staticmethod
    def _too_long_msg(limit: int | None, prompt: int | None) -> str:
        if limit and prompt:
            return f"입력({prompt}토큰)이 모델 최대 길이({limit}토큰)에 비해 김"
        return "입력이 모델 최대 길이를 넘음"

    @staticmethod
    def _content(resp, max_tokens: int) -> str:
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            # 같은 크기로 재시도해도 또 잘리므로 호출한 쪽에서 묶음을 나눠 다시 요청하게 함
            raise LLMTooLong(f"응답이 최대 길이({max_tokens}토큰)에서 잘림")
        return choice.message.content or ""

    def chat_structured(self, system: str, user: str, schema_model: Type[T]) -> T:
        schema_hint = json.dumps(schema_model.model_json_schema(), ensure_ascii=False)
        messages = [
            {
                "role": "system",
                "content": f"{system}\n\n반드시 아래 JSON 스키마를 따르는 JSON 객체 하나만 출력하세요. "
                f"설명 문장이나 마크다운 없이 JSON만 출력합니다.\n스키마:\n{schema_hint}",
            },
            {"role": "user", "content": user},
        ]
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            raw = ""
            try:
                raw = self._complete(messages, schema_model)
                return schema_model.model_validate(extract_json(raw))
            except LLMTooLong:
                raise  # 같은 크기로 재시도하지 않고 호출한 쪽에서 묶음을 나눔
            except (LLMError, json.JSONDecodeError, ValidationError) as exc:
                last_err = exc
                messages = messages[:2] + [
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": f"출력이 유효한 JSON이 아니거나 스키마와 맞지 않습니다 ({exc}). "
                        "스키마에 맞는 JSON 객체만 다시 출력하세요.",
                    },
                ]
            except APIConnectionError as exc:
                last_err = exc
                time.sleep(2 ** attempt)
            except APIStatusError as exc:
                if _CONTEXT_ERR.search(str(exc)):
                    raise LLMTooLong(self._too_long_msg(*parse_context_error(str(exc)))) from exc
                raise self._status_error(exc) from exc
        if isinstance(last_err, APIConnectionError):
            raise LLMError(
                f"LLM 서버에 연결할 수 없습니다: {self.base_url} ({last_err.__cause__ or last_err})\n"
                "· 주소/포트, 사내망(VPN) 연결을 확인하세요. 진단: python -m email_task_agent --check-llm"
            )
        raise LLMError(f"LLM 구조화 응답 실패: {last_err}")
