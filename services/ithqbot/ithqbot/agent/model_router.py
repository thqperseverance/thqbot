import asyncio
import json
import re
from typing import Any, Literal

from loguru import logger

from ithqbot.config.schema import AgentRoutingConfig, RouteProfile, RoutePurpose, RoutingMatrixEntry
from ithqbot.providers.base import GenerationSettings, LLMProvider

TierName = Literal["small", "medium", "large"]

RoutingDecision = RouteProfile


class ModelRouterConfigError(ValueError):
    """Raised when routing configuration is incomplete."""


class ModelRouter:
    def __init__(
        self,
        provider: LLMProvider,
        default_model: str,
        routing: AgentRoutingConfig,
        generation: GenerationSettings | None = None,
    ) -> None:
        self.provider = provider
        self.default_model = default_model
        self.routing = routing
        self.generation = generation if isinstance(generation, GenerationSettings) else GenerationSettings()

    async def decide(
        self,
        user_text: str,
        metadata: dict[str, Any] | None = None,
        purpose: RoutePurpose | str = RoutePurpose.PLANNER,
    ) -> RoutingDecision:
        meta = metadata or {}
        route_purpose = self._normalize_purpose(purpose)
        override_decision = self._resolve_override(meta, route_purpose)
        if override_decision is not None:
            return override_decision
        base = self._profile_for_tier(route_purpose, "medium", source="default")
        if not self.routing.enabled:
            return base

        rule_profile = self._match_rule_profile(user_text, route_purpose)
        if rule_profile is not None:
            return rule_profile

        try:
            classified = await asyncio.wait_for(
                self._classify(user_text, meta, route_purpose),
                timeout=5.0
            )
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning("Model routing classification failed or timed out: {}. Falling back to patterns.", e)
            classified = None

        if classified:
            return self._profile_from_classifier(
                purpose=route_purpose,
                classified=classified,
                source="classifier",
            )

        pattern_profile = self._match_patterns(user_text, route_purpose)
        if pattern_profile is not None:
            return pattern_profile
        return base

    def _resolve_override(
        self,
        metadata: dict[str, Any],
        purpose: RoutePurpose,
    ) -> RoutingDecision | None:
        tenant_id = str(metadata.get("tenant_id") or "").strip()
        bot_id = str(metadata.get("bot_id") or "").strip()
        for override in self.routing.overrides:
            if override.purpose is not None and override.purpose != purpose:
                continue
            match_tenant = bool(override.tenant_id) and override.tenant_id == tenant_id
            match_bot = bool(override.bot_id) and override.bot_id == bot_id
            if not (match_tenant or match_bot):
                continue
            forced_model = override.forced_model.strip()
            tier = override.forced_tier or self._guess_tier_from_model(forced_model) or "medium"
            return self._profile_for_tier(
                purpose,
                tier,
                source="override",
                active_model=forced_model or None,
                fallback_models=list(override.fallback_models) or None,
                max_tokens=override.max_tokens,
                reasoning_effort=override.reasoning_effort,
            )
        return None

    def _match_rule_profile(self, text: str, purpose: RoutePurpose) -> RoutingDecision | None:
        lowered = text.lower()
        for rule in self.routing.rules:
            if rule.purpose is not None and rule.purpose != purpose:
                continue
            for kw in rule.keywords:
                if kw and kw.lower() in lowered:
                    return self._profile_for_tier(
                        purpose,
                        rule.tier,
                        source="rule",
                        active_model=rule.active_model or None,
                        fallback_models=list(rule.fallback_models) or None,
                        max_tokens=rule.max_tokens,
                        reasoning_effort=rule.reasoning_effort,
                    )
        return None

    def _match_patterns(self, text: str, purpose: RoutePurpose) -> RoutingDecision | None:
        lowered = text.lower()
        if re.search(r"https?://[^\s/$.?#].[^\s]*", text):
            tier: TierName = "medium"
            if purpose == RoutePurpose.FINAL_ANSWER:
                tier = "small"
            return self._profile_for_tier(purpose, tier, source="pattern")
        if any(kw in lowered for kw in ("代码", "code", "python", "javascript", "写个", "修复bug", "debug")):
            tier = "large" if purpose == RoutePurpose.FINAL_ANSWER else "medium"
            return self._profile_for_tier(purpose, tier, source="pattern")
        if any(kw in lowered for kw in ("提取", "抽取", "整理字段", "总结附件")) and purpose == RoutePurpose.EXTRACTION:
            return self._profile_for_tier(purpose, "small", source="pattern")
        return None

    def _profile_for_tier(
        self,
        purpose: RoutePurpose,
        tier: TierName,
        *,
        source: str,
        active_model: str | None = None,
        fallback_models: list[str] | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> RoutingDecision:
        matrix = self._matrix_defaults(purpose, tier)
        purpose_profile = getattr(self.routing.profiles, purpose.value)
        tier_cfg = getattr(self.routing.tiers, tier)
        resolved_active_model = (
            active_model
            or self._tier_model(purpose, tier)
            or purpose_profile.active_model.strip()
            or self.default_model
        )
        if not resolved_active_model:
            raise ModelRouterConfigError(
                "Missing routing model config for "
                f"{self._matrix_field_path(purpose, tier, 'activeModel')}. "
                "Configure `agents.routing.matrix` or an explicit tier/profile model."
            )
        resolved_fallbacks = self._dedupe_models(
            fallback_models
            or self._tier_fallback_models(purpose, tier)
            or matrix.fallback_models
            or purpose_profile.fallback_models
        )
        resolved_max_tokens = (
            max_tokens
            if max_tokens is not None
            else tier_cfg.max_tokens
            or matrix.max_tokens
            or purpose_profile.max_tokens
            or self.generation.max_tokens
        )
        if resolved_max_tokens is None:
            raise ModelRouterConfigError(
                "Missing routing max_tokens config for "
                f"{self._matrix_field_path(purpose, tier, 'maxTokens')}. "
                "Configure `agents.routing.matrix`, tier overrides, purpose profiles, or generation defaults."
            )
        resolved_reasoning = (
            reasoning_effort
            if reasoning_effort is not None
            else tier_cfg.reasoning_effort
            or matrix.reasoning_effort
            or purpose_profile.reasoning_effort
            or self.generation.reasoning_effort
        )
        resolved_fallbacks = [
            candidate for candidate in resolved_fallbacks if candidate and candidate != resolved_active_model
        ]
        return RouteProfile(
            purpose=purpose,
            active_model=resolved_active_model,
            fallback_models=resolved_fallbacks,
            max_tokens=resolved_max_tokens,
            reasoning_effort=resolved_reasoning,
            source=source,
            tier=tier,
        )

    async def _classify(
        self,
        user_text: str,
        metadata: dict[str, Any],
        purpose: RoutePurpose,
    ) -> dict[str, Any] | None:
        router_model = self._classifier_model()
        channel = str(metadata.get("channel") or "")
        tool_hint = str(metadata.get("tool_hint") or "")

        messages = [
            {
                "role": "system",
                "content": (
                    "Return only valid JSON with keys: intent, complexity, need_tools, "
                    "reasoning_intent, risk, suggested_tier, confidence. "
                    "suggested_tier must be one of small/medium/large. confidence is 0..1."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"purpose={purpose.value}\n"
                    f"channel={channel}\n"
                    f"tool_hint={tool_hint}\n"
                    f"user_text={user_text}"
                ),
            },
        ]
        response = await self.provider.chat_with_retry(
            messages=messages,
            model=router_model,
            max_tokens=self.routing.classifier_max_tokens,
            temperature=self.routing.classifier_temperature,
        )
        if response.finish_reason == "error" or not response.content:
            logger.warning("Model routing classify failed: {}", response.content or "empty response")
            return None
        return self._parse_json(response.content)

    def _profile_from_classifier(
        self,
        *,
        purpose: RoutePurpose,
        classified: dict[str, Any],
        source: str,
    ) -> RoutingDecision:
        tier = self._derive_classifier_tier(purpose, classified)
        return self._profile_for_tier(purpose, tier, source=source)

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", text)
            if not match:
                return None
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None

    @staticmethod
    def _normalize_tier(value: Any) -> TierName | None:
        v = str(value or "").lower().strip()
        if v in {"small", "medium", "large"}:
            return v
        return None

    @staticmethod
    def _normalize_purpose(value: RoutePurpose | str) -> RoutePurpose:
        if isinstance(value, RoutePurpose):
            return value
        try:
            return RoutePurpose(str(value).strip().lower())
        except ValueError:
            return RoutePurpose.PLANNER

    @staticmethod
    def _to_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _escalate_tier(tier: TierName) -> TierName:
        if tier == "small":
            return "medium"
        if tier == "medium":
            return "large"
        return "large"

    def _derive_classifier_tier(
        self,
        purpose: RoutePurpose,
        classified: dict[str, Any],
    ) -> TierName:
        tier = self._normalize_tier(classified.get("suggested_tier")) or "medium"
        complexity = str(classified.get("complexity") or "").lower()
        intent = str(classified.get("intent") or "").lower()
        risk = str(classified.get("risk") or "").lower()
        reasoning_intent = str(classified.get("reasoning_intent") or "").lower()
        need_tools = str(classified.get("need_tools") or "").lower() in {"1", "true", "yes", "tool", "tools"}
        confidence = self._to_float(classified.get("confidence"))

        if risk == "high":
            tier = "large"
        if purpose == RoutePurpose.PLANNER and (need_tools or complexity in {"medium", "high", "complex"}):
            tier = "large" if complexity in {"high", "complex"} else "medium"
        if purpose == RoutePurpose.FINAL_ANSWER and (
            complexity in {"high", "complex"}
            or reasoning_intent in {"true", "yes", "high"}
            or "reason" in intent
            or "分析" in intent
        ):
            tier = "large"
        if purpose == RoutePurpose.EXTRACTION and complexity in {"low", "simple"}:
            tier = "small"
        if confidence is None or confidence < self.routing.thresholds.confidence_min:
            tier = self._escalate_tier(tier)
        return tier

    def _classifier_model(self) -> str:
        profile_model = self.routing.profiles.classifier.active_model.strip()
        matrix_model = self._matrix_defaults(RoutePurpose.CLASSIFIER, "medium").active_model.strip()
        resolved = self.routing.router_model.strip() or profile_model or matrix_model
        if resolved:
            return resolved
        raise ModelRouterConfigError(
            "Missing classifier routing model config. Configure "
            "`agents.routing.routerModel`, `agents.routing.profiles.classifier.activeModel`, "
            "or `agents.routing.matrix.classifier.medium.activeModel`."
        )

    def _guess_tier_from_model(self, model: str) -> TierName | None:
        candidate = (model or "").strip()
        if not candidate:
            return None
        lowered = candidate.lower()
        if "100b" in lowered or "72b" in lowered:
            return "large"
        if "32b" in lowered or "27b" in lowered:
            return "medium"
        if "flash" in lowered or "mini" in lowered or "8b" in lowered:
            return "small"
        for tier in ("small", "medium", "large"):
            cfg = getattr(self.routing.tiers, tier)
            if cfg.model == candidate:
                return tier
        return None

    def _tier_model(self, purpose: RoutePurpose, tier: TierName) -> str:
        tier_cfg = getattr(self.routing.tiers, tier)
        if tier_cfg.model:
            return tier_cfg.model
        matrix_model = self._matrix_defaults(purpose, tier).active_model.strip()
        if matrix_model:
            return matrix_model
        purpose_profile = getattr(self.routing.profiles, purpose.value)
        return purpose_profile.active_model.strip()

    def _tier_fallback_models(self, purpose: RoutePurpose, tier: TierName) -> list[str]:
        fallback_models: list[str] = []
        for fb_tier in getattr(self.routing.fallbacks, tier):
            candidate = self._tier_model(purpose, fb_tier)
            if candidate and candidate not in fallback_models:
                fallback_models.append(candidate)
        return fallback_models

    @staticmethod
    def _dedupe_models(models: list[str] | None) -> list[str]:
        deduped: list[str] = []
        for candidate in models or []:
            normalized = str(candidate or "").strip()
            if normalized and normalized not in deduped:
                deduped.append(normalized)
        return deduped

    def _matrix_defaults(self, purpose: RoutePurpose, tier: TierName) -> RoutingMatrixEntry:
        return getattr(getattr(self.routing.matrix, purpose.value), tier)

    @staticmethod
    def _matrix_field_path(purpose: RoutePurpose, tier: TierName, field_name: str) -> str:
        purpose_name = {
            RoutePurpose.CLASSIFIER: "classifier",
            RoutePurpose.PLANNER: "planner",
            RoutePurpose.FINAL_ANSWER: "finalAnswer",
            RoutePurpose.EXTRACTION: "extraction",
            RoutePurpose.VISION: "vision",
        }[purpose]
        return f"agents.routing.matrix.{purpose_name}.{tier}.{field_name}"
