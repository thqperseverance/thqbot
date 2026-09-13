"""Direct OpenAI-compatible provider."""

from __future__ import annotations

import os
import uuid
from typing import Any

import json_repair
from openai import AsyncOpenAI

from ithqbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

# httpx 在构造客户端时会解析 NO_PROXY / no_proxy。带方括号的 IPv6 回环条目
# （如 ``[::1]``）会被它当成 "host:port" 解析，从而抛
# ``InvalidURL: Invalid port: ':1]'``，导致**所有** LLM 调用失败。
# 这属于 httpx 的解析缺陷而非配置错误，因此在构造客户端前就地清理这些条目。
_PROXY_LOOPBACK_ENTRIES = frozenset({"::1", "[::1]"})


def sanitize_no_proxy_env(environ: dict[str, str] | None = None) -> bool:
    """移除 NO_PROXY/no_proxy 中的 IPv6 回环条目。

    返回是否发生了修改。幂等，可安全重复调用。
    """
    target = os.environ if environ is None else environ
    changed = False
    for name in ("NO_PROXY", "no_proxy"):
        raw = target.get(name)
        if not raw:
            continue
        kept = [
            item.strip()
            for item in raw.split(",")
            if item.strip() and item.strip() not in _PROXY_LOOPBACK_ENTRIES
        ]
        cleaned = ",".join(kept)
        if cleaned != raw:
            target[name] = cleaned
            changed = True
    return changed


class CustomProvider(LLMProvider):
    _ALLOWED_MESSAGE_KEYS = frozenset({"role", "content", "name", "tool_call_id", "tool_calls"})

    def __init__(
        self,
        api_key: str = "no-key",
        api_base: str = "http://localhost:8000/v1",
        default_model: str = "default",
        extra_headers: dict[str, str] | None = None,
        model_api_bases: dict[str, str] | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        headers = {"x-session-affinity": uuid.uuid4().hex}
        if isinstance(extra_headers, dict):
            headers.update({str(k): str(v) for k, v in extra_headers.items()})
        self._api_key = api_key
        self._default_headers = headers
        self._client_cache: dict[str, AsyncOpenAI] = {}
        self._model_api_bases = self._normalize_model_api_bases(model_api_bases)

    @staticmethod
    def _normalize_model_api_bases(model_api_bases: dict[str, str] | None) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for raw_model, raw_base in (model_api_bases or {}).items():
            model = str(raw_model or "").strip()
            base = str(raw_base or "").strip()
            if model and base:
                normalized[model] = base
        return normalized

    @staticmethod
    def _strip_alias_suffix(model: str) -> str:
        head, sep, tail = model.rpartition("@")
        if not sep or "/" in tail:
            return model
        return head or model

    @staticmethod
    def _strip_provider_prefix(model: str) -> str:
        if "/" not in model:
            return model
        return model.split("/", 1)[1]

    def _resolve_api_base(self, model: str | None) -> str:
        raw_model = str(model or self.default_model or "").strip()
        candidates = [
            raw_model,
            self._strip_alias_suffix(raw_model),
            self._strip_provider_prefix(raw_model),
            self._strip_alias_suffix(self._strip_provider_prefix(raw_model)),
        ]
        for candidate in candidates:
            api_base = self._model_api_bases.get(candidate)
            if api_base:
                return api_base
        return self.api_base

    def _client_for_api_base(self, api_base: str) -> AsyncOpenAI:
        client = self._client_cache.get(api_base)
        if client is None:
            # 必须在 AsyncOpenAI 构造前清理，否则 httpx 会先抛 InvalidURL
            sanitize_no_proxy_env()
            client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=api_base,
                default_headers=self._default_headers,
            )
            self._client_cache[api_base] = client
        return client

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   model: str | None = None, max_tokens: int = 4096, temperature: float = 0.7,
                   reasoning_effort: str | None = None,
                   tool_choice: str | dict[str, Any] | None = None) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": self._sanitize_request_messages(
                self._sanitize_empty_content(messages),
                self._ALLOWED_MESSAGE_KEYS,
            ),
            "max_tokens": max(1, max_tokens),
            "temperature": temperature,
        }
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if tools:
            kwargs.update(tools=tools, tool_choice=tool_choice or "auto")
        try:
            client = self._client_for_api_base(self._resolve_api_base(model))
            return self._parse(await client.chat.completions.create(**kwargs))
        except Exception as e:
            return self._exception_error_response("OpenAI-compatible provider", e)

    def _parse(self, response: Any) -> LLMResponse:
        choice = response.choices[0]
        msg = choice.message
        tool_calls = [
            ToolCallRequest(
                id=tc.id,
                name=tc.function.name,
                arguments=json_repair.loads(tc.function.arguments)
                if isinstance(tc.function.arguments, str)
                else tc.function.arguments,
                provider_specific_fields=getattr(tc, "provider_specific_fields", None),
                function_provider_specific_fields=getattr(tc.function, "provider_specific_fields", None),
            )
            for tc in (msg.tool_calls or [])
        ]
        u = response.usage
        return LLMResponse(
            content=msg.content, tool_calls=tool_calls, finish_reason=choice.finish_reason or "stop",
            usage={"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens, "total_tokens": u.total_tokens} if u else {},
            reasoning_content=getattr(msg, "reasoning_content", None) or None,
        )

    def get_default_model(self) -> str:
        return self.default_model
