"""Base classes for Python-based skills."""

from dataclasses import dataclass, field
from typing import Any, TypeVar

from ithqbot.observability import audit_call, record_trace_event
from ithqbot.config.schema import RouteProfile
from pydantic import BaseModel

from .llm_router import LLMTaskRouter

TModel = TypeVar("TModel", bound=BaseModel)


@dataclass
class SkillContext:
    """Context for skill execution, providing identity and resource access."""

    # --- Identity & Routing ---
    tenant_id: str
    account_id: str
    chat_id: str
    bot_id: str
    channel_user_id: str = ""
    client_id: str = "web"

    # --- Tracing ---
    trace_id: str = ""
    request_msg_id: str = ""
    parent_run_id: str = ""
    priority: str = "normal"

    # --- Skill Info ---
    skill_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    # --- Resource Access (Injected by Runner) ---
    config: Any = None  # Full ithqbot Config object
    provider_factory: Any = None  # Callable or object to get LLM providers
    emit_callback: Any = None  # Callback for progress updates
    log_callback: Any = None
    file_service: Any = None
    cancellation_token: Any = None
    _llm_router: LLMTaskRouter | None = field(default=None, init=False, repr=False)

    async def emit_progress(self, percent: int, stage: str, message: str, **kwargs: Any) -> None:
        """Emit real-time progress back to the user."""
        await self.log_event("progress", {"percent": percent, "stage": stage, "message": message, **kwargs})
        details_payload = dict(kwargs.get("details", {})) if isinstance(kwargs.get("details"), dict) else {}
        status_details = kwargs.get("status_details")
        if isinstance(status_details, dict):
            details_payload["status_details"] = status_details

        # Record trace event for internal skill status tracking
        record_trace_event(
            event_name="skill.progress",
            phase="running",
            status="running",
            component=self.skill_name or "skill",
            source="skill",
            account_id=self.account_id,
            tenant_id=self.tenant_id,
            bot_id=self.bot_id,
            chat_id=self.chat_id,
            client_id=self.client_id,
            request_msg_id=self.request_msg_id,
            trace_id=self.trace_id,
            parent_run_id=self.parent_run_id,
            event_type="skill.progress",
            details={
                "percent": percent,
                "stage": stage,
                "status_text": message,
                "run_id": self.skill_name or "skill",
                **details_payload,
            },
        )

        if self.emit_callback:
            try:
                await self.emit_callback(percent, stage, message, **kwargs)
            except TypeError:
                await self.emit_callback(percent, stage, message)

    async def emit_interaction(self, interaction: dict[str, Any], message: str = "") -> None:
        await self.log_event("interaction", {"interaction": interaction, "message": message})
        if not self.emit_callback:
            return
        try:
            await self.emit_callback(
                message or interaction.get("prompt") or interaction.get("title") or "",
                interaction=interaction,
                status_event="interaction",
            )
            return
        except TypeError:
            pass
        try:
            await self.emit_callback(-1, "interaction", message or interaction.get("prompt") or "")
        except TypeError:
            await self.emit_callback(message or interaction.get("prompt") or "")

    async def log_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.log_callback:
            await self.log_callback(event_type, payload)
        audit_call(
            call_type="skill",
            name=self.skill_name or "unknown_skill",
            phase=f"event:{event_type}",
            interaction_data=payload,
        )

    async def call_llm(
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
        if not self.config or not self.provider_factory:
            raise RuntimeError("SkillContext not initialized with config or provider_factory")
        if self._llm_router is None:
            self._llm_router = LLMTaskRouter(
                config=self.config,
                provider_factory=self.provider_factory,
                skill_name=self.skill_name,
            )
        return await self._llm_router.call(
            task=task,
            prompt=prompt,
            prompt_template=prompt_template,
            template_vars=template_vars,
            messages=messages,
            system_prompt=system_prompt,
            output_model=output_model,
            retries=retries,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )

    async def resolve_llm_route_profile(
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
        if not self.config or not self.provider_factory:
            raise RuntimeError("SkillContext not initialized with config or provider_factory")
        if self._llm_router is None:
            self._llm_router = LLMTaskRouter(
                config=self.config,
                provider_factory=self.provider_factory,
                skill_name=self.skill_name,
            )
        return await self._llm_router.resolve_profile_for_call(
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

    def _require_file_service(self) -> Any:
        if self.file_service is None:
            raise RuntimeError("FileService is not configured for current skill context")
        return self.file_service

    async def save_file(
        self,
        name: str,
        content: str | bytes,
        mime: str = "application/octet-stream",
    ) -> dict[str, Any]:
        service = self._require_file_service()
        file_meta = await service.save(
            tenant_id=self.tenant_id,
            account_id=self.account_id,
            bot_id=self.bot_id,
            chat_id=self.chat_id,
            request_msg_id=self.request_msg_id,
            trace_id=self.trace_id,
            name=name,
            content=content,
            mime=mime,
            source_skill=self.skill_name or "",
        )
        file_id = str(file_meta.get("file_id") or "")
        await self.emit_progress(
            100,
            "file_write",
            f"文件已保存：{name}",
            status_event="file",
            files=[{"file_id": file_id}],
            content_type="file",
            tool_name=self.skill_name or "skill",
            call_type="skill",
            status_details={
                "execution": {"file_id": file_id, "operation": "write"},
                "graph": {},
            },
        )
        record_trace_event(
            event_name="file.write",
            phase="running",
            status="success",
            component=self.skill_name or "skill",
            source="skill",
            tenant_id=self.tenant_id,
            account_id=self.account_id,
            bot_id=self.bot_id,
            chat_id=self.chat_id,
            request_msg_id=self.request_msg_id,
            trace_id=self.trace_id,
            details={"file_id": file_id, "operation": "write"},
        )
        return {
            "file_id": file_id,
            "name": str(file_meta.get("name") or name),
            "mime": str(file_meta.get("mime") or mime),
            "size": int(file_meta.get("size") or 0),
        }

    async def read_file(self, file_id: str) -> str | bytes:
        service = self._require_file_service()
        raw = await service.read(
            file_id=file_id,
            tenant_id=self.tenant_id,
            account_id=self.account_id,
            bot_id=self.bot_id,
        )
        metadata = await service.get(
            file_id=file_id,
            tenant_id=self.tenant_id,
            account_id=self.account_id,
            bot_id=self.bot_id,
            include_internal=True,
        )
        record_trace_event(
            event_name="file.read",
            phase="running",
            status="success",
            component=self.skill_name or "skill",
            source="skill",
            tenant_id=self.tenant_id,
            account_id=self.account_id,
            bot_id=self.bot_id,
            chat_id=self.chat_id,
            request_msg_id=self.request_msg_id,
            trace_id=self.trace_id,
            details={"file_id": file_id, "operation": "read"},
        )
        mime = str(metadata.get("mime") or "")
        if mime.startswith("text/") or mime in {"application/json", "application/xml"}:
            return raw.decode("utf-8", errors="replace")
        return raw

    async def get_file(self, file_id: str) -> dict[str, Any]:
        service = self._require_file_service()
        return await service.get(
            file_id=file_id,
            tenant_id=self.tenant_id,
            account_id=self.account_id,
            bot_id=self.bot_id,
            include_internal=False,
        )

    async def list_files(self, limit: int = 50) -> list[dict[str, Any]]:
        service = self._require_file_service()
        return await service.list(
            tenant_id=self.tenant_id,
            account_id=self.account_id,
            bot_id=self.bot_id,
            limit=limit,
        )


class BasePythonSkill:
    """Abstract base class for all Python-based skills."""

    name: str = ""

    async def execute(self, _context: SkillContext, _payload: dict[str, Any]) -> dict[str, Any]:
        """
        Execute the skill logic.

        Args:
            context: Execution context with identity and LLM access.
            payload: Skill-specific input data.

        Returns:
            Dict containing the execution result (status, data, message).
        """
        raise NotImplementedError("Skills must implement execute()")
