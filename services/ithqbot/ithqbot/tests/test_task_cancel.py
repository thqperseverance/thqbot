"""Tests for /stop task cancellation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_loop():
    """Create a minimal AgentLoop with mocked dependencies."""
    from ithqbot.agent.loop import AgentLoop
    from ithqbot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    workspace = MagicMock()
    workspace.__truediv__ = MagicMock(return_value=MagicMock())

    with patch("ithqbot.agent.loop.ContextBuilder"), \
         patch("ithqbot.agent.loop.SessionManager"), \
         patch("ithqbot.agent.loop.FileService"), \
         patch("ithqbot.agent.loop.FileServiceConfig"):
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace, tool_registrars=[])
    return loop, bus


class TestHandleStop:
    @pytest.mark.asyncio
    async def test_stop_no_active_task(self):
        from ithqbot.bus.events import InboundMessage

        loop, bus = _make_loop()
        msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="/stop")
        await loop._handle_stop(msg)
        out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert "No active task" in out.content

    @pytest.mark.asyncio
    async def test_stop_cancels_active_task(self):
        from ithqbot.bus.events import InboundMessage

        loop, bus = _make_loop()
        cancelled = asyncio.Event()

        async def slow_task():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(slow_task())
        await asyncio.sleep(0)
        loop._active_tasks["test:c1"] = [task]

        msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="/stop")
        await loop._handle_stop(msg)

        assert cancelled.is_set()
        out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert "stopped" in out.content.lower()

    @pytest.mark.asyncio
    async def test_stop_cancels_multiple_tasks(self):
        from ithqbot.bus.events import InboundMessage

        loop, bus = _make_loop()
        events = [asyncio.Event(), asyncio.Event()]

        async def slow(idx):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                events[idx].set()
                raise

        tasks = [asyncio.create_task(slow(i)) for i in range(2)]
        await asyncio.sleep(0)
        loop._active_tasks["test:c1"] = tasks

        msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="/stop")
        await loop._handle_stop(msg)

        assert all(e.is_set() for e in events)
        out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert "2 task" in out.content


class TestDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_processes_and_publishes(self):
        from ithqbot.bus.events import InboundMessage, OutboundMessage

        loop, bus = _make_loop()
        msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="hello")
        loop._process_message = AsyncMock(
            return_value=OutboundMessage(channel="test", chat_id="c1", content="hi")
        )
        await loop._dispatch(msg)
        out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert out.content == "hi"

    @pytest.mark.asyncio
    async def test_session_lock_serializes_same_session(self):
        from ithqbot.bus.events import InboundMessage, OutboundMessage

        loop, bus = _make_loop()
        order = []

        async def mock_process(m, **kwargs):
            order.append(f"start-{m.content}")
            await asyncio.sleep(0.05)
            order.append(f"end-{m.content}")
            return OutboundMessage(channel="test", chat_id="c1", content=m.content)

        loop._process_message = mock_process
        msg1 = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="a")
        msg2 = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="b")

        t1 = asyncio.create_task(loop._dispatch(msg1))
        t2 = asyncio.create_task(loop._dispatch(msg2))
        await asyncio.gather(t1, t2)
        assert order == ["start-a", "end-a", "start-b", "end-b"]

    @pytest.mark.asyncio
    async def test_session_lock_allows_parallel_different_sessions(self):
        from ithqbot.bus.events import InboundMessage, OutboundMessage

        loop, bus = _make_loop()
        started: list[str] = []
        gate = asyncio.Event()

        async def mock_process(m, **kwargs):
            started.append(m.content)
            await gate.wait()
            return OutboundMessage(channel="test", chat_id=m.chat_id, content=m.content)

        loop._process_message = mock_process
        msg1 = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="a")
        msg2 = InboundMessage(channel="test", sender_id="u1", chat_id="c2", content="b")

        t1 = asyncio.create_task(loop._dispatch(msg1))
        t2 = asyncio.create_task(loop._dispatch(msg2))
        await asyncio.sleep(0.02)
        assert sorted(started) == ["a", "b"]
        gate.set()
        await asyncio.gather(t1, t2)

    @pytest.mark.asyncio
    async def test_dispatch_exception_keeps_route_metadata(self):
        from ithqbot.bus.events import InboundMessage

        loop, bus = _make_loop()
        msg = InboundMessage(
            channel="icatmsg",
            sender_id="u1",
            chat_id="c1",
            content="hello",
            account_id="user_B",
            tenant_id="tenant_X",
            bot_id="bot_A",
            metadata={"source_channel": "icatmsg", "client_id": "web_x"},
        )

        loop._process_message = AsyncMock(side_effect=RuntimeError("boom"))
        await loop._dispatch(msg)
        out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert out.content == "处理请求时出现异常，请稍后重试。"
        assert out.account_id == "user_B"
        assert out.tenant_id == "tenant_X"
        assert out.bot_id == "bot_A"
        assert out.metadata.get("client_id") == "web_x"


class TestRepairMessages:
    def test_repair_messages_for_retry_strips_tool_turns_and_keeps_recent_dialogue(self):
        from ithqbot.agent.loop import AgentLoop

        repaired = AgentLoop._repair_messages_for_retry([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "a", "name": "x", "content": "ok"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ])

        assert repaired == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ]
