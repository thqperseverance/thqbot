from __future__ import annotations

import json
from typing import Any

from ithqbot.agent.tools.base import Tool
from ithqbot.skills._test_platform_client import PlatformTestClient


class ModelCompareTool(Tool):
    def __init__(self, workspace=None, config=None, provider_factory=None):  # noqa: ANN001
        self._workspace = workspace
        self._config = config
        self._provider_factory = provider_factory

    @property
    def name(self) -> str:
        return "model_compare"

    @property
    def description(self) -> str:
        return "Compare multiple model outputs with the platform test-eval plugin."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "providers": {"type": "array", "items": {"type": "string"}},
                "criteria": {"type": "array", "items": {"type": "string"}},
                "gateway_url": {"type": "string"},
                "cookie": {"type": "string"},
                "csrf_token": {"type": "string"}
            },
            "required": ["prompt", "providers"]
        }

    async def execute(self, **kwargs: Any) -> str:
        prompt = str(kwargs.get("prompt") or "").strip()
        providers = [str(item).strip() for item in kwargs.get("providers") or [] if str(item).strip()]
        if not prompt or not providers:
            return "错误：model_compare 需要 prompt 和 providers。"
        client = PlatformTestClient(
            gateway_url=kwargs.get("gateway_url"),
            cookie=kwargs.get("cookie"),
            csrf_token=kwargs.get("csrf_token"),
        )
        result = await client.request(
            "POST",
            "/api/ext/test-eval/multi-model-compare",
            {
                "prompt": prompt,
                "providers": providers,
                "criteria": kwargs.get("criteria") or [],
            },
        )
        return json.dumps(result, ensure_ascii=False)
