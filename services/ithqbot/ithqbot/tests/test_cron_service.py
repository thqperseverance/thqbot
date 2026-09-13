import asyncio
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ithqbot import context
from ithqbot.agent.skills.base import SkillContext
from ithqbot.agent.tools.cron import CronTool
from ithqbot.cron import service as cron_service_module
from ithqbot.cron.service import _CRON_STORE_FACTORIES, CronService, PostgreSQLCronStore
from ithqbot.cron.types import CronSchedule


def test_add_job_rejects_unknown_timezone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    with pytest.raises(ValueError, match="unknown timezone 'America/Vancovuer'"):
        service.add_job(
            name="tz typo",
            schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="America/Vancovuer"),
            message="hello",
        )

    assert service.list_jobs(include_disabled=True) == []


def test_add_job_accepts_valid_timezone(tmp_path) -> None:
    pytest.importorskip("croniter")
    service = CronService(tmp_path / "cron" / "jobs.json")

    job = service.add_job(
        name="tz ok",
        schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="America/Vancouver"),
        message="hello",
    )

    assert job.schedule.tz == "America/Vancouver"
    assert job.state.next_run_at_ms is not None


def test_compute_next_run_defaults_to_platform_timezone_for_cron(monkeypatch) -> None:
    pytest.importorskip("croniter")
    previous_tz = os.environ.get("TZ")
    monkeypatch.setenv("ITHQBOT_TIMEZONE", "Asia/Shanghai")
    os.environ["TZ"] = "UTC"
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        now = datetime(2026, 4, 19, 11, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        next_run_ms = cron_service_module._compute_next_run(
            CronSchedule(kind="cron", expr="32 23 * * *"),
            int(now.timestamp() * 1000),
        )
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        if hasattr(time, "tzset"):
            time.tzset()

    assert next_run_ms is not None
    next_run = datetime.fromtimestamp(next_run_ms / 1000, tz=ZoneInfo("Asia/Shanghai"))
    assert next_run.strftime("%Y-%m-%d %H:%M:%S") == "2026-04-19 23:32:00"


@pytest.mark.asyncio
async def test_running_service_honors_external_disable(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    called: list[str] = []

    async def on_job(job) -> None:
        called.append(job.id)

    service = CronService(store_path, on_job=on_job)
    job = service.add_job(
        name="external-disable",
        schedule=CronSchedule(kind="every", every_ms=200),
        message="hello",
    )
    await service.start()
    try:
        # Wait slightly to ensure file mtime is definitively different
        await asyncio.sleep(0.05)
        external = CronService(store_path)
        updated = external.enable_job(job.id, enabled=False)
        assert updated is not None
        assert updated.enabled is False

        await asyncio.sleep(0.35)
        assert called == []
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_cron_service_start_reschedules_recently_missed_one_time_jobs(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path)
    service.add_job(
        name="提醒喝茶",
        schedule=CronSchedule(kind="at", at_ms=int(time.time() * 1000) + 60_000),
        message="喝茶",
        delete_after_run=True,
    )
    jobs = service.list_jobs(include_disabled=True)
    assert len(jobs) == 1
    jobs[0].schedule.at_ms = int(time.time() * 1000) - 60_000
    jobs[0].state.next_run_at_ms = jobs[0].schedule.at_ms
    service._save_store()

    restarted = CronService(store_path)
    await restarted.start()
    try:
        all_jobs = restarted.list_jobs(include_disabled=True)
    finally:
        restarted.stop()

    assert len(all_jobs) == 1
    assert all_jobs[0].enabled is True
    assert all_jobs[0].state.next_run_at_ms is not None
    assert all_jobs[0].state.last_status is None


@pytest.mark.asyncio
async def test_cron_service_start_skips_old_missed_one_time_jobs(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path)
    service.add_job(
        name="提醒喝水",
        schedule=CronSchedule(kind="at", at_ms=int(time.time() * 1000) + 60_000),
        message="喝水",
        delete_after_run=True,
    )
    jobs = service.list_jobs(include_disabled=True)
    assert len(jobs) == 1
    jobs[0].schedule.at_ms = int(time.time() * 1000) - 10 * 60 * 1000
    jobs[0].state.next_run_at_ms = jobs[0].schedule.at_ms
    service._save_store()

    restarted = CronService(store_path)
    await restarted.start()
    try:
        assert restarted.list_jobs() == []
        all_jobs = restarted.list_jobs(include_disabled=True)
    finally:
        restarted.stop()

    assert len(all_jobs) == 1
    assert all_jobs[0].enabled is False
    assert all_jobs[0].state.last_status == "skipped"
    assert all_jobs[0].state.next_run_at_ms is None
    assert "Missed scheduled run" in (all_jobs[0].state.last_error or "")


@pytest.mark.asyncio
async def test_cron_tool_every_seconds_defaults_to_one_time_for_reminder(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service)
    tool.set_context("icatmsg", "u1")
    token = context.account_id.set("u1")
    try:
        result = await tool.execute(action="add", message="提醒喝茶", every_seconds=300)
    finally:
        context.account_id.reset(token)

    assert "Created job" in result
    assert "Active reminders:" in result
    jobs = service.list_jobs(include_disabled=True, owner_id="u1")
    assert len(jobs) == 1
    assert jobs[0].schedule.kind == "at"
    assert jobs[0].delete_after_run is True


@pytest.mark.asyncio
async def test_cron_tool_every_seconds_can_be_forced_recurring(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service)
    tool.set_context("icatmsg", "u2")
    token = context.account_id.set("u2")
    try:
        result = await tool.execute(action="add", message="提醒喝茶", every_seconds=300, one_time=False)
    finally:
        context.account_id.reset(token)

    assert "Created job" in result
    assert "Active reminders:" in result
    jobs = service.list_jobs(include_disabled=True, owner_id="u2")
    assert len(jobs) == 1
    assert jobs[0].schedule.kind == "every"
    assert jobs[0].schedule.every_ms == 300000


@pytest.mark.asyncio
async def test_cron_tool_every_seconds_defaults_to_one_time_without_reminder_keyword(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service)
    tool.set_context("icatmsg", "u3")
    token = context.account_id.set("u3")
    try:
        result = await tool.execute(action="add", message="看网关日志", every_seconds=300)
    finally:
        context.account_id.reset(token)

    assert "Created job" in result
    assert "Active reminders:" in result
    jobs = service.list_jobs(include_disabled=True, owner_id="u3")
    assert len(jobs) == 1
    assert jobs[0].schedule.kind == "at"
    assert jobs[0].delete_after_run is True


@pytest.mark.asyncio
async def test_cron_tool_writes_tenant_account_scope_into_metadata(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service)
    tool.set_context("icatmsg", "u4")
    account_token = context.account_id.set("u4")
    tenant_token = context.tenant_id.set("t1")
    try:
        result = await tool.execute(action="add", message="每周复盘", every_seconds=300, one_time=False)
    finally:
        context.tenant_id.reset(tenant_token)
        context.account_id.reset(account_token)

    assert "Created job" in result
    assert "Active reminders:" in result
    jobs = service.list_jobs(include_disabled=True, owner_id="u4")
    assert len(jobs) == 1
    assert jobs[0].payload.metadata["tenant_id"] == "t1"
    assert jobs[0].payload.metadata["account_id"] == "u4"


@pytest.mark.asyncio
async def test_cron_tool_writes_bot_scope_into_metadata(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service)
    tool.set_context("icatmsg", "u5")
    account_token = context.account_id.set("u5")
    bot_token = context.bot_id.set("bot-alpha")
    try:
        await tool.execute(action="add", message="每周巡检", every_seconds=300, one_time=False)
    finally:
        context.bot_id.reset(bot_token)
        context.account_id.reset(account_token)

    jobs = service.list_jobs(include_disabled=True, owner_id="u5", bot_id="bot-alpha")
    assert len(jobs) == 1
    assert jobs[0].payload.metadata["bot_id"] == "bot-alpha"


@pytest.mark.asyncio
async def test_cron_tool_list_and_remove_are_isolated_by_bot(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service)
    tool.set_context("icatmsg", "shared-chat")
    account_token = context.account_id.set("u6")
    try:
        bot_a_token = context.bot_id.set("bot-a")
        try:
            await tool.execute(action="add", message="A 机器人提醒", every_seconds=300, one_time=False)
        finally:
            context.bot_id.reset(bot_a_token)

        bot_b_token = context.bot_id.set("bot-b")
        try:
            await tool.execute(action="add", message="B 机器人提醒", every_seconds=300, one_time=False)
            listed_b = await tool.execute(action="list")
            job_id_a = service.list_jobs(include_disabled=True, owner_id="u6", bot_id="bot-a")[0].id
            removed_in_b = await tool.execute(action="remove", job_id=job_id_a)
        finally:
            context.bot_id.reset(bot_b_token)

        bot_a_token = context.bot_id.set("bot-a")
        try:
            listed_a = await tool.execute(action="list")
            job_id_a = service.list_jobs(include_disabled=True, owner_id="u6", bot_id="bot-a")[0].id
            removed_in_a = await tool.execute(action="remove", job_id=job_id_a)
        finally:
            context.bot_id.reset(bot_a_token)
    finally:
        context.account_id.reset(account_token)

    assert "A 机器人提醒" in listed_a
    assert "B 机器人提醒" not in listed_a
    assert "B 机器人提醒" in listed_b
    assert "A 机器人提醒" not in listed_b
    assert removed_in_b == f"Job {job_id_a} not found"
    assert removed_in_a == f"Removed job {job_id_a}"
    remaining_jobs = service.list_jobs(include_disabled=True, owner_id="u6")
    assert len(remaining_jobs) == 1
    assert remaining_jobs[0].payload.metadata["bot_id"] == "bot-b"


@pytest.mark.asyncio
async def test_cron_tool_list_formats_next_run_in_beijing_time(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service)
    tool.set_context("icatmsg", "u6")
    account_token = context.account_id.set("u6")
    try:
        await tool.execute(
            action="add",
            message="睡觉提醒",
            cron_expr="32 23 * * *",
            tz="Asia/Shanghai",
        )
        jobs = service.list_jobs(include_disabled=True, owner_id="u6")
        assert len(jobs) == 1
        jobs[0].state.next_run_at_ms = 1776612720000

        listed = await tool.execute(action="list")
    finally:
        context.account_id.reset(account_token)

    assert "2026-04-19 23:32:00 (北京时间)" in listed
    assert "15:32" not in listed


@pytest.mark.asyncio
async def test_cron_tool_list_and_remove_use_runtime_bot_scope(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service)
    tool.set_context("icatmsg", "shared-chat")
    tokens_bot_a = context.set_runtime_context(
        account="u7",
        tenant="tenant-shared",
        bot="bot-a",
        channel_name="icatmsg",
        chat="shared-chat",
        metadata={},
    )
    try:
        await tool.execute(action="add", message="A 机器人提醒", every_seconds=300, one_time=False)
        listed_a = await tool.execute(action="list")
        job_id_a = service.list_jobs(
            include_disabled=True,
            tenant_id="tenant-shared",
            owner_id="u7",
            bot_id="bot-a",
        )[0].id
    finally:
        context.reset_runtime_context(tokens_bot_a)

    tokens_bot_b = context.set_runtime_context(
        account="u7",
        tenant="tenant-shared",
        bot="bot-b",
        channel_name="icatmsg",
        chat="shared-chat",
        metadata={},
    )
    try:
        await tool.execute(action="add", message="B 机器人提醒", every_seconds=300, one_time=False)
        listed_b = await tool.execute(action="list")
        removed_in_b = await tool.execute(action="remove", job_id=job_id_a)
    finally:
        context.reset_runtime_context(tokens_bot_b)

    assert "A 机器人提醒" in listed_a
    assert "B 机器人提醒" not in listed_a
    assert "B 机器人提醒" in listed_b
    assert "A 机器人提醒" not in listed_b
    assert removed_in_b == f"Job {job_id_a} not found"


@pytest.mark.asyncio
async def test_cron_tool_uses_injected_skill_context_for_bot_scope(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service)
    tool.set_context("icatmsg", "shared-chat")

    context_a = SkillContext(
        tenant_id="tenant-shared",
        account_id="u7",
        chat_id="shared-chat",
        bot_id="bot-a",
        metadata={"channel": "icatmsg", "chat_id": "shared-chat"},
    )
    context_b = SkillContext(
        tenant_id="tenant-shared",
        account_id="u7",
        chat_id="shared-chat",
        bot_id="bot-b",
        metadata={"channel": "icatmsg", "chat_id": "shared-chat"},
    )

    await tool.execute(action="add", message="A 机器人提醒", every_seconds=300, one_time=False, context=context_a)
    listed_a = await tool.execute(action="list", context=context_a)

    await tool.execute(action="add", message="B 机器人提醒", every_seconds=300, one_time=False, context=context_b)
    listed_b = await tool.execute(action="list", context=context_b)

    assert "A 机器人提醒" in listed_a
    assert "B 机器人提醒" not in listed_a
    assert "B 机器人提醒" in listed_b
    assert "A 机器人提醒" not in listed_b

    jobs_a = service.list_jobs(
        include_disabled=True,
        tenant_id="tenant-shared",
        owner_id="u7",
        bot_id="bot-a",
    )
    jobs_b = service.list_jobs(
        include_disabled=True,
        tenant_id="tenant-shared",
        owner_id="u7",
        bot_id="bot-b",
    )
    assert len(jobs_a) == 1
    assert len(jobs_b) == 1


@pytest.mark.asyncio
async def test_cron_tool_uses_context_obj_kwarg_like_agent_loop(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service)
    tool.set_context("icatmsg", "shared-chat")

    context_a = SkillContext(
        tenant_id="tenant-shared",
        account_id="u70",
        chat_id="shared-chat",
        bot_id="bot-a",
        metadata={"channel": "icatmsg", "chat_id": "shared-chat"},
    )
    context_b = SkillContext(
        tenant_id="tenant-shared",
        account_id="u70",
        chat_id="shared-chat",
        bot_id="bot-b",
        metadata={"channel": "icatmsg", "chat_id": "shared-chat"},
    )

    await tool.execute(action="add", message="A 机器人提醒", every_seconds=300, one_time=False, context_obj=context_a)
    listed_a = await tool.execute(action="list", context_obj=context_a)

    await tool.execute(action="add", message="B 机器人提醒", every_seconds=300, one_time=False, context_obj=context_b)
    listed_b = await tool.execute(action="list", context_obj=context_b)

    assert "A 机器人提醒" in listed_a
    assert "B 机器人提醒" not in listed_a
    assert "B 机器人提醒" in listed_b
    assert "A 机器人提醒" not in listed_b


@pytest.mark.asyncio
async def test_cron_tool_falls_back_to_chat_id_bot_scope_when_context_missing_bot_id(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service)
    tool.set_context("icatmsg", "u9-bot-b-default")

    account_token = context.account_id.set("u9")
    tenant_token = context.tenant_id.set("tenant-shared")
    try:
        service.add_job(
            name="tenant-a-bot-a",
            schedule=CronSchedule(kind="every", every_ms=300_000),
            message="A 机器人提醒",
            owner_id="u9",
            metadata={"tenant_id": "tenant-shared", "account_id": "u9", "bot_id": "bot-a"},
        )
        service.add_job(
            name="tenant-a-bot-b",
            schedule=CronSchedule(kind="every", every_ms=300_000),
            message="B 机器人提醒",
            owner_id="u9",
            metadata={"tenant_id": "tenant-shared", "account_id": "u9", "bot_id": "bot-b"},
        )

        listed = await tool.execute(action="list")
    finally:
        context.tenant_id.reset(tenant_token)
        context.account_id.reset(account_token)

    assert "tenant-a-bot-b" in listed
    assert "tenant-a-bot-a" not in listed


@pytest.mark.asyncio
async def test_cron_tool_list_and_remove_are_isolated_by_tenant(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service)
    tool.set_context("icatmsg", "shared-chat")
    account_token = context.account_id.set("u8")
    bot_token = context.bot_id.set("bot-shared")
    try:
        tenant_a_token = context.tenant_id.set("tenant-a")
        try:
            await tool.execute(action="add", message="租户 A 提醒", every_seconds=300, one_time=False)
        finally:
            context.tenant_id.reset(tenant_a_token)

        tenant_b_token = context.tenant_id.set("tenant-b")
        try:
            await tool.execute(action="add", message="租户 B 提醒", every_seconds=300, one_time=False)
            listed_b = await tool.execute(action="list")
            job_id_a = service.list_jobs(
                include_disabled=True,
                tenant_id="tenant-a",
                owner_id="u8",
                bot_id="bot-shared",
            )[0].id
            removed_in_b = await tool.execute(action="remove", job_id=job_id_a)
        finally:
            context.tenant_id.reset(tenant_b_token)

        tenant_a_token = context.tenant_id.set("tenant-a")
        try:
            listed_a = await tool.execute(action="list")
            job_id_a = service.list_jobs(
                include_disabled=True,
                tenant_id="tenant-a",
                owner_id="u8",
                bot_id="bot-shared",
            )[0].id
            removed_in_a = await tool.execute(action="remove", job_id=job_id_a)
        finally:
            context.tenant_id.reset(tenant_a_token)
    finally:
        context.bot_id.reset(bot_token)
        context.account_id.reset(account_token)

    assert "租户 A 提醒" in listed_a
    assert "租户 B 提醒" not in listed_a
    assert "租户 B 提醒" in listed_b
    assert "租户 A 提醒" not in listed_b
    assert removed_in_b == f"Job {job_id_a} not found"
    assert removed_in_a == f"Removed job {job_id_a}"
    remaining_jobs = service.list_jobs(
        include_disabled=True,
        tenant_id="tenant-b",
        owner_id="u8",
        bot_id="bot-shared",
    )
    assert len(remaining_jobs) == 1
    assert remaining_jobs[0].payload.metadata["tenant_id"] == "tenant-b"


def test_cron_service_list_and_remove_are_isolated_by_tenant_owner_and_bot(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    schedule = CronSchedule(kind="every", every_ms=300_000)
    job_a = service.add_job(
        name="tenant-a-bot-a",
        schedule=schedule,
        message="提醒 A",
        owner_id="shared-user",
        metadata={"tenant_id": "tenant-a", "account_id": "shared-user", "bot_id": "bot-a"},
    )
    service.add_job(
        name="tenant-b-bot-a",
        schedule=schedule,
        message="提醒 B",
        owner_id="shared-user",
        metadata={"tenant_id": "tenant-b", "account_id": "shared-user", "bot_id": "bot-a"},
    )
    service.add_job(
        name="tenant-a-bot-b",
        schedule=schedule,
        message="提醒 C",
        owner_id="shared-user",
        metadata={"tenant_id": "tenant-a", "account_id": "shared-user", "bot_id": "bot-b"},
    )

    jobs = service.list_jobs(tenant_id="tenant-a", owner_id="shared-user", bot_id="bot-a")
    assert len(jobs) == 1
    assert jobs[0].id == job_a.id

    removed_wrong_tenant = service.remove_job(
        job_a.id,
        tenant_id="tenant-b",
        owner_id="shared-user",
        bot_id="bot-a",
    )
    assert removed_wrong_tenant is False

    removed_wrong_bot = service.remove_job(
        job_a.id,
        tenant_id="tenant-a",
        owner_id="shared-user",
        bot_id="bot-b",
    )
    assert removed_wrong_bot is False

    removed_exact = service.remove_job(
        job_a.id,
        tenant_id="tenant-a",
        owner_id="shared-user",
        bot_id="bot-a",
    )
    assert removed_exact is True
    assert len(service.list_jobs(include_disabled=True)) == 2


@pytest.mark.asyncio
async def test_cron_tool_list_returns_active_reminders_with_next_run(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service)
    tool.set_context("icatmsg", "u7")
    token = context.account_id.set("u7")
    try:
        await tool.execute(action="add", message="每周复盘", every_seconds=300, one_time=False)
        listed = await tool.execute(action="list")
    finally:
        context.account_id.reset(token)

    assert "Active reminders:" in listed
    assert "schedule: every" in listed
    assert "status: pending" in listed
    assert "next:" in listed


@pytest.mark.asyncio
async def test_cron_tool_list_marks_due_jobs(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service)
    tool.set_context("icatmsg", "u10")
    token = context.account_id.set("u10")
    try:
        await tool.execute(action="add", message="每周复盘", every_seconds=300, one_time=False)
        jobs = service.list_jobs(include_disabled=True, owner_id="u10")
        assert len(jobs) == 1
        jobs[0].state.next_run_at_ms = int(time.time() * 1000) - 1_000
        listed = await tool.execute(action="list")
    finally:
        context.account_id.reset(token)

    assert "status: due" in listed


def test_cron_service_supports_postgresql_plus_driver_scheme(tmp_path, monkeypatch) -> None:
    class FakePGStore:
        def __init__(self, uri: str):
            self.uri = uri

        def load_store(self):
            from ithqbot.cron.types import CronStore
            return CronStore()

        def save_store(self, _store):
            return None

    monkeypatch.setitem(_CRON_STORE_FACTORIES, "postgresql", lambda uri: FakePGStore(uri))
    service = CronService(
        tmp_path / "cron" / "jobs.json",
        store_uri="postgresql+psycopg://user:pass@localhost:5432/ithqbot",
    )
    assert service._external_store is not None


def test_cron_service_rejects_unsupported_scheme_even_without_file_fallback(tmp_path) -> None:
    with pytest.raises(ValueError, match="Unsupported cron store URI scheme"):
        CronService(
            tmp_path / "cron" / "jobs.json",
            store_uri="mongodb://localhost:27017/ithqbot",
        )


def test_cron_service_requires_external_uri_when_enabled() -> None:
    with pytest.raises(ValueError, match="requires external store"):
        CronService(
            None,
            require_external_store=True,
        )


def test_cron_service_without_file_store_raises_for_local_fallback() -> None:
    service = CronService(None)
    with pytest.raises(ValueError, match="file store is disabled"):
        service.list_jobs()


def test_postgresql_cron_store_wraps_connection_error_with_masked_uri(monkeypatch) -> None:
    def _boom(_self, _uri):
        raise Exception("auth failed")

    monkeypatch.setattr(PostgreSQLCronStore, "_connect", _boom)
    with pytest.raises(
        RuntimeError,
        match="cronStoreUri=postgresql://ithqbot:\\*\\*\\*@127.0.0.1:5432/ithqbot",
    ):
        PostgreSQLCronStore("postgresql://ithqbot:secret@127.0.0.1:5432/ithqbot")


def test_postgresql_cron_store_creates_scope_indexes_for_events(monkeypatch) -> None:
    class _Cursor:
        def __init__(self, queries: list[str]) -> None:
            self._queries = queries

        def execute(self, query: str, _params=None) -> None:
            self._queries.append(query)

        def close(self) -> None:
            return None

    class _Conn:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def cursor(self):
            return _Cursor(self.queries)

        def close(self) -> None:
            return None

    conn = _Conn()

    def _fake_connect(_self, _uri):
        return conn

    monkeypatch.setattr(PostgreSQLCronStore, "_connect", _fake_connect)
    PostgreSQLCronStore("postgresql://user:pass@127.0.0.1:5432/ithqbot")

    ddl = "\n".join(conn.queries)
    assert "idx_ithqbot_cron_events_scope_status_run" in ddl
    assert "idx_ithqbot_cron_events_scope_job" in ddl
