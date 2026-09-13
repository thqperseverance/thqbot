from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ithqbot.config.schema import RoutePurpose

if TYPE_CHECKING:
    from ithqbot.agent.loop import AgentLoop
    from ithqbot.config.schema import BotGuardrailsPolicy
    from ithqbot.agent.model_router import RoutingDecision


@dataclass(frozen=True)
class GuardrailsHit:
    policy: str
    action: str
    detail: str


@dataclass(frozen=True)
class OutputReviewResult:
    text: str | None
    guardrails_hits: tuple[GuardrailsHit, ...] = ()


@dataclass
class PolicyTurn:
    active_bot_id: str
    guardrails_policy: "BotGuardrailsPolicy | None"
    guarded_tool_defs: list[dict[str, Any]]
    allowed_tool_names: tuple[str, ...] = ()
    decision_trace: tuple[str, ...] = ()
    decision: "RoutingDecision | None" = None
    active_model: str = ""
    fallback_models: list[str] | None = None
    blocked_response: str | None = None
    guardrails_hits: tuple[GuardrailsHit, ...] = ()
    routing_state: dict[str, Any] = field(default_factory=dict)


class AgentPolicy:
    def __init__(self, loop: "AgentLoop"):
        self.loop = loop

    def record_routing_metric(self, tier: str, key: str) -> None:
        self.loop._record_routing_metric(tier, key)

    @staticmethod
    def _resolve_route_purpose(route_metadata: dict[str, Any] | None) -> RoutePurpose:
        candidate = ""
        if isinstance(route_metadata, dict):
            candidate = str(
                route_metadata.get("_route_purpose")
                or route_metadata.get("route_purpose")
                or ""
            ).strip()
        try:
            return RoutePurpose(candidate)
        except ValueError:
            return RoutePurpose.PLANNER

    async def resolve_turn(
        self,
        *,
        route_text: str,
        route_metadata: dict[str, Any] | None,
        tool_defs: list[dict[str, Any]],
    ) -> PolicyTurn:
        active_bot_id, guardrails_policy = self.resolve_bot_guardrails_policy(route_metadata)
        blocked_instruction, blocked_hits = self.block_instruction(
            route_text,
            active_bot_id,
            guardrails_policy,
        )
        if blocked_instruction is not None:
            return PolicyTurn(
                active_bot_id=active_bot_id,
                guardrails_policy=guardrails_policy,
                guarded_tool_defs=[],
                blocked_response=blocked_instruction,
                guardrails_hits=blocked_hits,
            )

        route_purpose = self._resolve_route_purpose(route_metadata)
        decision = await self.loop.model_router.decide(
            route_text,
            route_metadata,
            purpose=route_purpose,
        )
        active_model = decision.model
        fallback_models = list(decision.fallback_models)
        decision_trace: list[str] = []
        route_target = str((route_metadata or {}).get("bot_id") or active_bot_id or "").strip()
        if route_target:
            decision_trace.append(f"route: {route_target}")
        decision_trace.append(f"purpose: {decision.purpose.value}")
        guarded_tool_defs: list[dict[str, Any]] = []
        allowed_tool_names: list[str] = []
        tool_hits: list[GuardrailsHit] = []
        for tool_def in tool_defs:
            if not isinstance(tool_def, dict):
                continue
            fn = tool_def.get("function")
            if not isinstance(fn, dict):
                continue
            tool_name = fn.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                continue
            is_allowed, hits = self.is_tool_allowed(
                tool_name,
                active_bot_id,
                guardrails_policy,
            )
            tool_hits.extend(hits)
            if is_allowed:
                guarded_tool_defs.append(tool_def)
                allowed_tool_names.append(tool_name)
            else:
                decision_trace.append(f"block: {tool_name} denied")
        if allowed_tool_names:
            decision_trace.append(
                "guardrails: allow=[" + ", ".join(allowed_tool_names) + "]"
            )

        return PolicyTurn(
            active_bot_id=active_bot_id,
            guardrails_policy=guardrails_policy,
            guarded_tool_defs=guarded_tool_defs,
            allowed_tool_names=tuple(allowed_tool_names),
            decision_trace=tuple(decision_trace),
            decision=decision,
            active_model=active_model,
            fallback_models=fallback_models,
            guardrails_hits=tuple(blocked_hits) + tuple(tool_hits),
            routing_state={
                "source": decision.source,
                "requested_purpose": route_purpose.value,
                "selected_purpose": decision.purpose.value,
                "current_purpose": decision.purpose.value,
                "purpose": decision.purpose.value,
                "tier": decision.tier,
                "initial_tier": decision.tier,
                "current_tier": decision.tier,
                "model": decision.model,
                "initial_model": decision.model,
                "current_model": decision.model,
                "fallback_models": list(decision.fallback_models),
                "max_tokens": decision.max_tokens,
                "reasoning_effort": decision.reasoning_effort,
                "fallback_count": 0,
                "fallback_used": False,
                "final_model": decision.model,
                "success": False,
            },
        )

    def resolve_bot_guardrails_policy(
        self,
        route_metadata: dict[str, Any] | None,
    ) -> tuple[str, "BotGuardrailsPolicy | None"]:
        cfg = self.loop.bot_guardrails_config
        if not cfg.enabled:
            return "", None
        bot_id = self.loop._resolve_active_bot_id(route_metadata)
        if bot_id and bot_id in cfg.bots:
            return bot_id, cfg.bots[bot_id]
        return bot_id, cfg.default_policy

    @staticmethod
    def text_matches_pattern(text: str, pattern: str) -> bool:
        try:
            return re.search(pattern, text, flags=re.IGNORECASE) is not None
        except re.error:
            return pattern.lower() in text.lower()

    def block_instruction(
        self,
        text: str,
        bot_id: str,
        policy: "BotGuardrailsPolicy | None",
    ) -> tuple[str | None, tuple[GuardrailsHit, ...]]:
        _ = bot_id
        if not policy or not policy.enabled or not text:
            return None, ()
        for pattern in policy.blocked_instruction_patterns:
            if isinstance(pattern, str) and pattern and self.text_matches_pattern(text, pattern):
                return (
                    policy.blocked_instruction_message,
                    (GuardrailsHit("blocked_instruction_patterns", "blocked", pattern),),
                )
        return None, ()

    def review_output(
        self,
        text: str | None,
        bot_id: str,
        policy: "BotGuardrailsPolicy | None",
    ) -> OutputReviewResult:
        _ = bot_id
        if not policy or not policy.enabled or not text:
            return OutputReviewResult(text=text)
        lower_text = text.lower()
        for word in policy.sensitive_words:
            if isinstance(word, str) and word and word.lower() in lower_text:
                return OutputReviewResult(
                    text=policy.sensitive_word_message,
                    guardrails_hits=(GuardrailsHit("sensitive_words", "blocked", word),),
                )
        return OutputReviewResult(text=text)

    def is_tool_allowed(
        self,
        tool_name: str,
        bot_id: str,
        policy: "BotGuardrailsPolicy | None",
    ) -> tuple[bool, tuple[GuardrailsHit, ...]]:
        _ = bot_id
        if not policy or not policy.enabled:
            return True, ()
        deny = {item for item in policy.tool_denylist if isinstance(item, str) and item}
        if tool_name in deny:
            return (
                False,
                (GuardrailsHit("tool_denylist", "blocked", tool_name),),
            )
        allow = {item for item in policy.tool_allowlist if isinstance(item, str) and item}
        if allow and tool_name not in allow:
            return (
                False,
                (GuardrailsHit("tool_allowlist", "blocked", tool_name),),
            )
        return True, ()

    def validate_tool_args(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        policy: "BotGuardrailsPolicy | None" = None,
    ) -> tuple[bool, str | None]:
        """Verify that tool arguments do not violate security policies."""
        if not policy:
            policy = self.resolve_bot_guardrails_policy(None)[1]
        if not policy or not policy.enabled:
            return True, None

        # 1. Path Traversal & Absolute Path Checks
        path_fields = {"path", "filepath", "filename", "dir", "directory", "src", "dest"}
        for key, value in arguments.items():
            if key.lower() in path_fields and isinstance(value, str):
                # Block path traversal attempts
                if ".." in value:
                    return False, f"安全拦截：检测到非法路径回溯操作 ({value})"
                # Block absolute paths if restriction is enabled
                if value.startswith("/") and self.loop.restrict_to_workspace:
                    return False, f"安全拦截：禁止访问工作区外的绝对路径 ({value})"

        # 2. Command Injection Checks
        cmd_fields = {"command", "cmd", "shell", "script", "code"}
        blocked_shell_metachars = {";", "&&", "||", "|", "`", "$(", ">", "<"}
        for key, value in arguments.items():
            if key.lower() in cmd_fields and isinstance(value, str):
                if any(char in value for char in blocked_shell_metachars):
                    return False, f"安全拦截：检测到潜在的命令注入操作"

        return True, None
