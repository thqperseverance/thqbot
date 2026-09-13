import pytest

from ithqbot.agent.model_router import ModelRouter
from ithqbot.config.schema import AgentRoutingConfig, RoutePurpose
from ithqbot.providers.base import LLMResponse


class StubProvider:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0
        self.last_kwargs = {}

    async def chat_with_retry(self, **kwargs):
        self.calls += 1
        self.last_kwargs = dict(kwargs)
        return LLMResponse(content=self.response, finish_reason="stop")


def _routing_matrix() -> dict:
    return {
        "classifier": {
            "small": {"activeModel": "deepseek/deepseek-3.4-flash", "maxTokens": 256},
            "medium": {"activeModel": "deepseek/deepseek-3.4-flash", "maxTokens": 256},
            "large": {"activeModel": "deepseek/deepseek-3.4-flash", "maxTokens": 256},
        },
        "planner": {
            "small": {
                "activeModel": "qwen3-32B",
                "fallbackModels": ["qwen3-72B"],
                "maxTokens": 4096,
                "reasoningEffort": "low",
            },
            "medium": {
                "activeModel": "qwen3-32B",
                "fallbackModels": ["qwen3-72B"],
                "maxTokens": 4096,
                "reasoningEffort": "medium",
            },
            "large": {
                "activeModel": "qwen3-72B",
                "fallbackModels": ["qwen3-32B"],
                "maxTokens": 8192,
                "reasoningEffort": "medium",
            },
        },
        "finalAnswer": {
            "small": {
                "activeModel": "deepseek/deepseek-3.4-flash",
                "fallbackModels": ["qwen3-27B"],
                "maxTokens": 2048,
            },
            "medium": {
                "activeModel": "qwen3-27B",
                "fallbackModels": ["deepseek/deepseek-3.4-flash"],
                "maxTokens": 4096,
                "reasoningEffort": "medium",
            },
            "large": {
                "activeModel": "minimax/minimax2.5-100B",
                "fallbackModels": ["qwen3-27B", "deepseek/deepseek-3.4-flash"],
                "maxTokens": 8192,
                "reasoningEffort": "high",
            },
        },
        "extraction": {
            "small": {
                "activeModel": "deepseek/deepseek-3.4-flash",
                "fallbackModels": ["qwen3-27B"],
                "maxTokens": 2048,
            },
            "medium": {
                "activeModel": "qwen3-27B",
                "fallbackModels": ["deepseek/deepseek-3.4-flash"],
                "maxTokens": 4096,
            },
            "large": {
                "activeModel": "qwen3-27B",
                "fallbackModels": ["deepseek/deepseek-3.4-flash"],
                "maxTokens": 4096,
                "reasoningEffort": "medium",
            },
        },
        "vision": {
            "small": {
                "activeModel": "Qwen/Qwen3-VL-32B-Thinking",
                "fallbackModels": ["qwen3-72B"],
                "maxTokens": 4096,
            },
            "medium": {
                "activeModel": "Qwen/Qwen3-VL-32B-Thinking",
                "fallbackModels": ["qwen3-72B"],
                "maxTokens": 4096,
                "reasoningEffort": "medium",
            },
            "large": {
                "activeModel": "Qwen/Qwen3-VL-32B-Thinking",
                "fallbackModels": ["qwen3-72B"],
                "maxTokens": 8192,
                "reasoningEffort": "high",
            },
        },
    }


@pytest.mark.asyncio
async def test_router_rule_takes_priority():
    provider = StubProvider(response='{"suggested_tier":"small","confidence":0.99,"risk":"low"}')
    cfg = AgentRoutingConfig.model_validate(
        {
            "enabled": True,
            "routerModel": "openrouter/qwen/qwen3-8b",
            "matrix": _routing_matrix(),
            "tiers": {
                "small": {"model": "model-small"},
                "medium": {"model": "model-medium"},
                "large": {"model": "model-large"},
            },
            "rules": [
                {"name": "coding", "keywords": ["代码", "bug"], "tier": "large"},
            ],
        }
    )
    router = ModelRouter(provider=provider, default_model="default-model", routing=cfg)
    decision = await router.decide("帮我修复这个bug")

    assert decision.model == "model-large"
    assert decision.source == "rule"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_router_escalates_when_confidence_low():
    provider = StubProvider(
        response='{"suggested_tier":"small","confidence":0.30,"risk":"low","reasoning_effort":"low"}'
    )
    cfg = AgentRoutingConfig.model_validate(
        {
            "enabled": True,
            "routerModel": "openrouter/qwen/qwen3-8b",
            "matrix": _routing_matrix(),
            "thresholds": {"confidenceMin": 0.72},
            "tiers": {
                "small": {"model": "model-small"},
                "medium": {"model": "model-medium"},
                "large": {"model": "model-large"},
            },
            "fallbacks": {"small": ["medium", "large"], "medium": ["large"], "large": []},
        }
    )
    router = ModelRouter(provider=provider, default_model="default-model", routing=cfg)
    decision = await router.decide("你好")

    assert decision.tier == "medium"
    assert decision.model == "model-medium"
    assert decision.fallback_models == ["model-large"]
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_router_applies_bot_override_before_classifier():
    provider = StubProvider(response='{"suggested_tier":"small","confidence":0.99,"risk":"low"}')
    cfg = AgentRoutingConfig.model_validate(
        {
            "enabled": True,
            "routerModel": "openrouter/qwen/qwen3-8b",
            "matrix": _routing_matrix(),
            "tiers": {
                "small": {"model": "model-small"},
                "medium": {"model": "model-medium"},
                "large": {"model": "model-large"},
            },
            "overrides": [
                {
                    "name": "botB-force",
                    "botId": "bot_B",
                    "forcedTier": "large",
                    "fallbackModels": ["model-medium"],
                }
            ],
        }
    )
    router = ModelRouter(provider=provider, default_model="default-model", routing=cfg)
    decision = await router.decide("你好", {"bot_id": "bot_B"})

    assert decision.source == "override"
    assert decision.tier == "large"
    assert decision.model == "model-large"
    assert decision.fallback_models == ["model-medium"]
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_router_uses_deepseek_flash_classifier_and_reasoning_matrix_for_final_answer():
    provider = StubProvider(
        response='{"intent":"analysis","complexity":"high","reasoning_intent":"high","confidence":0.96,"risk":"low","suggested_tier":"medium"}'
    )
    cfg = AgentRoutingConfig.model_validate({"enabled": True, "matrix": _routing_matrix()})
    router = ModelRouter(provider=provider, default_model="default-model", routing=cfg)

    decision = await router.decide("请深入分析这个方案", purpose=RoutePurpose.FINAL_ANSWER)

    assert decision.purpose == RoutePurpose.FINAL_ANSWER
    assert decision.tier == "large"
    assert decision.active_model == "minimax/minimax2.5-100B"
    assert decision.fallback_models == ["qwen3-27B", "deepseek/deepseek-3.4-flash"]
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_router_uses_deepseek_flash_as_classifier_model():
    provider = StubProvider(response='{"suggested_tier":"small","confidence":0.99,"risk":"low"}')
    cfg = AgentRoutingConfig.model_validate({"enabled": True, "matrix": _routing_matrix()})
    router = ModelRouter(provider=provider, default_model="default-model", routing=cfg)

    await router.decide("你好", purpose=RoutePurpose.EXTRACTION)

    assert provider.calls == 1
    assert provider.last_kwargs["model"] == "deepseek/deepseek-3.4-flash"


@pytest.mark.asyncio
async def test_router_uses_matrix_fallbacks_when_tier_models_are_not_configured():
    provider = StubProvider(
        response='{"suggested_tier":"medium","confidence":0.98,"risk":"low","complexity":"medium"}'
    )
    cfg = AgentRoutingConfig.model_validate({"enabled": True, "matrix": _routing_matrix()})
    router = ModelRouter(provider=provider, default_model="default-model", routing=cfg)

    decision = await router.decide("帮我规划一个带工具调用的任务", purpose=RoutePurpose.PLANNER)

    assert decision.active_model == "qwen3-32B"
    assert decision.fallback_models == ["qwen3-72B"]


@pytest.mark.asyncio
async def test_router_supports_all_tiers_shorthand():
    provider = StubProvider(response='{"suggested_tier":"small","confidence":0.99,"risk":"low"}')
    cfg = AgentRoutingConfig.model_validate(
        {
            "enabled": True,
            "matrix": {
                "classifier": {
                    "allTiers": {
                        "activeModel": "qwen3.5",
                        "maxTokens": 256,
                    }
                },
                "planner": {
                    "allTiers": {
                        "activeModel": "qwen3.5",
                        "maxTokens": 4096,
                    }
                },
                "finalAnswer": {
                    "allTiers": {
                        "activeModel": "qwen3.5",
                        "maxTokens": 4096,
                    }
                },
                "extraction": {
                    "allTiers": {
                        "activeModel": "qwen3.5",
                        "maxTokens": 2048,
                    }
                },
                "vision": {
                    "allTiers": {
                        "activeModel": "qwen2-vl",
                        "maxTokens": 4096,
                    }
                },
            },
        }
    )
    router = ModelRouter(provider=provider, default_model="default-model", routing=cfg)

    decision = await router.decide("请提取字段", purpose=RoutePurpose.EXTRACTION)

    assert decision.active_model == "qwen3.5"


@pytest.mark.asyncio
async def test_router_raises_when_matrix_config_is_missing():
    provider = StubProvider(response='{"suggested_tier":"small","confidence":0.99,"risk":"low"}')
    cfg = AgentRoutingConfig.model_validate({"enabled": True})
    router = ModelRouter(provider=provider, default_model="default-model", routing=cfg)

    with pytest.raises(ValueError, match="agents\\.routing\\.matrix"):
        await router.decide("你好", purpose=RoutePurpose.PLANNER)
