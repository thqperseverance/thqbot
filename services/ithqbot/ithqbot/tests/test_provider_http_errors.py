from types import SimpleNamespace

import pytest

from ithqbot.providers.custom_provider import CustomProvider
from ithqbot.providers.openai_codex_provider import OpenAICodexProvider, _CodexHTTPStatusError


class _FakeCompletions:
    def __init__(self, side_effect: Exception):
        self._side_effect = side_effect

    async def create(self, **_kwargs):
        raise self._side_effect


@pytest.mark.asyncio
async def test_custom_provider_classifies_timeout_as_retryable() -> None:
    class APITimeoutError(Exception):
        pass

    provider = object.__new__(CustomProvider)
    provider.default_model = "test-model"
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=_FakeCompletions(APITimeoutError("request exceeded deadline"))
        )
    )

    response = await provider.chat(messages=[{"role": "user", "content": "hello"}])

    assert response.finish_reason == "error"
    assert response.error_type == "timeout"
    assert response.retryable is True
    assert "OpenAI-compatible provider request timed out." in (response.content or "")


@pytest.mark.asyncio
async def test_custom_provider_classifies_rate_limit_status_as_retryable() -> None:
    class RateLimitError(Exception):
        def __init__(self, message: str):
            super().__init__(message)
            self.status_code = 429

    provider = object.__new__(CustomProvider)
    provider.default_model = "test-model"
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=_FakeCompletions(RateLimitError("too many requests"))
        )
    )

    response = await provider.chat(messages=[{"role": "user", "content": "hello"}])

    assert response.finish_reason == "error"
    assert response.http_status == 429
    assert response.error_type == "rate_limit"
    assert response.retryable is True
    assert "OpenAI-compatible provider rate limited the request (HTTP 429)." in (response.content or "")


@pytest.mark.asyncio
async def test_codex_provider_classifies_5xx_as_retryable(monkeypatch) -> None:
    from ithqbot.providers import openai_codex_provider as codex_module

    provider = OpenAICodexProvider()

    monkeypatch.setattr(
        codex_module,
        "get_codex_token",
        lambda: SimpleNamespace(account_id="acct_123", access="token_123"),
    )

    async def _fake_request(*_args, **_kwargs):
        raise _CodexHTTPStatusError(503, "HTTP 503: upstream unavailable")

    monkeypatch.setattr(codex_module, "_request_codex", _fake_request)

    response = await provider.chat(messages=[{"role": "user", "content": "hello"}])

    assert response.finish_reason == "error"
    assert response.http_status == 503
    assert response.error_type == "server_error"
    assert response.retryable is True
    assert "OpenAI Codex returned a server error (HTTP 503)." in (response.content or "")
