from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class HTTPClientError:
    service: str
    message: str
    error_type: str
    http_status: int | None = None
    retryable: bool = False
    detail: str | None = None

    def to_text(self, prefix: str = "错误") -> str:
        return f"{prefix}：{self.message}"

    def to_payload(self, **extra: Any) -> dict[str, Any]:
        payload = {
            "error": self.message,
            "errorType": self.error_type,
            "httpStatus": self.http_status,
            "retryable": self.retryable,
        }
        if self.detail:
            payload["detail"] = self.detail
        payload.update(extra)
        return payload

    def to_log_context(self) -> dict[str, Any]:
        return asdict(self)


_RETRYABLE_HTTP_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def classify_http_client_error(service: str, exc: Exception, action: str = "请求") -> HTTPClientError:
    status_code = _extract_status_code(exc)
    detail_text = _normalize_detail(exc)

    if status_code is not None:
        return _http_status_error(
            service=service,
            status_code=status_code,
            action=action,
            detail=detail_text,
        )

    error_name = exc.__class__.__name__.lower()
    if isinstance(exc, TimeoutError) or "timeout" in error_name or "timedout" in error_name:
        return HTTPClientError(
            service=service,
            message=f"{service}{action}超时，可稍后重试。",
            error_type="timeout",
            retryable=True,
            detail=detail_text,
        )
    if "connect" in error_name or "connection" in error_name or "proxy" in error_name:
        return HTTPClientError(
            service=service,
            message=f"{service}{action}连接失败，可稍后重试。",
            error_type="connection_error",
            retryable=True,
            detail=detail_text,
        )

    return HTTPClientError(
        service=service,
        message=f"{service}{action}失败。",
        error_type="client_error",
        retryable=False,
        detail=detail_text,
    )


def _http_status_error(
    service: str,
    status_code: int,
    action: str,
    detail: str | None = None,
) -> HTTPClientError:
    retryable = status_code in _RETRYABLE_HTTP_STATUSES
    if status_code == 429:
        message = f"{service}{action}触发限流（HTTP 429），可稍后重试。"
        error_type = "rate_limit"
    elif 500 <= status_code < 600:
        message = f"{service}{action}遇到服务端异常（HTTP {status_code}），可稍后重试。"
        error_type = "server_error"
    else:
        message = f"{service}{action}被拒绝（HTTP {status_code}）。"
        error_type = "http_error"
    return HTTPClientError(
        service=service,
        message=message,
        error_type=error_type,
        http_status=status_code,
        retryable=retryable,
        detail=detail,
    )


def _extract_status_code(exc: Exception) -> int | None:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code

    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    if isinstance(response_status, int):
        return response_status
    return None


def _normalize_detail(exc: Exception) -> str | None:
    text = str(exc).strip()
    if text:
        return text
    return repr(exc)
