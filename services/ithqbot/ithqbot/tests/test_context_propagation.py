from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ithqbot import context
from ithqbot.agent.loop import AgentLoop
from ithqbot.agent.skills.base import SkillContext
from ithqbot.agent.tools.base import Tool
from ithqbot.agent.tools.registry import ToolRegistry
from ithqbot.bus.events import InboundMessage
from ithqbot.bus.queue import MessageBus
from ithqbot.providers.base import LLMProvider, LLMResponse


@pytest.fixture
def mock_provider():
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "gpt-4"
    provider.chat_with_retry = AsyncMock()
    provider.generation = AsyncMock()
    return provider

@pytest.fixture
def mock_bus():
    return MagicMock(spec=MessageBus)

@pytest.fixture
def temp_workspace(tmp_path):
    return tmp_path

@pytest.mark.asyncio
async def test_agent_loop_sets_context(mock_provider, mock_bus, temp_workspace):
    # Setup agent
    agent = AgentLoop(
        bus=mock_bus,
        provider=mock_provider,
        workspace=temp_workspace,
        model="gpt-4"
    )

    # Create inbound message with account_id and tenant_id
    msg = InboundMessage(
        channel="icatmsg",
        sender_id="user_123",
        chat_id="chat_456",
        content="hello",
        metadata={"tenant_id": "tenant_abc", "trace_id": "trace_001"}
    )

    results = {}

    async def mock_internal(m, s=None, o=None):
        results["account_id"] = context.account_id.get()
        results["tenant_id"] = context.tenant_id.get()
        results["channel"] = context.channel.get()
        results["chat_id"] = context.chat_id.get()
        results["trace_id"] = context.trace_id.get()
        return None

    # Patch _process_message_internal to capture context
    with patch.object(agent, "_process_message_internal", side_effect=mock_internal):
        await agent._process_message(msg)

    assert results["account_id"] == "user_123"
    assert results["tenant_id"] == "tenant_abc"
    assert results["channel"] == "icatmsg"
    assert results["chat_id"] == "chat_456"
    assert results["trace_id"] == "trace_001"

    # Verify context is cleared after processing
    assert context.account_id.get() is None
    assert context.tenant_id.get() is None
    assert context.channel.get() is None
    assert context.chat_id.get() is None

@pytest.mark.asyncio
async def test_minio_tool_uses_context(temp_workspace):
    from ithqbot.agent.tools.minio import MinioPushTool

    tool = MinioPushTool(
        endpoint="localhost:9000",
        access_key="key",
        secret_key="secret",
        bucket="bucket",
        workspace=temp_workspace
    )
    tokens = context.set_runtime_context(
        account="user_123",
        tenant="tenant_abc",
        bot=None,
        channel_name="icatmsg",
        chat="chat_456",
        metadata={"trace_id": "trace_001"},
    )

    try:
        # Create a dummy file
        test_file = temp_workspace / "test.txt"
        test_file.write_text("hello")

        # Mock minio client
        tool.client = MagicMock()

        # Mock fput_object as a standard sync method since we use asyncio.to_thread
        tool.client.fput_object = MagicMock()

        await tool.execute("test.txt")

        # Verify it used the account_id from context
        assert tool.client.fput_object.called
        args, kwargs = tool.client.fput_object.call_args
        rel_path = args[1]
        assert rel_path.startswith("user_123/chat_456/")

        # Verify outbound_media collected the url
        media_list = context.outbound_media.get()
        assert len(media_list) == 1
        attachment = media_list[0]
        assert attachment["kind"] == "file"
        assert attachment["storage"]["backend"] == "minio"
        assert attachment["storage"]["bucket"] == "bucket"
        assert attachment["storage"]["path"].startswith("user_123/chat_456/")
        assert attachment["rel_path"] == attachment["storage"]["path"]

    finally:
        context.reset_runtime_context(tokens)


@pytest.mark.asyncio
async def test_minio_push_tool_reconnects_after_failure(temp_workspace):
    from ithqbot.agent.tools.minio import MinioPushTool

    tool = MinioPushTool(
        endpoint="localhost:9000",
        access_key="key",
        secret_key="secret",
        bucket="bucket",
        workspace=temp_workspace
    )
    tokens = context.set_runtime_context(
        account="user_123",
        tenant="tenant_abc",
        bot=None,
        channel_name="icatmsg",
        chat="chat_456",
        metadata={"trace_id": "trace_001"},
    )
    test_file = temp_workspace / "retry.txt"
    test_file.write_text("hello")

    flaky_client = MagicMock()
    flaky_client.fput_object = MagicMock(side_effect=RuntimeError("minio disconnected"))
    healthy_client = MagicMock()
    healthy_client.fput_object = MagicMock()
    tool.client = flaky_client

    def _swap_client():
        tool.client = healthy_client

    tool._reconnect = _swap_client

    try:
        await tool.execute("retry.txt")
        assert healthy_client.fput_object.called
    finally:
        context.reset_runtime_context(tokens)

@pytest.mark.asyncio
async def test_codex_provider_uses_context():
    token_account = context.account_id.set("user_123")

    try:
        from ithqbot.providers.openai_codex_provider import _build_headers
        headers = _build_headers("bot_account", "some_token")

        assert headers["chatgpt-account-id"] == "user_123"
    finally:
        context.account_id.reset(token_account)

@pytest.mark.asyncio
async def test_codex_provider_fallback_to_bot_account():
    # Context is None by default
    from ithqbot.providers.openai_codex_provider import _build_headers
    headers = _build_headers("bot_account", "some_token")

    assert headers["chatgpt-account-id"] == "bot_account"


@pytest.mark.asyncio
async def test_skill_context_emit_interaction_passes_payload_to_callback():
    callback = AsyncMock()
    ctx = SkillContext(
        tenant_id="t1",
        account_id="u1",
        chat_id="c1",
        bot_id="b1",
        emit_callback=callback,
    )

    interaction = {
        "type": "select",
        "title": "环境选择",
        "prompt": "请选择环境",
        "options": [{"label": "prod", "value": "prod"}],
    }
    await ctx.emit_interaction(interaction)

    args, kwargs = callback.await_args
    assert args[0] == "请选择环境"
    assert kwargs["interaction"]["type"] == "select"
    assert kwargs["status_event"] == "interaction"


@pytest.mark.asyncio
async def test_skill_context_emit_progress_forwards_tracking_kwargs():
    callback = AsyncMock()
    ctx = SkillContext(
        tenant_id="t1",
        account_id="u1",
        chat_id="c1",
        bot_id="b1",
        emit_callback=callback,
        skill_name="check_skill",
    )

    await ctx.emit_progress(
        35,
        "skill_call",
        "正在执行技能检查",
        progress_stage="skill_call",
        call_type="skill",
        skill_name="check_skill",
        status_details={"execution": {"target_skill": "doc_compare"}},
    )

    args, kwargs = callback.await_args
    assert args[0] == 35
    assert args[1] == "skill_call"
    assert args[2] == "正在执行技能检查"
    assert kwargs["progress_stage"] == "skill_call"
    assert kwargs["call_type"] == "skill"
    assert kwargs["skill_name"] == "check_skill"
    assert kwargs["status_details"]["execution"]["target_skill"] == "doc_compare"


class _SourceAwareTool(Tool):
    def __init__(self, tool_name: str):
        self._name = tool_name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "test"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    async def execute(self, **kwargs):
        llm_ctx = context.get_llm_call_context()
        return f"{llm_ctx.get('source')}:{kwargs.get('value')}"


@pytest.mark.asyncio
async def test_tool_registry_sets_llm_source_and_audit_phase():
    import ithqbot.agent.tools.registry as registry_module

    events = []

    def _audit(**kwargs):
        events.append(kwargs)

    registry = ToolRegistry()
    registry.register(_SourceAwareTool("echo_tool"))
    with patch.object(registry_module, "audit_call", side_effect=_audit):
        result = await registry.execute("echo_tool", {"value": "ok"})
    assert result == "tool:ok"
    assert events[0]["phase"] == "start"
    assert events[-1]["phase"] == "end"


@pytest.mark.asyncio
async def test_tool_registry_marks_mcp_source():
    registry = ToolRegistry()
    registry.register(_SourceAwareTool("mcp_demo_weather"))
    result = await registry.execute("mcp_demo_weather", {"value": "ok"})
    assert result == "mcp:ok"


@pytest.mark.asyncio
async def test_tool_registry_marks_dynamic_skill_source():
    import ithqbot.agent.tools.registry as registry_module

    events = []

    class _DynamicSkillTool(_SourceAwareTool):
        pass

    _DynamicSkillTool.__module__ = "ithqbot.skills.dynamic.unit_skill"
    registry = ToolRegistry()
    registry.register(_DynamicSkillTool("skill_demo"))

    def _audit(**kwargs):
        events.append(kwargs)

    with patch.object(registry_module, "audit_call", side_effect=_audit):
        result = await registry.execute("skill_demo", {"value": "ok"})
    assert result == "skill:ok"
    assert events[0]["call_type"] == "skill"


@pytest.mark.asyncio
async def test_tool_registry_marks_builtin_skill_source():
    import ithqbot.agent.tools.registry as registry_module

    events = []

    class _BuiltinSkillTool(_SourceAwareTool):
        pass

    _BuiltinSkillTool.__module__ = "ithqbot.skills.doc_compare.tool.tool"
    registry = ToolRegistry()
    registry.register(_BuiltinSkillTool("doc_compare"))

    def _audit(**kwargs):
        events.append(kwargs)

    with patch.object(registry_module, "audit_call", side_effect=_audit):
        result = await registry.execute("doc_compare", {"value": "ok"})
    assert result == "skill:ok"
    assert events[0]["call_type"] == "skill"


@pytest.mark.asyncio
async def test_provider_records_llm_usage_with_source():
    import ithqbot.providers.base as provider_base

    captured = []

    def _record(**kwargs):
        captured.append(kwargs)

    class _Provider(LLMProvider):
        async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, reasoning_effort=None, tool_choice=None):
            return LLMResponse(content="ok", finish_reason="stop", usage={"prompt_tokens": 2, "completion_tokens": 3})

        def get_default_model(self) -> str:
            return "test-model"

    provider = _Provider()
    tokens = context.set_llm_call_context(source="skill", component="unit_skill")
    try:
        with patch.object(provider_base, "record_llm_usage", side_effect=_record):
            await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}])
    finally:
        context.reset_llm_call_context(tokens)
    assert captured
    assert captured[0]["source"] == "skill"
    assert captured[0]["usage"]["prompt_tokens"] == 2
