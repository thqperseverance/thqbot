from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger

from ithqbot import context as runtime_context
from ithqbot.config.schema import RoutePurpose
from ithqbot.observability import build_routing_observability_payload, record_trace_event
from ithqbot.providers.base import LLMResponse, ToolCallRequest

from .models import ToolCallPlan

if TYPE_CHECKING:
    from ithqbot.agent.loop import AgentLoop


@dataclass
class PlannerIterationState:
    illegal_repair_attempts: int = 0
    illegal_repair_signatures: set[str] = field(default_factory=set)


@dataclass
class PlannerIterationResult:
    messages: list[dict[str, Any]]
    response: LLMResponse | None = None
    recovered_text_tool_calls: list[ToolCallRequest] = field(default_factory=list)
    plan: ToolCallPlan = field(default_factory=ToolCallPlan)
    active_model: str = ""
    should_continue: bool = False
    final_content: str | None = None


class AgentPlanner:
    def __init__(self, loop: "AgentLoop"):
        self.loop = loop

    @staticmethod
    def _should_use_final_answer_route(messages: list[dict[str, Any]]) -> bool:
        for message in reversed(messages):
            role = message.get("role")
            if role == "tool":
                return True
            if role == "assistant" and message.get("tool_calls"):
                return False
        return False

    async def plan_iteration(
        self,
        *,
        messages: list[dict[str, Any]],
        guarded_tool_defs: list[dict[str, Any]],
        active_model: str,
        route_text: str,
        route_metadata: dict[str, Any] | None,
        iteration: int,
        decision: Any,
        fallback_models: list[str],
        state: PlannerIterationState,
    ) -> PlannerIterationResult:
        effective_decision = decision
        if self._should_use_final_answer_route(messages):
            effective_decision = await self.loop.model_router.decide(
                route_text,
                route_metadata,
                purpose=RoutePurpose.FINAL_ANSWER,
            )
            active_model = effective_decision.active_model or active_model
            fallback_models[:] = list(effective_decision.fallback_models)
            self.loop._update_routing_state(
                route_metadata,
                current_purpose=effective_decision.purpose.value,
                current_model=active_model,
                final_model=active_model,
                current_tier=effective_decision.tier,
            )

        budget_tokens = self.loop._prompt_budget_tokens()
        prompt_tokens, prompt_token_source = self.loop._estimate_prompt_tokens(
            messages,
            guarded_tool_defs,
            active_model,
        )
        trimmed_messages, trimmed_tokens, trimmed_source, trim_strategy = self.loop._trim_messages_to_budget(
            messages,
            tools=guarded_tool_defs,
            model=active_model,
            budget_tokens=budget_tokens,
        )
        if trim_strategy != "none":
            logger.warning(
                "Prompt over budget before LLM call (iteration={}): {}/{} via {}, strategy={}",
                iteration,
                prompt_tokens,
                budget_tokens,
                prompt_token_source,
                trim_strategy,
            )
            record_trace_event(
                event_name="llm.prompt.trimmed",
                phase="point",
                status="ok" if trim_strategy != "unfit" else "error",
                component="agent_loop",
                source="agent_loop",
                account_id=(route_metadata or {}).get("account_id") if isinstance(route_metadata, dict) else None,
                tenant_id=(route_metadata or {}).get("tenant_id") if isinstance(route_metadata, dict) else None,
                bot_id=(route_metadata or {}).get("bot_id") if isinstance(route_metadata, dict) else None,
                channel=(route_metadata or {}).get("channel") if isinstance(route_metadata, dict) else None,
                chat_id=(route_metadata or {}).get("chat_id") if isinstance(route_metadata, dict) else None,
                client_id=(route_metadata or {}).get("client_id") if isinstance(route_metadata, dict) else None,
                content_preview=route_text,
                event_type="llm.prompt",
                details={
                    "iteration": iteration,
                    "model": active_model,
                    "budget_tokens": budget_tokens,
                    "before_tokens": prompt_tokens,
                    "before_source": prompt_token_source,
                    "after_tokens": trimmed_tokens,
                    "after_source": trimmed_source,
                    "strategy": trim_strategy,
                    "message_count_before": len(messages),
                    "message_count_after": len(trimmed_messages),
                    "routing": build_routing_observability_payload(
                        (route_metadata or {}).get("_routing") if isinstance(route_metadata, dict) else None,
                    ),
                },
            )
            if trim_strategy != "unfit":
                messages = trimmed_messages
                prompt_tokens = trimmed_tokens
                prompt_token_source = trimmed_source

        if prompt_tokens > 0 and prompt_tokens > budget_tokens:
            err_text = (
                f"当前会话上下文过长（估算 {prompt_tokens} tokens，预算 {budget_tokens} tokens），"
                "已阻止本次模型调用。请发送 /new 开启新会话，或缩短本次输入。"
            )
            record_trace_event(
                event_name="llm.request.rejected",
                phase="end",
                status="error",
                component="agent_loop",
                source="agent_loop",
                account_id=(route_metadata or {}).get("account_id") if isinstance(route_metadata, dict) else None,
                tenant_id=(route_metadata or {}).get("tenant_id") if isinstance(route_metadata, dict) else None,
                bot_id=(route_metadata or {}).get("bot_id") if isinstance(route_metadata, dict) else None,
                channel=(route_metadata or {}).get("channel") if isinstance(route_metadata, dict) else None,
                chat_id=(route_metadata or {}).get("chat_id") if isinstance(route_metadata, dict) else None,
                client_id=(route_metadata or {}).get("client_id") if isinstance(route_metadata, dict) else None,
                content_preview=route_text,
                event_type="llm.request",
                details={
                    "iteration": iteration,
                    "model": active_model,
                    "prompt_tokens": prompt_tokens,
                    "prompt_token_source": prompt_token_source,
                    "budget_tokens": budget_tokens,
                    "routing": build_routing_observability_payload(
                        (route_metadata or {}).get("_routing") if isinstance(route_metadata, dict) else None,
                    ),
                },
            )
            return PlannerIterationResult(
                messages=messages,
                active_model=active_model,
                final_content=err_text,
            )

        record_trace_event(
            event_name="llm.request.started",
            phase="start",
            status="running",
            component="agent_loop",
            source="agent_loop",
            account_id=(route_metadata or {}).get("account_id") if isinstance(route_metadata, dict) else None,
            tenant_id=(route_metadata or {}).get("tenant_id") if isinstance(route_metadata, dict) else None,
            bot_id=(route_metadata or {}).get("bot_id") if isinstance(route_metadata, dict) else None,
            channel=(route_metadata or {}).get("channel") if isinstance(route_metadata, dict) else None,
            chat_id=(route_metadata or {}).get("chat_id") if isinstance(route_metadata, dict) else None,
            client_id=(route_metadata or {}).get("client_id") if isinstance(route_metadata, dict) else None,
            content_preview=route_text,
            event_type="llm.request",
            details={
                "iteration": iteration,
                "model": active_model,
                "prompt_tokens": prompt_tokens,
                "prompt_token_source": prompt_token_source,
                "prompt_budget_tokens": budget_tokens,
                "message_count": len(messages),
                "tool_count": len(guarded_tool_defs),
                "routing": build_routing_observability_payload(
                    (route_metadata or {}).get("_routing") if isinstance(route_metadata, dict) else None,
                ),
            },
        )

        llm_tokens = runtime_context.set_llm_call_context(source="bot", component="agent_loop")
        try:
            provider_factory = self.loop.provider_factory or (lambda _model: self.loop.provider)
            provider = provider_factory(active_model) or self.loop.provider
            response = await provider.chat_with_retry(
                messages=messages,
                tools=guarded_tool_defs,
                model=active_model,
                max_tokens=effective_decision.max_tokens,
                reasoning_effort=effective_decision.reasoning_effort,
            )
        finally:
            runtime_context.reset_llm_call_context(llm_tokens)

        recovered_text_tool_calls: list[ToolCallRequest] = []
        if not response.has_tool_calls:
            recovered_text_tool_calls, recovered_content = self.loop._recover_textual_tool_calls(response.content)
            if recovered_text_tool_calls:
                response.tool_calls = recovered_text_tool_calls
                response.content = recovered_content
                logger.warning(
                    "Recovered {} textual tool call(s) from assistant content",
                    len(recovered_text_tool_calls),
                )

        record_trace_event(
            event_name="llm.response.received",
            phase="end",
            status="ok" if response.finish_reason != "error" else "error",
            component="agent_loop",
            source="agent_loop",
            account_id=(route_metadata or {}).get("account_id") if isinstance(route_metadata, dict) else None,
            tenant_id=(route_metadata or {}).get("tenant_id") if isinstance(route_metadata, dict) else None,
            bot_id=(route_metadata or {}).get("bot_id") if isinstance(route_metadata, dict) else None,
            channel=(route_metadata or {}).get("channel") if isinstance(route_metadata, dict) else None,
            chat_id=(route_metadata or {}).get("chat_id") if isinstance(route_metadata, dict) else None,
            client_id=(route_metadata or {}).get("client_id") if isinstance(route_metadata, dict) else None,
            content_preview=response.content,
            event_type="llm.response",
            details={
                "iteration": iteration,
                "model": active_model,
                "finish_reason": response.finish_reason,
                "has_tool_calls": response.has_tool_calls,
                "tool_call_count": len(response.tool_calls or []),
                "textual_tool_call_recovered": bool(recovered_text_tool_calls),
                "routing": build_routing_observability_payload(
                    (route_metadata or {}).get("_routing") if isinstance(route_metadata, dict) else None,
                ),
            },
        )

        if response.finish_reason == "error":
            err = (response.content or "").lower()
            if "messages" in err and "illegal" in err:
                current_sig = self.loop._messages_signature(messages)
                can_repair = (
                    state.illegal_repair_attempts < 3
                    and current_sig not in state.illegal_repair_signatures
                )
                repaired = self.loop._repair_messages_for_retry(messages)
                if can_repair and repaired and repaired != messages:
                    state.illegal_repair_attempts += 1
                    state.illegal_repair_signatures.add(current_sig)
                    logger.warning("Retrying with repaired message history after provider validation error")
                    return PlannerIterationResult(
                        messages=repaired,
                        active_model=active_model,
                        should_continue=True,
                    )
                minimized = self.loop._minimal_messages_for_retry(messages)
                if can_repair and minimized and minimized != messages:
                    state.illegal_repair_attempts += 1
                    state.illegal_repair_signatures.add(current_sig)
                    logger.warning("Retrying with minimal message context after provider validation error")
                    return PlannerIterationResult(
                        messages=minimized,
                        active_model=active_model,
                        should_continue=True,
                    )

        if response.finish_reason == "error" and fallback_models:
            next_model = fallback_models.pop(0)
            self.loop.policy.record_routing_metric(effective_decision.tier, "fallbacks")
            self.loop._update_routing_state(
                route_metadata,
                current_model=next_model,
                final_model=next_model,
                fallback_used=True,
                fallback_count_delta=1,
            )
            logger.warning("Switching to fallback model: {}", next_model)
            return PlannerIterationResult(
                messages=messages,
                active_model=next_model,
                should_continue=True,
            )

        return PlannerIterationResult(
            messages=messages,
            response=response,
            recovered_text_tool_calls=recovered_text_tool_calls,
            plan=ToolCallPlan.from_tool_calls(list(response.tool_calls or []), parallel=True),
            active_model=active_model,
        )
