"""Thin client for the on-premise vLLM server (OpenAI-compatible API)."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Type, TypeVar

from openai import APIConnectionError, APIStatusError, BadRequestError, OpenAI
from pydantic import BaseModel, ValidationError

DEFAULT_BASE_URL = "http://75.12.15.121:8000/v1"
DEFAULT_MODEL = "thinkingcap"

T = TypeVar("T", bound=BaseModel)

_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)


class LLMError(RuntimeError):
    pass


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
    ):
        self.model = model or os.getenv("LLM_MODEL", DEFAULT_MODEL)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.use_json_schema = use_json_schema
        self.client = OpenAI(
            base_url=base_url or os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL),
            api_key=os.getenv("LLM_API_KEY", "EMPTY"),  # vLLM 서버는 키를 검사하지 않음
            timeout=timeout,
            max_retries=max_retries,
        )

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
        try:
            resp = self.client.chat.completions.create(**kwargs)
        except BadRequestError:
            if "response_format" not in kwargs:
                raise
            # 서버가 guided decoding(json_schema)을 지원하지 않으면 프롬프트 기반 JSON으로 재시도
            self.use_json_schema = False
            kwargs.pop("response_format")
            resp = self.client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            print("[경고] 응답이 max_tokens에서 잘렸습니다. --max-tokens 또는 --batch-chars 를 조정하세요.")
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
                raise LLMError(f"LLM 서버 오류 {exc.status_code}: {exc.message}") from exc
        raise LLMError(f"LLM 구조화 응답 실패: {last_err}")
