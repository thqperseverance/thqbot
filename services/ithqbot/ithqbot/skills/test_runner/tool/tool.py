from __future__ import annotations

import json
from typing import Any

from ithqbot.agent.tools.base import Tool
from ithqbot.skills._test_platform_client import PlatformTestClient


class DatasetRunnerTool(Tool):
    def __init__(self, workspace=None, config=None, provider_factory=None):  # noqa: ANN001, D401
        self._workspace = workspace
        self._config = config
        self._provider_factory = provider_factory

    @property
    def name(self) -> str:
        return "test_runner"

    @property
    def description(self) -> str:
        return "Run one dataset or a suite of datasets through the test orchestrator plugin."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["run_dataset", "run_suite"]},
                "dataset_id": {"type": "string"},
                "dataset_ids": {"type": "array", "items": {"type": "string"}},
                "steps": {"type": "array", "items": {"type": "object"}},
                "gateway_url": {"type": "string"},
                "cookie": {"type": "string"},
                "csrf_token": {"type": "string"},
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> str:
        action = str(kwargs.get("action") or "").strip()
        client = PlatformTestClient(
            gateway_url=kwargs.get("gateway_url"),
            cookie=kwargs.get("cookie"),
            csrf_token=kwargs.get("csrf_token"),
        )
        steps = kwargs.get("steps")

        if action == "run_dataset":
            dataset_id = str(kwargs.get("dataset_id") or "").strip()
            if not dataset_id:
                return "错误：run_dataset 需要 dataset_id。"
            result = await client.request(
                "POST",
                "/api/ext/test-orchestrator/run",
                {"dataset_id": dataset_id, "steps": steps},
            )
            return json.dumps(result, ensure_ascii=False)

        if action == "run_suite":
            dataset_ids = [str(item).strip() for item in kwargs.get("dataset_ids") or [] if str(item).strip()]
            if not dataset_ids:
                return "错误：run_suite 需要 dataset_ids。"
            outputs = []
            for dataset_id in dataset_ids:
                outputs.append(
                    {
                        "dataset_id": dataset_id,
                        "result": await client.request(
                            "POST",
                            "/api/ext/test-orchestrator/run",
                            {"dataset_id": dataset_id, "steps": steps},
                        ),
                    }
                )
            return json.dumps({"status": "success", "items": outputs}, ensure_ascii=False)

        return "错误：未知 action，只支持 run_dataset 或 run_suite。"
