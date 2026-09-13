from __future__ import annotations

import json
from typing import Any

from ithqbot.agent.tools.base import Tool
from ithqbot.skills._test_platform_client import PlatformTestClient


class EvalAnalyzerTool(Tool):
    def __init__(self, workspace=None, config=None, provider_factory=None):  # noqa: ANN001
        self._workspace = workspace
        self._config = config
        self._provider_factory = provider_factory

    @property
    def name(self) -> str:
        return "eval_analyzer"

    @property
    def description(self) -> str:
        return "Analyze failed cases and categorize likely issues."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "failed_cases": {"type": "array", "items": {"type": "object"}},
                "criteria": {"type": "array", "items": {"type": "string"}},
                "gateway_url": {"type": "string"},
                "cookie": {"type": "string"},
                "csrf_token": {"type": "string"}
            },
            "required": ["failed_cases"]
        }

    async def execute(self, **kwargs: Any) -> str:
        failed_cases = kwargs.get("failed_cases") or []
        if not isinstance(failed_cases, list) or not failed_cases:
            return "错误：eval_analyzer 需要 failed_cases。"
        client = PlatformTestClient(
            gateway_url=kwargs.get("gateway_url"),
            cookie=kwargs.get("cookie"),
            csrf_token=kwargs.get("csrf_token"),
        )
        result = await client.request(
            "POST",
            "/api/ext/test-eval/analyze-failures",
            {
                "failed_cases": failed_cases,
                "criteria": kwargs.get("criteria") or [],
            },
        )
        return json.dumps(result, ensure_ascii=False)
