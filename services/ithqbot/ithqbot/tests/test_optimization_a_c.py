from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch, AsyncMock
from pathlib import Path

import pytest

from ithqbot.agent.runtime.models import ToolCallPlan, ToolCallNode
from ithqbot.agent.runtime.executor import ToolCallExecutor, ExecutorState, ExecutorConfig
from ithqbot.agent.runtime.policy import AgentPolicy
from ithqbot.providers.base import ToolCallRequest
from ithqbot.agent.skills.base import SkillContext
from ithqbot.agent.tools.base import ToolResult


def _make_mock_loop():
    loop = MagicMock()
    loop.restrict_to_workspace = True
    
    # Configure policy mock explicitly
    loop.policy.validate_tool_args.side_effect = lambda name, args, policy=None: (True, "safe")
    
    loop._normalize_progress_text.side_effect = lambda x: x
    loop._strip_think.side_effect = lambda x: x
    loop._tool_hint.return_value = ""
    loop._parse_tool_event_payload.return_value = (None, {})
    loop._emit_progress = AsyncMock()
    loop.context = MagicMock()
    loop.tools = MagicMock()
    
    # Sync methods in reality
    loop._trace_tool_blocked = MagicMock()
    loop._trace_tool_lifecycle_start = MagicMock()
    loop._trace_tool_lifecycle_end = MagicMock()
    loop._record_bot_guardrails_hit = MagicMock()
    
    # Async methods that are actually async
    loop._normalize_payload_files = AsyncMock(side_effect=lambda p, **kw: p)
    
    return loop


@pytest.mark.asyncio
async def test_parallel_plan_generation() -> None:
    tool_calls = [
        ToolCallRequest(id="c1", name="t1", arguments={}),
        ToolCallRequest(id="c2", name="t2", arguments={}),
    ]
    
    plan_linear = ToolCallPlan.from_tool_calls(tool_calls, parallel=False)
    assert plan_linear.nodes[1].depends_on == ("c1",)
    
    plan_parallel = ToolCallPlan.from_tool_calls(tool_calls, parallel=True)
    assert plan_parallel.nodes[1].depends_on == ()


@pytest.mark.asyncio
async def test_parallel_execution_speed() -> None:
    loop = _make_mock_loop()
    # MUST set max_concurrency > 1 for parallel execution
    # Set max_retries to 0 to avoid any retry interference
    executor = ToolCallExecutor(loop, config=ExecutorConfig(max_concurrency=5, max_retries=0))
    
    class SlowTool:
        timeout_s = 10
        async def invoke(self, arguments, context_obj=None):
            await asyncio.sleep(0.5)
            return ToolResult(content="ok", success=True)
            
    loop.tools.get_tool.return_value = SlowTool()
    
    tool_calls = [
        ToolCallRequest(id="c1", name="t1", arguments={}),
        ToolCallRequest(id="c2", name="t2", arguments={}),
    ]
    
    # Linear execution should take ~1.0s
    plan_linear = ToolCallPlan.from_tool_calls(tool_calls, parallel=False)
    start = time.time()
    await executor.execute_plan(
        plan=plan_linear,
        response_content="msg",
        response_reasoning_content="reasoning",
        response_thinking_blocks=[],
        messages=[],
        skill_context=SkillContext(
            tenant_id="t", account_id="a", chat_id="c", bot_id="b"
        ),
        on_progress=AsyncMock(),
        route_metadata={},
        active_bot_id="b",
        guardrails_policy=None,
        iteration=1,
        state=ExecutorState(),
    )
    duration_linear = time.time() - start
    assert duration_linear >= 1.0
    
    # Parallel execution should take ~0.5s
    plan_parallel = ToolCallPlan.from_tool_calls(tool_calls, parallel=True)
    start = time.time()
    await executor.execute_plan(
        plan=plan_parallel,
        response_content="msg",
        response_reasoning_content="reasoning",
        response_thinking_blocks=[],
        messages=[],
        skill_context=SkillContext(
            tenant_id="t", account_id="a", chat_id="c", bot_id="b"
        ),
        on_progress=AsyncMock(),
        route_metadata={},
        active_bot_id="b",
        guardrails_policy=None,
        iteration=1,
        state=ExecutorState(),
    )
    duration_parallel = time.time() - start
    assert duration_parallel < 0.8


@pytest.mark.asyncio
async def test_security_argument_validation() -> None:
    loop = MagicMock()
    loop.restrict_to_workspace = True
    policy = AgentPolicy(loop)
    
    mock_guardrails = MagicMock()
    mock_guardrails.enabled = True
    policy.resolve_bot_guardrails_policy = MagicMock(return_value=(None, mock_guardrails))
    
    is_safe, msg = policy.validate_tool_args("write_file", {"path": "../../etc/passwd"})
    assert is_safe is False
    assert "非法路径回溯" in msg
    
    is_safe, msg = policy.validate_tool_args("read_file", {"path": "/etc/shadow"})
    assert is_safe is False
    assert "禁止访问工作区外的绝对路径" in msg
    
    is_safe, msg = policy.validate_tool_args("shell", {"command": "ls; rm -rf /"})
    assert is_safe is False
    assert "潜在的命令注入" in msg
    
    is_safe, msg = policy.validate_tool_args("write_file", {"path": "logs/test.log"})
    assert is_safe is True


@pytest.mark.asyncio
async def test_executor_enforces_security() -> None:
    loop = _make_mock_loop()
    executor = ToolCallExecutor(loop)
    
    loop.policy.validate_tool_args.side_effect = lambda name, args, policy=None: (False, "Blocked by policy")
    
    plan = ToolCallPlan.single(tool_name="shell", arguments={"command": "rm -rf /;"}, tool_call_id="c1")
    
    mock_tool = MagicMock()
    mock_tool.timeout_s = 60
    loop.tools.get_tool.return_value = mock_tool
    
    result = await executor.execute_plan(
        plan=plan,
        response_content="msg",
        response_reasoning_content="reasoning",
        response_thinking_blocks=[],
        messages=[],
        skill_context=SkillContext(
            tenant_id="t", account_id="a", chat_id="c", bot_id="b"
        ),
        on_progress=AsyncMock(),
        route_metadata={},
        active_bot_id="b",
        guardrails_policy=None,
        iteration=1,
        state=ExecutorState(),
    )
    
    assert result.tools_used == []
    assert "c1" in result.node_results
    assert result.node_results["c1"].success is False
    loop._trace_tool_blocked.assert_called()
