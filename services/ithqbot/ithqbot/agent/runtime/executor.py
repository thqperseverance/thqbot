from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from ithqbot.agent.tools.base import ToolResult

from .models import ToolCallNode, ToolCallPlan

if TYPE_CHECKING:
    from ithqbot.agent.loop import AgentLoop


@dataclass
class ExecutorState:
    last_tool_batch_signature: tuple[str, ...] | None = None
    repeated_tool_batch_count: int = 0


@dataclass
class ExecutorResult:
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    node_results: dict[str, ToolResult] = field(default_factory=dict)
    stop_loop: bool = False
    final_content: str | None = None
    pending_interaction: dict[str, Any] | None = None
    pending_node_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": list(self.messages),
            "tools_used": list(self.tools_used),
            "node_results": {
                node_id: result.to_dict()
                for node_id, result in self.node_results.items()
            },
            "stop_loop": self.stop_loop,
            "final_content": self.final_content,
            "pending_interaction": dict(self.pending_interaction) if isinstance(self.pending_interaction, dict) else None,
            "pending_node_id": self.pending_node_id,
        }


@dataclass(frozen=True)
class ExecutorConfig:
    max_concurrency: int = 1
    per_node_timeout_s: int = 60
    max_retries: int = 1


class ToolCallExecutor:
    def __init__(self, loop: "AgentLoop", config: ExecutorConfig | None = None):
        self.loop = loop
        self.config = config or ExecutorConfig()

    @staticmethod
    def _plan_status_details(
        plan: ToolCallPlan,
        *,
        active_node_id: str | None = None,
        completed_node_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        completed = completed_node_ids or set()
        steps: list[dict[str, Any]] = []
        for index, node in enumerate(plan.nodes, start=1):
            state = "pending"
            if node.id in completed:
                state = "completed"
            elif node.id == active_node_id:
                state = "active"
            steps.append(
                {
                    "id": node.id,
                    "title": f"第 {index} 步：{node.tool_name}",
                    "state": state,
                    "expandable": state != "active",
                    "collapsed": state != "active",
                    "tool_name": node.tool_name,
                }
            )
        return {
            "presentation": "step_list",
            "show_progress_percent": False,
            "auto_collapse_on_complete": True,
            "collapsed_sections": {"completed": True, "pending": True},
            "current_step_id": active_node_id,
            "steps": steps,
        }

    @staticmethod
    def _is_fast_path_eligible(plan: ToolCallPlan) -> bool:
        if len(plan.nodes) != 1:
            return False
        node = plan.nodes[0]
        return not node.depends_on and not str(node.condition or "").strip()

    @staticmethod
    def _fast_plan_signature(plan: ToolCallPlan) -> tuple[str, ...]:
        if not plan.nodes:
            return ()
        node = plan.nodes[0]
        shallow_args = tuple(sorted((str(key), repr(value)) for key, value in dict(node.arguments).items()))
        return (f"{node.tool_name}:{shallow_args}",)

    async def execute_plan(
        self,
        *,
        plan: ToolCallPlan,
        response_content: str | None,
        response_reasoning_content: str | None,
        response_thinking_blocks: list[dict[str, Any]] | None,
        messages: list[dict[str, Any]],
        skill_context: Any,
        on_progress: Callable[..., Awaitable[None]] | None,
        route_metadata: dict[str, Any] | None,
        active_bot_id: str,
        guardrails_policy: Any | None,
        iteration: int,
        state: ExecutorState,
        record_assistant_tool_call: bool = True,
    ) -> ExecutorResult:
        batch_signature = self._fast_plan_signature(plan) if self._is_fast_path_eligible(plan) else plan.signature()
        repeated_tool_batch = bool(batch_signature) and batch_signature == state.last_tool_batch_signature
        if repeated_tool_batch:
            state.repeated_tool_batch_count += 1
            logger.warning(
                "Detected repeated tool batch on iteration {}: {}",
                iteration,
                batch_signature,
            )
        else:
            state.repeated_tool_batch_count = 0
        state.last_tool_batch_signature = batch_signature

        if on_progress and plan.nodes:
            thought = self.loop._normalize_progress_text(self.loop._strip_think(response_content))
            if thought:
                await self.loop._emit_progress(
                    on_progress,
                    thought,
                    progress_percent=min(20 + iteration * 10, 85),
                    progress_kind="reasoning",
                    progress_stage="extracting",
                )
            tool_hint = self.loop._strip_think(
                self.loop._tool_hint([self._node_to_hint_payload(node) for node in plan.nodes])
            )
            first_tool_name = plan.nodes[0].tool_name if plan.nodes else ""
            tool_hint_message = (
                f"正在执行第 1 步：{first_tool_name}"
                if len(plan.nodes) == 1
                else f"已规划 {len(plan.nodes)} 个步骤，正在执行第 1 步：{first_tool_name}"
            )
            await self.loop._emit_progress(
                on_progress,
                tool_hint_message,
                tool_hint=True,
                progress_percent=min(35 + iteration * 10, 90),
                progress_kind="tool_hint",
                progress_stage="tool_call",
                tool_name=first_tool_name or None,
                status_details={
                    **self._plan_status_details(
                        plan,
                        active_node_id=plan.nodes[0].id if plan.nodes else None,
                    ),
                    "raw_hint": tool_hint or "",
                },
            )

        if record_assistant_tool_call:
            tool_call_dicts = [self._node_to_tool_call_dict(node) for node in plan.nodes]
            messages = self.loop.context.add_assistant_message(
                messages,
                response_content,
                tool_call_dicts,
                reasoning_content=response_reasoning_content,
                thinking_blocks=response_thinking_blocks,
            )

        if self._is_fast_path_eligible(plan):
            return await self._execute_plan_fast_path(
                plan=plan,
                messages=messages,
                skill_context=skill_context,
                on_progress=on_progress,
                route_metadata=route_metadata,
                active_bot_id=active_bot_id,
                guardrails_policy=guardrails_policy,
                iteration=iteration,
                repeated_tool_batch=repeated_tool_batch,
                repeated_tool_batch_count=state.repeated_tool_batch_count,
            )

        executed: dict[str, ToolResult] = {}
        tools_used: list[str] = []
        pending = list(plan.nodes)
        while pending:
            ready = [
                node
                for node in pending
                if all(dep in executed for dep in node.depends_on)
                and self._condition_matches(node, executed)
            ]
            if not ready:
                break
            ready.sort(key=lambda item: item.sequence)
            concurrency = max(1, int(self.config.max_concurrency))
            for offset in range(0, len(ready), concurrency):
                batch = ready[offset : offset + concurrency]
                for node in batch:
                    pending.remove(node)
                results = await asyncio.gather(
                    *[
                        self._execute_node(
                            plan=plan,
                            node=node,
                            completed_node_ids=set(executed),
                            skill_context=skill_context,
                            on_progress=on_progress,
                            route_metadata=route_metadata,
                            active_bot_id=active_bot_id,
                            guardrails_policy=guardrails_policy,
                            iteration=iteration,
                            repeated_tool_batch=repeated_tool_batch,
                            repeated_tool_batch_count=state.repeated_tool_batch_count,
                        )
                        for node in batch
                    ]
                )
                for node, result in zip(batch, results):
                    if result.success:
                        tools_used.append(node.tool_name)
                    executed[node.id] = result
                    llm_result, tool_payload = self.loop._parse_tool_event_payload(result)
                    tool_payload = await self.loop._normalize_payload_files(
                        tool_payload,
                        skill_context=replace(skill_context, parent_run_id=node.id)
                        if repeated_tool_batch is False
                        else skill_context,
                    )
                    if on_progress and isinstance(tool_payload, dict):
                        interaction = tool_payload.get("interaction")
                        if isinstance(interaction, dict):
                            await self.loop._emit_progress(
                                on_progress,
                                str(tool_payload.get("interaction_message") or "请完成交互后继续。"),
                                status_event="interaction",
                                interaction=interaction,
                            )
                        files = tool_payload.get("files")
                        if isinstance(files, list) and files:
                            await self.loop._emit_progress(
                                on_progress,
                                str(tool_payload.get("file_message") or "结果文件已生成。"),
                                status_event="file",
                                files=files,
                                content_type="file",
                            )
                    messages = self.loop.context.add_tool_result(
                        messages,
                        node.id,
                        node.tool_name,
                        llm_result,
                    )
                    if isinstance(tool_payload, dict) and isinstance(tool_payload.get("interaction"), dict):
                        return ExecutorResult(
                            messages=messages,
                            tools_used=tools_used,
                            node_results=executed,
                            stop_loop=True,
                            final_content=str(tool_payload.get("interaction_message") or llm_result or "请完成交互后继续。"),
                            pending_interaction=dict(tool_payload["interaction"]),
                            pending_node_id=node.id,
                        )

        if repeated_tool_batch:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "你刚刚重复请求了完全相同的一组工具调用，说明已经陷入循环。"
                        "不要再次调用相同工具，请基于现有工具结果直接输出最终答复。"
                    ),
                }
            )
            if state.repeated_tool_batch_count >= 2:
                final_content = (
                    "我已完成必要的工具调用，但模型连续重复请求相同工具，"
                    "未能稳定生成最终答复。请稍后重试。"
                )
                messages = self.loop.context.add_assistant_message(messages, final_content)
                return ExecutorResult(
                    messages=messages,
                    tools_used=tools_used,
                    node_results=executed,
                    stop_loop=True,
                    final_content=final_content,
                )

        return ExecutorResult(messages=messages, tools_used=tools_used, node_results=executed)

    async def _execute_plan_fast_path(
        self,
        *,
        plan: ToolCallPlan,
        messages: list[dict[str, Any]],
        skill_context: Any,
        on_progress: Callable[..., Awaitable[None]] | None,
        route_metadata: dict[str, Any] | None,
        active_bot_id: str,
        guardrails_policy: Any | None,
        iteration: int,
        repeated_tool_batch: bool,
        repeated_tool_batch_count: int,
    ) -> ExecutorResult:
        node = plan.nodes[0]
        result = await self._execute_node(
            plan=plan,
            node=node,
            completed_node_ids=set(),
            skill_context=skill_context,
            on_progress=on_progress,
            route_metadata=route_metadata,
            active_bot_id=active_bot_id,
            guardrails_policy=guardrails_policy,
            iteration=iteration,
            repeated_tool_batch=repeated_tool_batch,
            repeated_tool_batch_count=repeated_tool_batch_count,
        )
        llm_result, tool_payload = self.loop._parse_tool_event_payload(result)
        tool_payload = await self.loop._normalize_payload_files(
            tool_payload,
            skill_context=replace(skill_context, parent_run_id=node.id),
        )
        if on_progress and isinstance(tool_payload, dict):
            interaction = tool_payload.get("interaction")
            if isinstance(interaction, dict):
                await self.loop._emit_progress(
                    on_progress,
                    str(tool_payload.get("interaction_message") or "请完成交互后继续。"),
                    status_event="interaction",
                    interaction=interaction,
                )
            files = tool_payload.get("files")
            if isinstance(files, list) and files:
                await self.loop._emit_progress(
                    on_progress,
                    str(tool_payload.get("file_message") or "结果文件已生成。"),
                    status_event="file",
                    files=files,
                    content_type="file",
                )
        messages = self.loop.context.add_tool_result(
            messages,
            node.id,
            node.tool_name,
            llm_result,
        )
        pending_interaction = (
            dict(tool_payload["interaction"])
            if isinstance(tool_payload, dict) and isinstance(tool_payload.get("interaction"), dict)
            else None
        )
        return ExecutorResult(
            messages=messages,
            tools_used=[node.tool_name] if result.success else [],
            node_results={node.id: result},
            stop_loop=bool(pending_interaction),
            final_content=(
                str((tool_payload or {}).get("interaction_message") or llm_result or "请完成交互后继续。")
                if pending_interaction
                else None
            ),
            pending_interaction=pending_interaction,
            pending_node_id=node.id if pending_interaction else None,
        )

    def _condition_matches(self, node: ToolCallNode, executed: dict[str, ToolResult]) -> bool:
        condition = str(node.condition or "").strip()
        if not condition:
            return True
        if condition.startswith("success:"):
            return condition.split(":", 1)[1] in executed
        if condition.startswith("error:"):
            return False
        return True

    async def _execute_node(
        self,
        *,
        plan: ToolCallPlan,
        node: ToolCallNode,
        completed_node_ids: set[str],
        skill_context: Any,
        on_progress: Callable[..., Awaitable[None]] | None,
        route_metadata: dict[str, Any] | None,
        active_bot_id: str,
        guardrails_policy: Any | None,
        iteration: int,
        repeated_tool_batch: bool,
        repeated_tool_batch_count: int,
    ) -> ToolResult:
        cancellation_token = getattr(skill_context, "cancellation_token", None)
        if cancellation_token is not None and hasattr(cancellation_token, "throw_if_cancelled"):
            cancellation_token.throw_if_cancelled()
        tool = self.loop.tools.get_tool(node.tool_name)
        node_timeout = self.config.per_node_timeout_s
        if tool and hasattr(tool, "timeout_s") and tool.timeout_s > 0:
            node_timeout = tool.timeout_s
        
        attempts = max(0, int(self.config.max_retries)) + 1
        last_result = ToolResult(content="工具执行未产生结果。", success=False, error="empty_result")
        for attempt in range(1, attempts + 1):
            try:
                last_result = await asyncio.wait_for(
                    self._invoke_node_once(
                        plan=plan,
                        node=node,
                        completed_node_ids=completed_node_ids,
                        skill_context=skill_context,
                        on_progress=on_progress,
                        route_metadata=route_metadata,
                        active_bot_id=active_bot_id,
                        guardrails_policy=guardrails_policy,
                        iteration=iteration,
                        repeated_tool_batch=repeated_tool_batch,
                        repeated_tool_batch_count=repeated_tool_batch_count,
                        attempt=attempt,
                        tool_instance=tool,
                    ),
                    timeout=max(1, int(node_timeout)),
                )
            except asyncio.TimeoutError:
                last_result = ToolResult(
                    content=(
                        f"错误：工具“{node.tool_name}”执行超时，已超过 {node_timeout} 秒。"
                    ),
                    success=False,
                    error="timeout",
                    metadata={"timeout_s": node_timeout, "attempt": attempt},
                )
            except Exception as exc:
                last_result = ToolResult(
                    content=f"错误：执行工具“{node.tool_name}”失败：{str(exc)}",
                    success=False,
                    error=str(exc),
                    metadata={"attempt": attempt},
                )
            if last_result.success or attempt >= attempts:
                return last_result
        return last_result

    async def _invoke_node_once(
        self,
        *,
        plan: ToolCallPlan,
        node: ToolCallNode,
        completed_node_ids: set[str],
        skill_context: Any,
        on_progress: Callable[..., Awaitable[None]] | None,
        route_metadata: dict[str, Any] | None,
        active_bot_id: str,
        guardrails_policy: Any | None,
        iteration: int,
        repeated_tool_batch: bool,
        repeated_tool_batch_count: int,
        attempt: int,
        tool_instance: Tool | None = None,
    ) -> ToolResult:
        # 注意：cancellation_token 原本只在 _execute_node 中取出，而 _invoke_node_once
        # 是独立方法，因此这里必须自己从 skill_context 再取一次；否则下面调用
        # tool.invoke(...) 时会抛 NameError，导致**所有**工具/技能执行失败。
        cancellation_token = getattr(skill_context, "cancellation_token", None)
        if on_progress:
            node_index = next((idx for idx, item in enumerate(plan.nodes, start=1) if item.id == node.id), 1)
            await self.loop._emit_progress(
                on_progress,
                f"正在执行第 {node_index} 步：{node.tool_name}",
                tool_hint=True,
                progress_kind="tool_hint",
                progress_stage="tool_call",
                tool_name=node.tool_name,
                status_details=self._plan_status_details(
                    plan,
                    active_node_id=node.id,
                    completed_node_ids=completed_node_ids,
                ),
            )
        args_str = self.loop._safe_json_dumps(node.arguments)
        logger.info("Tool call: {}({})", node.tool_name, args_str[:200])
        self.loop._trace_tool_lifecycle_start(
            tool_name=node.tool_name,
            tool_args=node.arguments,
            tool_call_id=node.id,
            route_metadata=route_metadata,
            active_bot_id=active_bot_id,
            content_preview=args_str,
            extra_details={"iteration": iteration, "attempt": attempt},
        )
        if repeated_tool_batch:
            result = ToolResult(
                content=self.loop._duplicate_tool_call_result(node.tool_name),
                success=False,
                error="duplicate_tool_batch",
                metadata={"attempt": attempt},
            )
            self.loop._trace_tool_blocked(
                tool_name=node.tool_name,
                tool_call_id=node.id,
                route_metadata=route_metadata,
                active_bot_id=active_bot_id,
                content_preview=args_str,
                extra_details={
                    "iteration": iteration,
                    "reason": "duplicate_tool_batch",
                    "repeat_count": repeated_tool_batch_count,
                        "attempt": attempt,
                },
            )
            return result

        # Security check: Validate tool arguments
        is_safe, error_msg = self.loop.policy.validate_tool_args(
            node.tool_name,
            node.arguments,
            guardrails_policy,
        )
        if not is_safe:
            result = ToolResult(
                content=error_msg or "安全校验失败",
                success=False,
                error="security_violation",
                metadata={"attempt": attempt},
            )
            self.loop._trace_tool_blocked(
                tool_name=node.tool_name,
                tool_call_id=node.id,
                route_metadata=route_metadata,
                active_bot_id=active_bot_id,
                content_preview=args_str,
                extra_details={
                    "iteration": iteration,
                    "reason": "security_violation",
                    "attempt": attempt,
                },
            )
            return result

        tool_skill_context = replace(skill_context, parent_run_id=node.id)
        tool = tool_instance or self.loop.tools.get_tool(node.tool_name)
        if tool is None:
            available_tools = ", ".join(item.name for item in self.loop.tools.list_tools())
            result = ToolResult(
                content=f"错误：未找到工具“{node.tool_name}”。可用工具：{available_tools}",
                success=False,
                error="tool_not_found",
                metadata={"attempt": attempt},
            )
        else:
            result = await tool.invoke(
                node.arguments,
                context_obj=tool_skill_context,
                cancellation_token=cancellation_token,
            )
        llm_result, tool_payload = self.loop._parse_tool_event_payload(result)
        tool_payload = await self.loop._normalize_payload_files(
            tool_payload,
            skill_context=tool_skill_context,
        )
        self.loop._trace_tool_lifecycle_end(
            tool_name=node.tool_name,
            tool_call_id=node.id,
            route_metadata=route_metadata,
            active_bot_id=active_bot_id,
            success=result.success,
            skill_content_preview=result.content,
            tool_content_preview=llm_result,
            result_payload=tool_payload,
            error_text=result.error,
            extra_details={"iteration": iteration, "attempt": attempt},
        )
        return result

    @staticmethod
    def _node_to_tool_call_dict(node: ToolCallNode) -> dict[str, Any]:
        return {
            "id": node.id,
            "type": "function",
            "function": {
                "name": node.tool_name,
                "arguments": __import__("json").dumps(node.arguments, ensure_ascii=False),
            },
        }

    @staticmethod
    def _node_to_hint_payload(node: ToolCallNode):
        return type(
            "HintPayload",
            (),
            {
                "name": node.tool_name,
                "arguments": node.arguments,
            },
        )()
