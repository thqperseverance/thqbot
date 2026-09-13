from __future__ import annotations

import pytest
from pydantic import BaseModel

from ithqbot import context as runtime_context
from ithqbot.agent.skills.base import SkillContext
from ithqbot.config.schema import AgentRoutingConfig, Config
from ithqbot.providers.base import LLMResponse


class _StubProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            return LLMResponse(content="", finish_reason="error")
        return LLMResponse(content=self._responses.pop(0), finish_reason="stop")


class _ExtractOut(BaseModel):
    answer: str
    score: int


def _routing_matrix() -> dict:
    return {
        "classifier": {
            "small": {"activeModel": "router-classifier-model", "maxTokens": 256},
            "medium": {"activeModel": "router-classifier-model", "maxTokens": 256},
            "large": {"activeModel": "router-classifier-model", "maxTokens": 256},
        },
        "planner": {
            "small": {"activeModel": "planner-small", "maxTokens": 2048},
            "medium": {"activeModel": "planner-medium", "maxTokens": 4096},
            "large": {"activeModel": "planner-large", "maxTokens": 8192, "reasoningEffort": "high"},
        },
        "finalAnswer": {
            "small": {"activeModel": "final-small", "maxTokens": 2048},
            "medium": {"activeModel": "final-medium", "maxTokens": 4096},
            "large": {"activeModel": "final-large", "maxTokens": 8192, "reasoningEffort": "high"},
        },
        "extraction": {
            "small": {"activeModel": "extract-small", "maxTokens": 1024},
            "medium": {"activeModel": "extract-medium", "maxTokens": 2048},
            "large": {"activeModel": "extract-large", "maxTokens": 4096},
        },
        "vision": {
            "small": {"activeModel": "vision-small", "maxTokens": 2048},
            "medium": {"activeModel": "vision-medium", "maxTokens": 4096},
            "large": {"activeModel": "vision-large", "maxTokens": 8192},
        },
    }


@pytest.mark.anyio
async def test_call_llm_routes_task_to_model() -> None:
    cfg = Config()
    cfg.agents.routing.matrix.final_answer.medium.active_model = "openai/gpt-4o-mini"
    provider = _StubProvider(["ok"])
    seen_models: list[str] = []

    def _factory(model: str):
        seen_models.append(model)
        return provider

    ctx = SkillContext(
        tenant_id="t1",
        account_id="a1",
        chat_id="c1",
        bot_id="b1",
        skill_name="demo_skill",
        config=cfg,
        provider_factory=_factory,
    )
    result = await ctx.call_llm(task="reasoning", prompt="hello")
    assert result == "ok"
    assert seen_models[-1] == "openai/gpt-4o-mini"
    assert provider.calls[0]["model"] == "openai/gpt-4o-mini"


@pytest.mark.anyio
async def test_call_llm_supports_template_and_pydantic_output() -> None:
    cfg = Config()
    provider = _StubProvider(['{"answer":"命中","score":9}'])
    ctx = SkillContext(
        tenant_id="t1",
        account_id="a1",
        chat_id="c1",
        bot_id="b1",
        skill_name="demo_skill",
        config=cfg,
        provider_factory=lambda _model: provider,
    )
    result = await ctx.call_llm(
        task="extraction",
        prompt_template='请输出 JSON：{{"answer":"...","score":0}}。输入={text}',
        template_vars={"text": "abc"},
        output_model=_ExtractOut,
    )
    assert isinstance(result, _ExtractOut)
    assert result.answer == "命中"
    assert result.score == 9


@pytest.mark.anyio
async def test_call_llm_retries_for_invalid_structured_output() -> None:
    cfg = Config()
    provider = _StubProvider(["not json", '{"answer":"二次成功","score":7}'])
    ctx = SkillContext(
        tenant_id="t1",
        account_id="a1",
        chat_id="c1",
        bot_id="b1",
        skill_name="demo_skill",
        config=cfg,
        provider_factory=lambda _model: provider,
    )
    result = await ctx.call_llm(
        task="extraction",
        prompt="返回结构化",
        output_model=_ExtractOut,
        retries=2,
    )
    assert result.answer == "二次成功"
    assert len(provider.calls) == 2


@pytest.mark.anyio
async def test_call_llm_uses_route_profile_when_skill_routing_enabled() -> None:
    cfg = Config()
    cfg.agents.routing = AgentRoutingConfig.model_validate(
        {
            "enabled": True,
            "matrix": _routing_matrix(),
            "rules": [
                {
                    "name": "reasoning-final",
                    "keywords": ["深入分析"],
                    "purpose": "final_answer",
                    "tier": "large",
                    "activeModel": "minimax/minimax2.5-100B",
                    "maxTokens": 3072,
                    "reasoningEffort": "high",
                }
            ],
        }
    )
    router_provider = _StubProvider(["router-unused"])
    active_provider = _StubProvider(["route-ok"])

    def _factory(model: str):
        if model == "minimax/minimax2.5-100B":
            return active_provider
        return router_provider

    ctx = SkillContext(
        tenant_id="t1",
        account_id="a1",
        chat_id="c1",
        bot_id="b1",
        skill_name="demo_skill",
        config=cfg,
        provider_factory=_factory,
    )
    result = await ctx.call_llm(task="reasoning", prompt="请深入分析这个方案")

    assert result == "route-ok"
    assert router_provider.calls == []
    assert active_provider.calls[0]["model"] == "minimax/minimax2.5-100B"
    assert active_provider.calls[0]["max_tokens"] == 3072
    assert active_provider.calls[0]["reasoning_effort"] == "high"


@pytest.mark.anyio
async def test_resolve_llm_route_profile_and_trace_use_fixed_routing_schema(monkeypatch) -> None:
    cfg = Config()
    cfg.agents.routing = AgentRoutingConfig.model_validate(
        {
            "enabled": True,
            "matrix": _routing_matrix(),
            "rules": [
                {
                    "name": "extract-json",
                    "keywords": ["结构化"],
                    "purpose": "extraction",
                    "tier": "small",
                    "activeModel": "deepseek/deepseek-3.4-flash",
                    "maxTokens": 600,
                }
            ],
        }
    )
    provider = _StubProvider(["{\"answer\":\"ok\",\"score\":1}"])
    captured: list[dict] = []

    def _factory(_model: str):
        return provider

    def _capture(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr("ithqbot.agent.skills.llm_router.record_trace_event", _capture)
    tokens = runtime_context.set_runtime_context(
        account="a1",
        tenant="t1",
        bot="b1",
        channel_name="icatmsg",
        chat="c1",
        metadata={"request_msg_id": "req-1", "trace_id": "trace-1", "client_id": "web"},
    )
    try:
        ctx = SkillContext(
            tenant_id="t1",
            account_id="a1",
            chat_id="c1",
            bot_id="b1",
            skill_name="demo_skill",
            config=cfg,
            provider_factory=_factory,
        )
        profile = await ctx.resolve_llm_route_profile(task="extraction", prompt="请结构化提取字段")
        await ctx.call_llm(task="extraction", prompt="请结构化提取字段", output_model=_ExtractOut)
    finally:
        runtime_context.reset_runtime_context(tokens)

    assert profile.purpose.value == "extraction"
    assert profile.active_model == "deepseek/deepseek-3.4-flash"
    trace = captured[-1]
    assert trace["event_name"] == "skill.llm.routed"
    assert trace["details"]["routing"]["selected_purpose"] == "extraction"
    assert trace["details"]["routing"]["current_model"] == "deepseek/deepseek-3.4-flash"
    assert trace["details"]["routing"]["task"] == "extraction"
    assert trace["details"]["routing"]["skill_name"] == "demo_skill"
