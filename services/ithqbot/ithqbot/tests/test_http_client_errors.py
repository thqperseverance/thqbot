import json

import httpx
import pytest

from ithqbot.agent.tools.web import WebFetchTool
from ithqbot.providers.transcription import GroqTranscriptionProvider
from ithqbot.utils.http_client import classify_http_client_error


def _status_error(status_code: int, message: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(message, request=request, response=response)


def test_classify_http_client_error_maps_timeout_to_retryable() -> None:
    error = classify_http_client_error("网页抓取", httpx.ReadTimeout("read timed out"))

    assert error.error_type == "timeout"
    assert error.retryable is True
    assert error.http_status is None
    assert "超时" in error.message


def test_classify_http_client_error_maps_connect_error_to_retryable() -> None:
    request = httpx.Request("GET", "https://example.com")
    error = classify_http_client_error("联网搜索", httpx.ConnectError("connection refused", request=request))

    assert error.error_type == "connection_error"
    assert error.retryable is True
    assert error.http_status is None
    assert "连接失败" in error.message


@pytest.mark.parametrize(
    ("status_code", "error_type", "retryable"),
    [
        (429, "rate_limit", True),
        (503, "server_error", True),
        (404, "http_error", False),
    ],
)
def test_classify_http_client_error_maps_http_status_codes(
    status_code: int,
    error_type: str,
    retryable: bool,
) -> None:
    error = classify_http_client_error("Brave 搜索", _status_error(status_code, f"HTTP {status_code}"))

    assert error.http_status == status_code
    assert error.error_type == error_type
    assert error.retryable is retryable


@pytest.mark.asyncio
async def test_web_fetch_returns_structured_error_payload_on_final_failure(monkeypatch) -> None:
    tool = WebFetchTool()

    async def _always_valid(_url: str) -> tuple[bool, str]:
        return True, ""

    class _FailingClient:
        async def get(self, *_args, **_kwargs):
            raise _status_error(503, "HTTP 503")

    async def _jina_none(_url: str, _max_chars: int) -> None:
        return None

    monkeypatch.setattr("ithqbot.agent.tools.web._validate_url_safe", _always_valid)
    monkeypatch.setattr(tool, "_fetch_jina", _jina_none)
    monkeypatch.setattr(tool, "_get_client", lambda *args, **kwargs: _FailingClient())

    result = await tool.execute("https://example.com")
    payload = json.loads(result)

    assert payload["errorType"] == "server_error"
    assert payload["httpStatus"] == 503
    assert payload["retryable"] is True
    assert payload["url"] == "https://example.com"


def test_transcription_provider_classifies_rate_limit_as_retryable() -> None:
    error = GroqTranscriptionProvider.classify_error(_status_error(429, "HTTP 429"))

    assert error.error_type == "rate_limit"
    assert error.http_status == 429
    assert error.retryable is True


def test_transcription_provider_classifies_timeout_as_retryable() -> None:
    error = GroqTranscriptionProvider.classify_error(httpx.ReadTimeout("read timed out"))

    assert error.error_type == "timeout"
    assert error.retryable is True
