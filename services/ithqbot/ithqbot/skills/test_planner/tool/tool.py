from __future__ import annotations

import json
from typing import Any

from ithqbot.agent.tools.base import Tool
from ithqbot.skills._test_platform_client import PlatformTestClient


class CasePlannerTool(Tool):
    def __init__(self, workspace=None, config=None, provider_factory=None):  # noqa: ANN001
        self._workspace = workspace
        self._config = config
        self._provider_factory = provider_factory

    @property
    def name(self) -> str:
        return "test_planner"

    @property
    def description(self) -> str:
        return "Generate candidate test cases through the test-eval plugin."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "objective": {"type": "string"},
                "target": {"type": "string"},
                "case_count": {"type": "integer", "minimum": 1, "maximum": 20},
                "gateway_url": {"type": "string"},
                "cookie": {"type": "string"},
                "csrf_token": {"type": "string"}
            },
            "required": ["objective"]
        }

    async def execute(self, **kwargs: Any) -> str:
        objective = str(kwargs.get("objective") or "").strip()
        if not objective:
            return "错误：test_planner 需要 objective。"
        client = PlatformTestClient(
            gateway_url=kwargs.get("gateway_url"),
            cookie=kwargs.get("cookie"),
            csrf_token=kwargs.get("csrf_token"),
        )
        result = await client.request(
            "POST",
            "/api/ext/test-eval/plan-cases",
            {
                "objective": objective,
                "target": kwargs.get("target"),
                "case_count": kwargs.get("case_count") or 3,
            },
        )
        return json.dumps(result, ensure_ascii=False)
