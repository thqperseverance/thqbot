from __future__ import annotations

import asyncio
import json

from ithqbot.skills.model_compare.tool.tool import ModelCompareTool
from ithqbot.skills.test_runner.tool.tool import DatasetRunnerTool


def test_test_runner_tool_runs_dataset(monkeypatch) -> None:  # noqa: ANN001
    captured: list[tuple[str, str, object]] = []

    async def fake_request(self, method: str, path: str, body=None):  # noqa: ANN001, ANN202
        captured.append((method, path, body))
        return {"status": "success", "run": {"id": "run-1", "pass_rate": 100.0}}

    monkeypatch.setattr("ithqbot.skills._test_platform_client.PlatformTestClient.request", fake_request)

    async def scenario() -> None:
        tool = DatasetRunnerTool()
        result = await tool.execute(action="run_dataset", dataset_id="qa_basic")
        payload = json.loads(result)
        assert payload["run"]["pass_rate"] == 100.0

    asyncio.run(scenario())
    assert captured[0][0] == "POST"
    assert captured[0][1] == "/api/ext/test-orchestrator/run"


def test_model_compare_tool_returns_winner(monkeypatch) -> None:  # noqa: ANN001
    async def fake_request(self, method: str, path: str, body=None):  # noqa: ANN001, ANN202
        return {"status": "success", "winner": "gpt-4o", "outputs": {"gpt-4o": "best", "qwen": "alt"}}

    monkeypatch.setattr("ithqbot.skills._test_platform_client.PlatformTestClient.request", fake_request)

    async def scenario() -> None:
        tool = ModelCompareTool()
        result = await tool.execute(prompt="compare", providers=["openai:gpt-4o", "qwen:qwen-max"])
        payload = json.loads(result)
        assert payload["winner"] == "gpt-4o"

    asyncio.run(scenario())
