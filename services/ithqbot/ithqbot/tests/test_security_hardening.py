from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from ithqbot.bus.events import InboundMessage, OutboundMessage
from ithqbot.config.schema import Config
from ithqbot.graph.condition import eval_condition


def _make_loop():
    from ithqbot.agent.loop import AgentLoop
    from ithqbot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    workspace = MagicMock()
    workspace.__truediv__ = MagicMock(return_value=MagicMock())

    with patch("ithqbot.agent.loop.ContextBuilder"), patch("ithqbot.agent.loop.SessionManager"):
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace)
    return loop


def test_eval_condition_supports_safe_state_access() -> None:
    assert eval_condition(
        "state.approved == true and state['count'] >= 2 and not state.blocked",
        {"approved": True, "count": 2, "blocked": False},
    ) is True


def test_eval_condition_blocks_code_execution_gadgets() -> None:
    assert eval_condition("__import__('os').system('echo hacked') == 0", {}) is False
    assert eval_condition("state.__class__.__mro__", {}) is False


@pytest.mark.asyncio
async def test_dispatch_cleans_up_idle_session_lock() -> None:
    loop = _make_loop()
    msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="hello")
    loop._process_message = MagicMock(  # type: ignore[method-assign]
        return_value=asyncio.Future()
    )
    loop._process_message.return_value.set_result(  # type: ignore[attr-defined]
        OutboundMessage(channel="test", chat_id="c1", content="hi")
    )

    await loop._dispatch(msg)

    assert msg.session_key not in loop._session_locks


def test_default_config_does_not_embed_minio_credentials() -> None:
    config = Config()

    assert config.tools.minio.access_key == ""
    assert config.tools.minio.secret_key == ""


@pytest.mark.asyncio
async def test_summarize_tool_kills_subprocess_on_timeout(monkeypatch) -> None:
    from ithqbot.skills.summarize.tool import tool as summarize_module

    class _FakeProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.killed = False
            self.waited = False

        async def communicate(self):
            await asyncio.sleep(3600)
            return b"", b""

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            self.waited = True
            return -9

    process = _FakeProcess()

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(summarize_module.shutil, "which", lambda _name: "/usr/bin/summarize")
    monkeypatch.setattr(summarize_module.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(summarize_module, "_SUMMARIZE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(summarize_module, "_PROCESS_KILL_WAIT_SECONDS", 0.01)

    tool = summarize_module.SummarizeTool()
    result = await tool.execute(target="https://example.com")

    assert "总结超时" in result
    assert process.killed is True
    assert process.waited is True


@pytest.mark.asyncio
async def test_observability_pg_purge_uses_parameterized_interval() -> None:
    from ithqbot.observability import ObservabilityPersistence

    calls: list[tuple[str, tuple[int, ...]]] = []

    class _FakeCursor:
        rowcount = 3

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, query, params):
            calls.append((query, params))

    class _FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return _FakeCursor()

    store = ObservabilityPersistence("postgresql://example")
    store._enabled = True
    store._get_conn = lambda: _fake_get_conn()  # type: ignore[method-assign]

    async def _fake_get_conn():
        return _FakeConn()

    deleted = await store.purge_old_data(7)

    assert deleted == 3
    assert calls == [(
        "DELETE FROM observability_runs WHERE started_at < NOW() - make_interval(days => %s)",
        (7,),
    )]


def test_ai_news_script_skips_send_when_webhook_missing(monkeypatch) -> None:
    from ithqbot.skills.ai_new_day_report import fetch_ai_news

    called = False

    def _fake_post(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("requests.post should not be called without webhook")

    monkeypatch.setattr(fetch_ai_news.requests, "post", _fake_post)

    fetch_ai_news.send_to_wechat("", "hello")

    assert called is False
