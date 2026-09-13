"""Cron tool for scheduling reminders and tasks."""

from contextvars import ContextVar
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ithqbot import context
from ithqbot.agent.tools.base import Tool
from ithqbot.cron.service import CronService
from ithqbot.cron.types import CronSchedule
from ithqbot.utils.helpers import default_timezone, default_timezone_name


class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    def __init__(self, cron_service: CronService):
        self._cron = cron_service
        self._channel = ""
        self._chat_id = ""
        self._metadata: dict[str, Any] = {}
        self._in_cron_context: ContextVar[bool] = ContextVar("cron_in_context", default=False)

    def set_context(self, channel: str, chat_id: str, metadata: dict[str, Any] | None = None) -> None:
        """Set the current session context for delivery."""
        self._channel = channel
        self._chat_id = chat_id
        self._metadata = metadata or {}

    def set_cron_context(self, active: bool):
        """Mark whether the tool is executing inside a cron job callback."""
        return self._in_cron_context.set(active)

    def reset_cron_context(self, token) -> None:
        """Restore previous cron context."""
        self._in_cron_context.reset(token)

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return "MUST USE this tool to add, list, or remove any reminders or scheduled tasks. NEVER confirm a reminder without successfully calling this tool first."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove"],
                    "description": "Action to perform",
                },
                "message": {"type": "string", "description": "Reminder message (for add)"},
                "every_seconds": {
                    "type": "integer",
                    "description": "Delay/interval in seconds. Use one_time=true for one-shot delay.",
                },
                "one_time": {
                    "type": "boolean",
                    "description": "If true with every_seconds, run once after delay and auto-delete.",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression like '0 9 * * *' (for scheduled tasks)",
                },
                "tz": {
                    "type": "string",
                    "description": "IANA timezone for cron expressions (e.g. 'America/Vancouver')",
                },
                "at": {
                    "type": "string",
                    "description": "ISO datetime for one-time execution (e.g. '2026-02-12T10:30:00')",
                },
                "job_id": {"type": "string", "description": "Job ID (for remove)"},
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        message: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        tz: str | None = None,
        at: str | None = None,
        job_id: str | None = None,
        one_time: bool | None = None,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> str:
        self.throw_if_cancelled(cancellation_token)
        context_obj = kwargs.get("context_obj")
        if context_obj is None:
            context_obj = kwargs.get("context")
        if action == "add":
            if self._in_cron_context.get():
                return "错误：在定时任务执行过程中，不能再次创建新任务。"
            if one_time is None and isinstance(kwargs.get("once"), bool):
                one_time = kwargs.get("once")
            return self._add_job(message, every_seconds, cron_expr, tz, at, one_time=one_time, context_obj=context_obj)
        elif action == "list":
            return self._list_jobs(context_obj=context_obj)
        elif action == "remove":
            return self._remove_job(job_id, context_obj=context_obj)
        return f"Unknown action: {action}"

    def _add_job(
        self,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        tz: str | None,
        at: str | None,
        one_time: bool | None = None,
        context_obj: Any | None = None,
    ) -> str:
        if not message:
            return "错误：新增任务时 message 为必填。"
        runtime_ctx = context.get_runtime_context()
        channel = runtime_ctx.get("channel") or self._channel
        chat_id = runtime_ctx.get("chat_id") or self._chat_id
        metadata = self._resolve_metadata(runtime_ctx, context_obj)
        if not channel or not chat_id:
            return "错误：缺少会话上下文（channel/chat_id）。"
        if tz and not (cron_expr or at):
            return "错误：tz 仅可与 cron_expr 或 at 一起使用。"
        if tz:
            from zoneinfo import ZoneInfo

            try:
                ZoneInfo(tz)
            except (KeyError, Exception):
                return f"错误：未知时区“{tz}”。"

        # Build schedule
        delete_after = False
        if every_seconds:
            inferred_one_time = one_time if one_time is not None else self._infer_one_time_every(message)
            if inferred_one_time:
                at_ms = int(time.time() * 1000) + every_seconds * 1000
                schedule = CronSchedule(kind="at", at_ms=at_ms)
                delete_after = True
            else:
                schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
        elif at:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            try:
                dt = datetime.fromisoformat(at)
                if dt.tzinfo is None:
                    # Default to the configured platform timezone for naive datetimes.
                    target_tz = tz or default_timezone_name()
                    try:
                        dt = dt.replace(tzinfo=ZoneInfo(target_tz))
                    except Exception:
                        # Fallback if timezone not found
                        dt = dt.replace(tzinfo=default_timezone())
            except ValueError:
                return f"错误：无效的 ISO 时间格式“{at}”，期望格式：YYYY-MM-DDTHH:MM:SS。"
            at_ms = int(dt.timestamp() * 1000)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        else:
            return "错误：every_seconds、cron_expr、at 三者至少提供一个。"

        owner_id, tenant_id, bot_id = self._resolve_scope(runtime_ctx, context_obj, fallback_chat_id=chat_id)
        if owner_id and "account_id" not in metadata:
            metadata["account_id"] = owner_id
        if tenant_id and "tenant_id" not in metadata:
            metadata["tenant_id"] = tenant_id
        if bot_id and "bot_id" not in metadata:
            metadata["bot_id"] = bot_id

        job = self._cron.add_job(
            name=message[:30],
            schedule=schedule,
            message=message,
            deliver=True,
            channel=channel,
            to=chat_id,
            owner_id=owner_id,
            metadata=metadata,
            delete_after_run=delete_after,
        )
        owner_jobs = self._cron.list_jobs(tenant_id=tenant_id, owner_id=owner_id, bot_id=bot_id)
        return (
            f"Created job '{job.name}' (id: {job.id})\n"
            f"{self._format_jobs(owner_jobs, title='Active reminders')}"
        )

    @staticmethod
    def _infer_one_time_every(message: str) -> bool:
        lowered = message.lower()
        recurring_markers = (
            "每", "每天", "每周", "每月", "每年", "每隔", "循环", "定时",
            "every", "daily", "weekly", "monthly",
        )
        if any(token in lowered for token in recurring_markers):
            return False
        return True

    def _list_jobs(self, context_obj: Any | None = None) -> str:
        owner_id, tenant_id, bot_id = self._resolve_scope(context_obj=context_obj, fallback_chat_id=self._chat_id)
        jobs = self._cron.list_jobs(tenant_id=tenant_id, owner_id=owner_id, bot_id=bot_id)
        return self._format_jobs(jobs, title="Active reminders")

    @staticmethod
    def _format_jobs(jobs: list[Any], title: str) -> str:
        if not jobs:
            return "No active reminders."
        lines = []
        now_ms = int(time.time() * 1000)
        for j in jobs:
            next_run = j.state.next_run_at_ms
            next_run_text = "-"
            if isinstance(next_run, int) and next_run > 0:
                next_run_text = CronTool._format_run_at(next_run, getattr(j.schedule, "tz", None))
            status = CronTool._job_status_text(j, now_ms)
            lines.append(
                f"- {j.name} (id: {j.id}, schedule: {j.schedule.kind}, status: {status}, next: {next_run_text})"
            )
        return f"{title}:\n" + "\n".join(lines)

    @staticmethod
    def _job_status_text(job: Any, now_ms: int) -> str:
        if not getattr(job, "enabled", True):
            if job.schedule.kind == "at" and getattr(job.state, "last_run_at_ms", None) is None:
                return "expired"
            return "disabled"
        if getattr(job.state, "last_status", None) == "error":
            return "failed"
        next_run = getattr(job.state, "next_run_at_ms", None)
        if isinstance(next_run, int) and next_run > 0 and next_run <= now_ms:
            return "due"
        return "pending"

    @staticmethod
    def _format_run_at(next_run_ms: int, tz_name: str | None) -> str:
        display_tz = tz_name or default_timezone_name()
        try:
            tz = ZoneInfo(display_tz)
        except Exception:
            tz = default_timezone()
            display_tz = getattr(tz, "key", "Asia/Shanghai")
        dt = datetime.fromtimestamp(next_run_ms / 1000, tz=tz)
        label = "北京时间" if display_tz == "Asia/Shanghai" else display_tz
        return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} ({label})"

    def _remove_job(self, job_id: str | None, context_obj: Any | None = None) -> str:
        if not job_id:
            return "错误：移除任务时 job_id 为必填。"

        owner_id, tenant_id, bot_id = self._resolve_scope(context_obj=context_obj, fallback_chat_id=self._chat_id)
        if self._cron.remove_job(job_id, tenant_id=tenant_id, owner_id=owner_id, bot_id=bot_id):
            return f"Removed job {job_id}"
        return f"Job {job_id} not found"

    def _resolve_scope(
        self,
        runtime_ctx: dict[str, Any] | None = None,
        context_obj: Any | None = None,
        fallback_chat_id: str | None = None,
    ) -> tuple[str | None, str | None, str | None]:
        ctx = runtime_ctx or context.get_runtime_context()
        metadata = ctx.get("metadata") if isinstance(ctx.get("metadata"), dict) else {}
        scoped_metadata = getattr(context_obj, "metadata", None)
        owner_id = (
            getattr(context_obj, "account_id", None)
            or ctx.get("account_id")
            or context.account_id.get()
            or (scoped_metadata.get("account_id") if isinstance(scoped_metadata, dict) else None)
            or metadata.get("account_id")
        )
        tenant_id = (
            getattr(context_obj, "tenant_id", None)
            or ctx.get("tenant_id")
            or context.tenant_id.get()
            or (scoped_metadata.get("tenant_id") if isinstance(scoped_metadata, dict) else None)
            or metadata.get("tenant_id")
        )
        bot_id = (
            getattr(context_obj, "bot_id", None)
            or ctx.get("bot_id")
            or context.bot_id.get()
            or (scoped_metadata.get("bot_id") if isinstance(scoped_metadata, dict) else None)
            or metadata.get("bot_id")
            or CronTool._infer_bot_id_from_chat_id(
                owner_id,
                str(ctx.get("chat_id") or metadata.get("chat_id") or fallback_chat_id or ""),
            )
        )
        return owner_id, tenant_id, bot_id

    def _resolve_metadata(self, runtime_ctx: dict[str, Any], context_obj: Any | None = None) -> dict[str, Any]:
        metadata = dict((runtime_ctx.get("metadata") or self._metadata) or {})
        scoped_metadata = getattr(context_obj, "metadata", None)
        if isinstance(scoped_metadata, dict):
            for key, value in scoped_metadata.items():
                metadata.setdefault(key, value)
        return metadata

    @staticmethod
    def _infer_bot_id_from_chat_id(owner_id: str | None, chat_id: str) -> str | None:
        if not owner_id or not chat_id:
            return None
        prefix = f"{owner_id}-"
        suffix = "-default"
        if not chat_id.startswith(prefix) or not chat_id.endswith(suffix):
            return None
        inferred = chat_id[len(prefix):-len(suffix)]
        inferred = inferred.strip("- ").strip()
        return inferred or None
