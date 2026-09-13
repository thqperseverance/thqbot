"""Base LLM provider interface."""

import asyncio
import json
from abc import ABC, abstractmethod
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from ithqbot import context
from ithqbot.observability import record_llm_usage

# 单回合 token 用量累计器。上层（AgentLoop）在回合开始重置、回合结束取出，
# 于是消息元信息里能带上**真实**的 token 用量，而不是估算值。
_turn_usage: ContextVar[dict[str, int] | None] = ContextVar("ithqbot_turn_usage", default=None)

_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def reset_turn_usage() -> None:
    """开始新回合时清零。"""
    _turn_usage.set({key: 0 for key in _USAGE_KEYS} | {"calls": 0})


def add_turn_usage(usage: dict[str, Any] | None) -> None:
    """累计一次 LLM 调用的用量（非整数/缺失字段按 0 计）。"""
    if not isinstance(usage, dict):
        return
    bucket = _turn_usage.get()
    if bucket is None:
        bucket = {key: 0 for key in _USAGE_KEYS} | {"calls": 0}
        _turn_usage.set(bucket)
    bucket["calls"] = bucket.get("calls", 0) + 1
    for key in _USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            bucket[key] = bucket.get(key, 0) + int(value)


def take_turn_usage() -> dict[str, int] | None:
    """取出并清空本回合用量；没有发生调用时返回 None。"""
    bucket = _turn_usage.get()
    _turn_usage.set(None)
    if not bucket or int(bucket.get("calls", 0)) <= 0:
        return None
    return dict(bucket)


@dataclass
class ToolCallRequest:
    """A tool call request from the LLM."""
    id: str
    name: str
    arguments: dict[str, Any]
    provider_specific_fields: dict[str, Any] | None = None
    function_provider_specific_fields: dict[str, Any] | None = None

    def to_openai_tool_call(self) -> dict[str, Any]:
        """Serialize to an OpenAI-style tool_call payload."""
        tool_call = {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }
        if self.provider_specific_fields:
            tool_call["provider_specific_fields"] = self.provider_specific_fields
        if self.function_provider_specific_fields:
            tool_call["function"]["provider_specific_fields"] = self.function_provider_specific_fields
        return tool_call


@dataclass
class LLMResponse:
    """Response from an LLM provider."""
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str | None = None  # Kimi, DeepSeek-R1 etc.
    thinking_blocks: list[dict] | None = None  # Anthropic extended thinking
    error_type: str | None = None
    http_status: int | None = None
    retryable: bool | None = None
    
    @property
    def has_tool_calls(self) -> bool:
        """Check if response contains tool calls."""
        return len(self.tool_calls) > 0


@dataclass(frozen=True)
class GenerationSettings:
    """Default generation parameters for LLM calls.

    Stored on the provider so every call site inherits the same defaults
    without having to pass temperature / max_tokens / reasoning_effort
    through every layer.  Individual call sites can still override by
    passing explicit keyword arguments to chat() / chat_with_retry().
    """

    temperature: float = 0.7
    max_tokens: int = 4096
    reasoning_effort: str | None = None


class LLMProvider(ABC):
    """
    Abstract base class for LLM providers.
    
    Implementations should handle the specifics of each provider's API
    while maintaining a consistent interface.
    """

    _CHAT_RETRY_DELAYS = (1, 2, 4)
    _TRANSIENT_ERROR_MARKERS = (
        "429",
        "rate limit",
        "500",
        "502",
        "503",
        "504",
        "overloaded",
        "timeout",
        "timed out",
        "connection",
        "server error",
        "temporarily unavailable",
    )
    _IMAGE_UNSUPPORTED_MARKERS = (
        "image_url is only supported",
        "does not support image",
        "images are not supported",
        "image input is not supported",
        "image_url is not supported",
        "unsupported image input",
    )

    _SENTINEL = object()
    _RETRYABLE_HTTP_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key
        self.api_base = api_base
        self.generation: GenerationSettings = GenerationSettings()

    @staticmethod
    def _sanitize_empty_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace empty text content that causes provider 400 errors.

        Empty content can appear when MCP tools return nothing. Most providers
        reject empty-string content or empty text blocks in list content.
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            if content is None or (isinstance(content, str) and not content):
                clean = dict(msg)
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    clean.pop("content", None)
                else:
                    clean["content"] = "(empty)"
                result.append(clean)
                continue

            if isinstance(content, list):
                if role == "tool":
                    clean = dict(msg)
                    clean["content"] = json.dumps(content, ensure_ascii=False)
                    result.append(clean)
                    continue
                filtered = [
                    item for item in content
                    if not (
                        isinstance(item, dict)
                        and item.get("type") in ("text", "input_text", "output_text")
                        and not item.get("text")
                    )
                ]
                if len(filtered) != len(content):
                    clean = dict(msg)
                    if filtered:
                        clean["content"] = filtered
                    elif msg.get("role") == "assistant" and msg.get("tool_calls"):
                        clean.pop("content", None)
                    else:
                        clean["content"] = "(empty)"
                    result.append(clean)
                    continue

            if isinstance(content, dict):
                clean = dict(msg)
                clean["content"] = json.dumps(content, ensure_ascii=False)
                result.append(clean)
                continue

            result.append(msg)
        return result

    @staticmethod
    def _sanitize_request_messages(
        messages: list[dict[str, Any]],
        allowed_keys: frozenset[str],
    ) -> list[dict[str, Any]]:
        """Keep only provider-safe message keys and normalize assistant content."""
        sanitized = []
        for msg in messages:
            clean = {k: v for k, v in msg.items() if k in allowed_keys}
            if clean.get("role") == "assistant" and clean.get("tool_calls"):
                if clean.get("content") is not None:
                    clean["content"] = str(clean.get("content"))
                elif "content" in clean:
                    clean.pop("content", None)
            elif clean.get("role") == "assistant" and clean.get("content") is None:
                clean["content"] = ""
            sanitized.append(clean)
        return sanitized

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        """
        Send a chat completion request.
        
        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions.
            model: Model identifier (provider-specific).
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
            tool_choice: Tool selection strategy ("auto", "required", or specific tool dict).
        
        Returns:
            LLMResponse with content and/or tool calls.
        """
        pass

    @classmethod
    def _is_transient_error(cls, content: str | None) -> bool:
        err = (content or "").lower()
        return any(marker in err for marker in cls._TRANSIENT_ERROR_MARKERS)

    @classmethod
    def _is_retryable_response(cls, response: LLMResponse) -> bool:
        if response.retryable is not None:
            return response.retryable
        if response.http_status is not None:
            return response.http_status in cls._RETRYABLE_HTTP_STATUSES
        return cls._is_transient_error(response.content)

    @classmethod
    def _is_image_unsupported_error(cls, content: str | None) -> bool:
        err = (content or "").lower()
        return any(marker in err for marker in cls._IMAGE_UNSUPPORTED_MARKERS)

    @staticmethod
    def _strip_image_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """Replace image_url blocks with text placeholder. Returns None if no images found."""
        found = False
        result = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                new_content = []
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "image_url":
                        new_content.append({"type": "text", "text": "[image omitted]"})
                        found = True
                    else:
                        new_content.append(b)
                result.append({**msg, "content": new_content})
            else:
                result.append(msg)
        return result if found else None

    async def _safe_chat(self, **kwargs: Any) -> LLMResponse:
        """Call chat() and convert unexpected exceptions to error responses."""
        try:
            return await self.chat(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._exception_error_response("LLM", exc)

    @classmethod
    def _http_error_response(
        cls,
        provider_name: str,
        status_code: int,
        detail: str | None = None,
    ) -> LLMResponse:
        detail_text = (detail or "").strip()
        retryable = status_code in cls._RETRYABLE_HTTP_STATUSES
        if status_code == 429:
            error_type = "rate_limit"
            message = f"{provider_name} rate limited the request (HTTP 429)."
        elif 500 <= status_code < 600:
            error_type = "server_error"
            message = f"{provider_name} returned a server error (HTTP {status_code})."
        else:
            error_type = "http_error"
            message = f"{provider_name} rejected the request (HTTP {status_code})."
        if detail_text:
            message = f"{message} {detail_text}"
        return LLMResponse(
            content=message,
            finish_reason="error",
            error_type=error_type,
            http_status=status_code,
            retryable=retryable,
        )

    @classmethod
    def _exception_error_response(
        cls,
        provider_name: str,
        exc: Exception,
    ) -> LLMResponse:
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int):
            return cls._http_error_response(provider_name, status_code, str(exc))

        error_name = exc.__class__.__name__.lower()
        error_text = str(exc).strip() or repr(exc)

        if isinstance(exc, TimeoutError) or "timeout" in error_name or "timedout" in error_name:
            return LLMResponse(
                content=f"{provider_name} request timed out. {error_text}",
                finish_reason="error",
                error_type="timeout",
                retryable=True,
            )
        if "connect" in error_name or "connection" in error_name:
            return LLMResponse(
                content=f"{provider_name} connection failed. {error_text}",
                finish_reason="error",
                error_type="connection_error",
                retryable=True,
            )

        return LLMResponse(
            content=f"Error calling {provider_name}: {error_text}",
            finish_reason="error",
            error_type="provider_error",
            retryable=False,
        )

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = _SENTINEL,
        temperature: object = _SENTINEL,
        reasoning_effort: object = _SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Call chat() with retry on transient provider failures.

        Parameters default to ``self.generation`` when not explicitly passed,
        so callers no longer need to thread temperature / max_tokens /
        reasoning_effort through every layer.
        """
        if max_tokens is self._SENTINEL:
            max_tokens = self.generation.max_tokens
        if temperature is self._SENTINEL:
            temperature = self.generation.temperature
        if reasoning_effort is self._SENTINEL:
            reasoning_effort = self.generation.reasoning_effort

        kw: dict[str, Any] = dict(
            messages=messages, tools=tools, model=model,
            max_tokens=max_tokens, temperature=temperature,
            reasoning_effort=reasoning_effort, tool_choice=tool_choice,
        )
        target_model = model or self.get_default_model()

        def _record(response: LLMResponse, duration_ms: int | None = None) -> None:
            add_turn_usage(response.usage)
            llm_ctx = context.get_llm_call_context()
            record_llm_usage(
                source=llm_ctx.get("source") or "bot",
                component=llm_ctx.get("component"),
                model=target_model,
                usage=response.usage,
                finish_reason=response.finish_reason,
                duration_ms=duration_ms,
            )
            
            # Detailed debug logging for each request
            usage = response.usage or {}
            request_preview = json.dumps(messages, ensure_ascii=False)
            if len(request_preview) > 3000:
                request_preview = request_preview[:3000] + "...(truncated)"
            
            logger.debug(
                "[LLM] Request: model={}, duration={}ms, tokens={}/{}, content={}",
                target_model,
                duration_ms,
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                request_preview
            )

        import time
        for attempt, delay in enumerate(self._CHAT_RETRY_DELAYS, start=1):
            start_ms = int(time.time() * 1000)
            response = await self._safe_chat(**kw)
            call_duration_ms = int(time.time() * 1000) - start_ms
            _record(response, duration_ms=call_duration_ms)

            if response.finish_reason != "error":
                return response

            if not self._is_retryable_response(response):
                if self._is_image_unsupported_error(response.content):
                    stripped = self._strip_image_content(messages)
                    if stripped is not None:
                        logger.warning("Model does not support image input, retrying without images")
                        stripped_response = await self._safe_chat(**{**kw, "messages": stripped})
                        _record(stripped_response)
                        return stripped_response
                return response

            logger.warning(
                "LLM transient error (attempt {}/{}), retrying in {}s: {}",
                attempt, len(self._CHAT_RETRY_DELAYS), delay,
                (response.content or "")[:120].lower(),
            )
            await asyncio.sleep(delay)

        start_ms = int(time.time() * 1000)
        response = await self._safe_chat(**kw)
        call_duration_ms = int(time.time() * 1000) - start_ms
        _record(response, duration_ms=call_duration_ms)
        return response

    @abstractmethod
    def get_default_model(self) -> str:
        """Get the default model for this provider."""
        pass
