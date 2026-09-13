from __future__ import annotations

import json
import os
from typing import Any

import httpx


class PlatformTestClient:
    def __init__(
        self,
        *,
        gateway_url: str | None = None,
        cookie: str | None = None,
        csrf_token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.gateway_url = (gateway_url or os.getenv("APP_HOST_GATEWAY_URL") or "http://localhost:8000").rstrip("/")
        self.cookie = cookie or os.getenv("PLATFORM_SESSION_COOKIE") or ""
        self.csrf_token = csrf_token or os.getenv("PLATFORM_CSRF_TOKEN") or ""
        self.timeout = timeout

    async def request(self, method: str, path: str, body: Any | None = None) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if self.cookie:
            headers["cookie"] = self.cookie
        if self.csrf_token:
            headers["x-csrf-token"] = self.csrf_token
        async with httpx.AsyncClient(base_url=self.gateway_url, follow_redirects=False, timeout=self.timeout) as client:
            response = await client.request(method, path, headers=headers, json=body)
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text}
        if response.status_code >= 400:
            raise RuntimeError(f"Platform request failed: {response.status_code} {json.dumps(payload, ensure_ascii=False)}")
        if not isinstance(payload, dict):
            raise RuntimeError("Platform response must be a JSON object")
        return payload

