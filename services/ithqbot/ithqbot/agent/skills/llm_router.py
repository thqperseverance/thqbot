"""LLM task router for SkillContext.call_llm()."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ithqbot import context as runtime_context
from ithqbot.agent.model_router import ModelRouter
from ithqbot.config.schema import RouteProfile, RoutePurpose
from ithqbot.observability import build_routing_observability_payload, record_trace_event
from ithqbot.providers.base import LLMProvider, LLMResponse

TModel = TypeVar("TModel", bound=BaseModel)


@dataclass(frozen=True)
class PromptTemplateEngine:
    templates: dict[str, str]

    def render(self, *, prompt: str | None, prompt_template: str | None, template_vars: dict[str, Any] | None) -> str:
        if prompt_template:
            source = self.templates.get(prompt_template, prompt_template)
            values = {"prompt": prompt or ""}
            if template_vars:
                values.update(template_vars)
            try:
                return source.format(**values)
            except KeyError as exc:
                missing = exc.args[0] if exc.args else "unknown"
                raise ValueError(f"Missing prompt template variable: {missing}") from exc
        return (prompt or "").strip()


class LLMTaskRouter:
    """Task-oriented LLM call layer used by skills."""

    def __init__(
        self,
        *,
        config: Any,
        provider_factory: Any,
        skill_name: str,
    ) -> None:
        self._config = config
        self._provider_factory = provider_factory
        self._skill_name = skill_name
        self._templates = PromptTemplateEngine(
            templates={
                "qa_with_evidence": (
                    "请基于以下内容回答问题，不要编造。\n"
                    "问题：{query}\n\n"
                    "证据：\n{evidence}\n\n"
                    "输出要求：先给结论，再给 2-5 条依据。"
                )
            }
        )
        self._model_router: ModelRouter | None = None

    @staticmethod
    def _task_to_purpose(task: str) -> RoutePurpose | None:
        mapping = {
            "classifier": RoutePurpose.CLASSIFIER,
            "planner": RoutePurpose.PLANNER,
            "tool_calling": RoutePurpose.PLANNER,
            "reasoning": RoutePurpose.FINAL_ANSWER,
            "final_answer": RoutePurpose.FINAL_ANSWER,
            "extraction": RoutePurpose.EXTRACTION,
            "vision": RoutePurpose.VISION,
        }
        return mapping.get(str(task or "").strip().lower())

    @staticmethod
    def _extract_route_text(
        *,
        prompt: str,
        messages: list[dict[str, Any]],
        task: str,
    ) -> str:
        if prompt.strip():
            return prompt.strip()
        parts: list[str] = [f"task={task}"]
        for message in messages:
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                parts.append(content.strip())
                continue
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
        return "\n".join(parts).strip()

    def _get_model_router(self) -> ModelRouter:
        if self._model_router is not None:
            return self._model_router
        agents = getattr(self._config, "agents", None)
        routing = getattr(agents, "routing", None)
        defaults = getattr(agents, "defaults", None)
        if routing is None:
            raise RuntimeError("Skill LLM routing is not configured")
        default_model = str(getattr(defaults, "model", "") or "")
        router_model = str(getattr(routing, "router_model", "") or default_model)
        provider = self._provider_factory(router_model or default_model)
        self._model_router = ModelRouter(
            provider=provider,
            default_model=default_model,
            routing=routing,
            generation=getattr(provider, "generation", None),
        )
        return self._model_router

    async def resolve_route_profile(
        self,
        *,
        task: str,
        prompt: str,
        messages: list[dict[str, Any]],
        model: str | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> RouteProfile:
        task_name = task.strip() if isinstance(task, str) else ""
        mapped_purpose = self._task_to_purpose(task_name)
        if model:
            return RouteProfile(
                purpose=mapped_purpose or RoutePurpose.PLANNER,
                active_model=model,
                fallback_models=[],
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                source="skill_override",
            )

        router = self._get_model_router()
        profile = await router.decide(
            self._extract_route_text(prompt=prompt, messages=messages, task=task_name),
            metadata={"channel": "skill", "skill_name": self._skill_name, "task": task_name},
            purpose=mapped_purpose or RoutePurpose.PLANNER,
        )
        if max_tokens is not None:
            profile.max_tokens = max_tokens
        if reasoning_effort is not None:
            profile.reasoning_effort = reasoning_effort
        return profile

    async def resolve_profile_for_call(
        self,
        *,
        task: str,
        prompt: str | None = None,
        prompt_template: str | None = None,
        template_vars: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> RouteProfile:
        rendered_prompt = self._templates.render(
            prompt=prompt,
            prompt_template=prompt_template,
            template_vars=template_vars,
        )
        base_messages = self._build_messages(messages=messages, system_prompt=system_prompt, prompt=rendered_prompt)
        return await self.resolve_route_profile(
            task=task,
            prompt=rendered_prompt,
            messages=base_messages,
            model=model,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )

    def _record_route_trace(self, *, task: str, route_profile: RouteProfile) -> None:
        runtime = runtime_context.get_runtime_context()
        record_trace_event(
            event_name="skill.llm.routed",
            phase="point",
            status="ok",
            component=self._skill_name or "skill",
            source="skill",
            account_id=runtime.get("account_id"),
            tenant_id=runtime.get("tenant_id"),
            bot_id=runtime.get("bot_id"),
            channel=runtime.get("channel"),
            chat_id=runtime.get("chat_id"),
            client_id=runtime.get("client_id"),
            request_msg_id=runtime.get("request_msg_id"),
            trace_id=runtime.get("trace_id"),
            content_preview=task,
            event_type="routing",
            details={
                "task": task,
                "routing": build_routing_observability_payload(
                    route_profile=route_profile,
                    task=task,
                    skill_name=self._skill_name,
                ),
            },
        )

    async def call(
        self,
        *,
        task: str,
        prompt: str | None = None,
        prompt_template: str | None = None,
        template_vars: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        output_model: type[TModel] | None = None,
        retries: int = 2,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> str | TModel:
        route_profile = await self.resolve_profile_for_call(
            task=task,
            prompt=prompt,
            prompt_template=prompt_template,
            template_vars=template_vars,
            messages=messages,
            system_prompt=system_prompt,
            model=model,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
        rendered_prompt = self._templates.render(
            prompt=prompt,
            prompt_template=prompt_template,
            template_vars=template_vars,
        )
        base_messages = self._build_messages(messages=messages, system_prompt=system_prompt, prompt=rendered_prompt)
        self._record_route_trace(task=task, route_profile=route_profile)
        resolved_model = route_profile.active_model
        provider = self._provider_factory(resolved_model)
        attempts = max(1, int(retries) + 1)
        validation_hint = ""

        for _ in range(attempts):
            response = await self._chat_once(
                provider=provider,
                model=resolved_model,
                messages=base_messages + ([{"role": "user", "content": validation_hint}] if validation_hint else []),
                temperature=temperature,
                max_tokens=route_profile.max_tokens,
                reasoning_effort=route_profile.reasoning_effort,
            )
            if response.finish_reason == "error":
                validation_hint = "上一次调用失败，请重试并仅返回有效内容。"
                continue
            content = (response.content or "").strip()
            if not output_model:
                if content:
                    return content
                validation_hint = "返回内容为空，请返回可读文本。"
                continue
            parsed = self._parse_structured_output(content, output_model)
            if parsed is not None:
                return parsed
            validation_hint = (
                "请只返回严格 JSON，且必须匹配该 schema："
                f"{json.dumps(output_model.model_json_schema(), ensure_ascii=False)}"
            )

        if output_model:
            raise RuntimeError(f"LLM structured output parsing failed for task '{task}'")
        raise RuntimeError(f"LLM call failed for task '{task}'")

    async def _chat_once(
        self,
        *,
        provider: LLMProvider,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        reasoning_effort: str | None,
    ) -> LLMResponse:
        tokens = runtime_context.set_llm_call_context(
            source="skill",
            component=self._skill_name or model,
        )
        try:
            kwargs: dict[str, Any] = {"messages": messages, "model": model}
            if temperature is not None:
                kwargs["temperature"] = temperature
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens
            if reasoning_effort is not None:
                kwargs["reasoning_effort"] = reasoning_effort
            return await provider.chat_with_retry(**kwargs)
        finally:
            runtime_context.reset_llm_call_context(tokens)

    @staticmethod
    def _build_messages(
        *,
        messages: list[dict[str, Any]] | None,
        system_prompt: str | None,
        prompt: str,
    ) -> list[dict[str, Any]]:
        if messages:
            return list(messages)
        output: list[dict[str, Any]] = []
        if system_prompt:
            output.append({"role": "system", "content": system_prompt})
        if prompt:
            output.append({"role": "user", "content": prompt})
        return output

    @staticmethod
    def _parse_structured_output(content: str, output_model: type[TModel]) -> TModel | None:
        text = (content or "").strip()
        if not text:
            return None
        for candidate in LLMTaskRouter._json_candidates(text):
            try:
                return output_model.model_validate_json(candidate)
            except ValidationError:
                continue
            except Exception:
                continue
        return None

    @staticmethod
    def _json_candidates(text: str) -> list[str]:
        candidates = [text]
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            candidates.append(match.group(0))
        return candidates
