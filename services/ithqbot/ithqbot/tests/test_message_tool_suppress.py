"""Test message tool suppress logic for final replies."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import ithqbot.agent.loop as loop_module
from ithqbot.agent.loop import AgentLoop
from ithqbot.agent.tools.base import Tool
from ithqbot.agent.tools.message import MessageTool
from ithqbot.bus.events import InboundMessage, OutboundMessage
from ithqbot.bus.queue import MessageBus
from ithqbot.config.schema import BotGuardrailsConfig, BotGuardrailsPolicy, ToolsConfig
from ithqbot.providers.base import LLMResponse, ToolCallRequest


def _make_loop(tmp_path: Path) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")


def test_graph_state_manager_requires_postgresql_when_external_persistence_is_enforced(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        session_manager=MagicMock(),
        session_store_uri="redis://cache:6379/0",
        require_external_session_store=True,
    )

    with pytest.raises(RuntimeError, match="Graph runtime requires a PostgreSQL-backed"):
        loop._get_graph_state_manager()


class TestMessageToolSuppressLogic:
    """Final reply is suppressed only when it is empty or duplicates message-tool output."""

    @pytest.mark.asyncio
    async def test_not_suppress_when_message_tool_sent_but_final_is_new(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(
            id="call1", name="message",
            arguments={"content": "Hello", "channel": "feishu", "chat_id": "chat123"},
        )
        calls = iter([
            LLMResponse(content="", tool_calls=[tool_call]),
            LLMResponse(content="Done", tool_calls=[]),
        ])
        loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])

        sent: list[OutboundMessage] = []
        mt = loop.tools.get("message")
        if isinstance(mt, MessageTool):
            mt.set_send_callback(AsyncMock(side_effect=lambda m: sent.append(m)))

        msg = InboundMessage(channel="feishu", sender_id="user1", chat_id="chat123", content="Send")
        result = await loop._process_message(msg)

        assert len(sent) == 1
        assert result is not None
        assert result.content == "Done"

    @pytest.mark.asyncio
    async def test_not_suppress_when_sent_to_different_target(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(
            id="call1", name="message",
            arguments={"content": "Email content", "channel": "email", "chat_id": "user@example.com"},
        )
        calls = iter([
            LLMResponse(content="", tool_calls=[tool_call]),
            LLMResponse(content="I've sent the email.", tool_calls=[]),
        ])
        loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])

        sent: list[OutboundMessage] = []
        mt = loop.tools.get("message")
        if isinstance(mt, MessageTool):
            mt.set_send_callback(AsyncMock(side_effect=lambda m: sent.append(m)))

        msg = InboundMessage(channel="feishu", sender_id="user1", chat_id="chat123", content="Send email")
        result = await loop._process_message(msg)

        assert len(sent) == 1
        assert sent[0].channel == "email"
        assert result is not None  # not suppressed
        assert result.channel == "feishu"

    @pytest.mark.asyncio
    async def test_not_suppress_when_no_message_tool_used(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="Hello!", tool_calls=[]))
        loop.tools.get_definitions = MagicMock(return_value=[])

        msg = InboundMessage(channel="feishu", sender_id="user1", chat_id="chat123", content="Hi")
        result = await loop._process_message(msg)

        assert result is not None
        assert "Hello" in result.content

    @pytest.mark.asyncio
    async def test_pure_file_upload_does_not_trigger_llm_or_auto_reply(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="不应触发", tool_calls=[]))
        loop.tools.get_definitions = MagicMock(return_value=[])
        msg = InboundMessage(
            channel="feishu",
            sender_id="user1",
            chat_id="chat123",
            content="[file: 1.docx]",
            metadata={
                "attachments": [
                    {"storage": {"bucket": "ithqbot-storage", "path": "feishu_attachments/a/1.docx"}},
                    {"storage": {"bucket": "ithqbot-storage", "path": "feishu_attachments/a/2.docx"}},
                ]
            },
        )
        result = await loop._process_message(msg)

        assert result is None
        loop.provider.chat_with_retry.assert_not_awaited()
        session = loop.sessions.get_or_create("feishu:chat123")
        assert session.messages
        assert session.messages[-1]["role"] == "user"

    @pytest.mark.asyncio
    async def test_official_doc_review_request_after_upload_routes_directly_with_recent_attachment(
        self,
        tmp_path: Path,
    ) -> None:
        class OfficialDocReviewTool(Tool):
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            @property
            def name(self) -> str:
                return "official_doc_review"

            @property
            def description(self) -> str:
                return "review official document quality"

            @property
            def parameters(self) -> dict[str, object]:
                return {
                    "type": "object",
                    "properties": {
                        "storage_uri": {"type": "string"},
                        "file_id": {"type": "string"},
                    },
                }

            @property
            def direct_handler(self):
                from ithqbot.skills.official_doc_review.direct_handler import handle_direct_official_doc_review

                return handle_direct_official_doc_review

            async def execute(self, **kwargs):
                self.calls.append(dict(kwargs))
                return (
                    '{"status":"success","message":"ok","summary":"审查完成，共发现 2 个问题。",'
                    '"llm_result":"审查完成，共发现 2 个问题。",'
                    '"problems":[{"dimension":"format","severity":"medium","location":"第1段","description":"标题末尾有标点","suggestion":"删除标题末尾句号"}]}'
                )

        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="不应触发", tool_calls=[]))
        loop.tools.get_definitions = MagicMock(return_value=[])
        review_tool = OfficialDocReviewTool()
        loop.tools.register(review_tool)

        upload_msg = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            chat_id="chat123",
            content="[file: oa_notice.docx]",
            metadata={
                "attachments": [
                    {
                        "kind": "file",
                        "name": "oa_notice.docx",
                        "storage_uri": "s3://tenant-files/u1/chat123/oa_notice.docx",
                    }
                ]
            },
        )
        upload_result = await loop._process_message(upload_msg)

        assert upload_result is None

        progress: list[dict[str, object]] = []

        async def on_progress(content: str, **kwargs) -> None:
            progress.append({"content": content, **kwargs})

        review_msg = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            chat_id="chat123",
            content="看看刚才上传的附件是否符合公文要求",
        )
        result = await loop._process_message(review_msg, on_progress=on_progress)

        assert result is not None
        assert result.content == "审查完成，共发现 2 个问题。"
        assert len(review_tool.calls) == 1
        assert review_tool.calls[0]["storage_uri"] == "s3://tenant-files/u1/chat123/oa_notice.docx"
        assert any(
            item.get("progress_stage") == "skill_call"
            and item.get("skill_name") == "official_doc_review"
            and item.get("call_type") == "skill"
            for item in progress
        )
        loop.provider.chat_with_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_official_doc_review_uses_latest_uploaded_message_only(
        self,
        tmp_path: Path,
    ) -> None:
        class OfficialDocReviewTool(Tool):
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            @property
            def name(self) -> str:
                return "official_doc_review"

            @property
            def description(self) -> str:
                return "review official document quality"

            @property
            def parameters(self) -> dict[str, object]:
                return {
                    "type": "object",
                    "properties": {
                        "storage_uri": {"type": "string"},
                        "file_id": {"type": "string"},
                    },
                }

            @property
            def direct_handler(self):
                from ithqbot.skills.official_doc_review.direct_handler import handle_direct_official_doc_review

                return handle_direct_official_doc_review

            async def execute(self, **kwargs):
                self.calls.append(dict(kwargs))
                file_name = str(kwargs.get("file_name") or "unknown.docx")
                return (
                    '{"status":"success","message":"ok","summary":"已完成",'
                    f'"llm_result":"本次审查文件：{file_name}",'
                    f'"source":{{"name":"{file_name}"}},'
                    '"problems":[]}'
                )

        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="不应触发", tool_calls=[]))
        loop.tools.get_definitions = MagicMock(return_value=[])
        review_tool = OfficialDocReviewTool()
        loop.tools.register(review_tool)

        first_upload = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            chat_id="chat123",
            content="[file: old.docx]",
            metadata={
                "attachments": [
                    {
                        "kind": "file",
                        "name": "old.docx",
                        "storage_uri": "s3://tenant-files/u1/chat123/old.docx",
                    }
                ]
            },
        )
        second_upload = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            chat_id="chat123",
            content="[file: latest.docx]",
            metadata={
                "attachments": [
                    {
                        "kind": "file",
                        "name": "latest.docx",
                        "storage_uri": "s3://tenant-files/u1/chat123/latest.docx",
                    }
                ]
            },
        )

        assert await loop._process_message(first_upload) is None
        assert await loop._process_message(second_upload) is None

        review_msg = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            chat_id="chat123",
            content="检查一下公文格式、内容合规性",
        )
        result = await loop._process_message(review_msg)

        assert result is not None
        assert len(review_tool.calls) == 1
        assert review_tool.calls[0]["storage_uri"] == "s3://tenant-files/u1/chat123/latest.docx"
        assert result.content == "本次审查文件：latest.docx"
        loop.provider.chat_with_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_official_doc_review_prefers_latest_attachment_within_same_upload_message(
        self,
        tmp_path: Path,
    ) -> None:
        class OfficialDocReviewTool(Tool):
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            @property
            def name(self) -> str:
                return "official_doc_review"

            @property
            def description(self) -> str:
                return "review official document quality"

            @property
            def parameters(self) -> dict[str, object]:
                return {
                    "type": "object",
                    "properties": {
                        "storage_uri": {"type": "string"},
                        "file_id": {"type": "string"},
                    },
                }

            @property
            def direct_handler(self):
                from ithqbot.skills.official_doc_review.direct_handler import handle_direct_official_doc_review

                return handle_direct_official_doc_review

            async def execute(self, **kwargs):
                self.calls.append(dict(kwargs))
                file_name = str(kwargs.get("file_name") or "unknown.docx")
                return (
                    '{"status":"success","message":"ok","summary":"已完成",'
                    f'"llm_result":"本次审查文件：{file_name}",'
                    f'"source":{{"name":"{file_name}"}},'
                    '"problems":[]}'
                )

        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="不应触发", tool_calls=[]))
        loop.tools.get_definitions = MagicMock(return_value=[])
        review_tool = OfficialDocReviewTool()
        loop.tools.register(review_tool)

        upload_msg = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            chat_id="chat123",
            content="[file: 活动通知.docx]",
            metadata={
                "attachments": [
                    {
                        "kind": "file",
                        "name": "1.docx",
                        "storage_uri": "s3://tenant-files/u1/chat123/1.docx",
                    },
                    {
                        "kind": "file",
                        "name": "活动通知.docx",
                        "storage_uri": "s3://tenant-files/u1/chat123/activity.docx",
                    },
                ]
            },
        )
        assert await loop._process_message(upload_msg) is None

        review_msg = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            chat_id="chat123",
            content="检查一下公文格式、内容合规性",
        )
        result = await loop._process_message(review_msg)

        assert result is not None
        assert len(review_tool.calls) == 1
        assert review_tool.calls[0]["storage_uri"] == "s3://tenant-files/u1/chat123/activity.docx"
        assert result.content == "本次审查文件：活动通知.docx"
        loop.provider.chat_with_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_official_doc_review_prefers_newer_uploaded_at_over_attachment_order(
        self,
        tmp_path: Path,
    ) -> None:
        class OfficialDocReviewTool(Tool):
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            @property
            def name(self) -> str:
                return "official_doc_review"

            @property
            def description(self) -> str:
                return "review official document quality"

            @property
            def parameters(self) -> dict[str, object]:
                return {
                    "type": "object",
                    "properties": {
                        "storage_uri": {"type": "string"},
                        "file_id": {"type": "string"},
                    },
                }

            @property
            def direct_handler(self):
                from ithqbot.skills.official_doc_review.direct_handler import handle_direct_official_doc_review

                return handle_direct_official_doc_review

            async def execute(self, **kwargs):
                self.calls.append(dict(kwargs))
                file_name = str(kwargs.get("file_name") or "unknown.docx")
                return (
                    '{"status":"success","message":"ok","summary":"已完成",'
                    f'"llm_result":"本次审查文件：{file_name}",'
                    f'"source":{{"name":"{file_name}"}},'
                    '"problems":[]}'
                )

        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="不应触发", tool_calls=[]))
        loop.tools.get_definitions = MagicMock(return_value=[])
        review_tool = OfficialDocReviewTool()
        loop.tools.register(review_tool)

        upload_msg = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            chat_id="chat123",
            content="[file: 活动通知.docx]",
            metadata={
                "attachments": [
                    {
                        "kind": "file",
                        "name": "活动通知.docx",
                        "storage_uri": "s3://tenant-files/u1/chat123/activity.docx",
                        "uploaded_at": "2026-04-23T17:21:15+08:00",
                    },
                    {
                        "kind": "file",
                        "name": "1.docx",
                        "storage_uri": "s3://tenant-files/u1/chat123/1.docx",
                        "uploaded_at": "2026-04-23T17:18:00+08:00",
                    },
                ]
            },
        )
        assert await loop._process_message(upload_msg) is None

        review_msg = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            chat_id="chat123",
            content="检查一下公文格式、内容合规性",
        )
        result = await loop._process_message(review_msg)

        assert result is not None
        assert len(review_tool.calls) == 1
        assert review_tool.calls[0]["storage_uri"] == "s3://tenant-files/u1/chat123/activity.docx"
        assert result.content == "本次审查文件：活动通知.docx"
        loop.provider.chat_with_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_official_doc_review_loads_latest_named_file_from_storage_when_message_has_no_attachment(
        self,
        tmp_path: Path,
    ) -> None:
        class OfficialDocReviewTool(Tool):
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            @property
            def name(self) -> str:
                return "official_doc_review"

            @property
            def description(self) -> str:
                return "review official document quality"

            @property
            def parameters(self) -> dict[str, object]:
                return {
                    "type": "object",
                    "properties": {
                        "storage_uri": {"type": "string"},
                        "file_id": {"type": "string"},
                    },
                }

            @property
            def direct_handler(self):
                from ithqbot.skills.official_doc_review.direct_handler import handle_direct_official_doc_review

                return handle_direct_official_doc_review

            async def execute(self, **kwargs):
                self.calls.append(dict(kwargs))
                file_name = str(kwargs.get("file_name") or "unknown.docx")
                return (
                    '{"status":"success","message":"ok","summary":"已完成",'
                    f'"llm_result":"本次审查文件：{file_name}",'
                    f'"source":{{"name":"{file_name}"}},'
                    '"problems":[]}'
                )

        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="不应触发", tool_calls=[]))
        loop.tools.get_definitions = MagicMock(return_value=[])
        review_tool = OfficialDocReviewTool()
        loop.tools.register(review_tool)
        loop.file_service.list = AsyncMock(
            return_value=[
                {
                    "file_id": "f_latest_duplicate",
                    "name": "activity-latest.docx",
                    "original_file_name": "活动通知.docx",
                    "storage_uri": "s3://tenant-files/u1/chat123/activity-latest.docx",
                    "uploaded_at": "2026-04-23T09:21:15Z",
                },
                {
                    "file_id": "f_old_duplicate",
                    "name": "activity-old.docx",
                    "original_file_name": "活动通知.docx",
                    "storage_uri": "s3://tenant-files/u1/chat123/activity-old.docx",
                    "uploaded_at": "2026-04-23T08:18:00Z",
                },
            ]
        )

        review_msg = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            account_id="user1",
            chat_id="chat123",
            bot_id="bot_A",
            tenant_id="winlmp",
            content="检查一下公文格式、内容合规性，文件名是活动通知.docx",
            metadata={},
        )
        result = await loop._process_message(review_msg)

        assert result is not None
        assert len(review_tool.calls) == 1
        assert review_tool.calls[0]["file_id"] == "f_latest_duplicate"
        assert review_tool.calls[0]["file_name"] == "活动通知.docx"
        assert result.content == "本次审查文件：活动通知.docx"
        loop.provider.chat_with_retry.assert_not_awaited()
        loop.file_service.list.assert_awaited()

    @pytest.mark.asyncio
    async def test_direct_route_trace_event_contains_tool_handler_and_latency(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class OfficialDocReviewTool(Tool):
            @property
            def name(self) -> str:
                return "official_doc_review"

            @property
            def description(self) -> str:
                return "review official document quality"

            @property
            def parameters(self) -> dict[str, object]:
                return {
                    "type": "object",
                    "properties": {
                        "storage_uri": {"type": "string"},
                        "file_id": {"type": "string"},
                    },
                }

            @property
            def direct_handler(self):
                from ithqbot.skills.official_doc_review.direct_handler import handle_direct_official_doc_review

                return handle_direct_official_doc_review

            async def execute(self, **_kwargs):
                return (
                    '{"status":"success","message":"ok","summary":"审查完成，共发现 2 个问题。",'
                    '"llm_result":"审查完成，共发现 2 个问题。","problems":[]}'
                )

        trace_events: list[dict[str, object]] = []

        def capture_trace_event(**kwargs) -> None:
            trace_events.append(kwargs)

        monkeypatch.setattr(loop_module, "record_trace_event", capture_trace_event)

        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="不应触发", tool_calls=[]))
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.register(OfficialDocReviewTool())

        upload_msg = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            chat_id="chat123",
            content="[file: oa_notice.docx]",
            metadata={
                "request_msg_id": "req-1",
                "attachments": [
                    {
                        "kind": "file",
                        "name": "oa_notice.docx",
                        "storage_uri": "s3://tenant-files/u1/chat123/oa_notice.docx",
                    }
                ],
            },
        )
        upload_result = await loop._process_message(upload_msg)

        assert upload_result is None

        review_msg = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            chat_id="chat123",
            content="帮我审查这个公文",
            metadata={"request_msg_id": "req-2", "client_id": "platform"},
        )
        result = await loop._process_message(review_msg)

        assert result is not None
        direct_route_events = [
            item
            for item in trace_events
            if item.get("event_name") == "agent.direct_route" and item.get("component") == "official_doc_review"
        ]
        assert len(direct_route_events) >= 2
        running_event = next(item for item in direct_route_events if item.get("phase") == "running")
        end_event = next(item for item in direct_route_events if item.get("phase") == "end")
        assert running_event["component"] == "official_doc_review"
        assert running_event["request_msg_id"] == "req-2"
        assert end_event["duration_ms"] >= 0
        assert end_event["details"]["tool_name"] == "official_doc_review"
        assert "official_doc_review.direct_handler.handle_direct_official_doc_review" in end_event["details"]["handler_source"]
        assert end_event["details"]["latency_ms"] >= 0

    @pytest.mark.asyncio
    async def test_doc_compare_download_returns_single_file_reply(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="不应触发", tool_calls=[]))
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.get = MagicMock(side_effect=lambda name: object() if name == "doc_compare" else None)
        loop.tools.execute = AsyncMock(return_value='{"_ithqbot_event":"doc_compare","llm_result":"对比结果已生成：[点击下载对比报告](https://example.com/report.md)","file_message":"文档对比结果已生成，请下载文件查看。","files":[{"name":"report.md","mime":"text/markdown","rel_path":"doc_compare_reports/report.md","size":128,"download_url":"https://example.com/report.md","storage":{"backend":"minio","bucket":"ithqbot-storage","path":"doc_compare_reports/report.md"}}]}')

        progress: list[dict[str, object]] = []

        async def on_progress(content: str, **kwargs) -> None:
            progress.append({"content": content, **kwargs})

        msg = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            chat_id="chat123",
            content="246810",
            metadata={
                "interaction_response": {
                    "values": {"otp_code": "246810"},
                    "context": {
                        "tool": "doc_compare",
                        "path1": "a.docx",
                        "path2": "b.docx",
                        "download": True,
                    },
                }
            },
        )

        result = await loop._process_message(msg, on_progress=on_progress)

        assert result is not None
        assert result.content == "对比结果已生成：[点击下载对比报告](https://example.com/report.md)"
        assert result.metadata["content_type"] == "file"
        assert result.metadata["files"][0]["size"] == 128
        assert result.metadata["files"][0]["download_url"] == "https://example.com/report.md"
        assert all(item.get("status_event") != "file" for item in progress)

    @pytest.mark.asyncio
    async def test_doc_compare_initial_reply_keeps_request_context_and_interaction(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="不应触发", tool_calls=[]))
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.get = MagicMock(side_effect=lambda name: object() if name == "doc_compare" else None)
        loop.tools.execute = AsyncMock(return_value='{"_ithqbot_event":"doc_compare","llm_result":"摘要：存在 3 处差异。","interaction_message":"请输入验证码后下载本次文档对比结果。","interaction":{"type":"otp","interaction_id":"doc-compare-1","title":"下载对比结果需要验证码","fields":[{"key":"otp_code","label":"验证码","input_type":"otp","required":true}]}}')

        progress: list[dict[str, object]] = []

        async def on_progress(content: str, **kwargs) -> None:
            progress.append({"content": content, **kwargs})

        msg = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            chat_id="chat123",
            content="比较一下两个文档的内容差异",
            metadata={
                "request_msg_id": "u-req-123",
                "client_id": "web_abc",
                "attachments": [
                    {"storage": {"bucket": "ithqbot-storage", "path": "u1/c1/a.docx"}},
                    {"storage": {"bucket": "ithqbot-storage", "path": "u1/c1/b.docx"}},
                ],
            },
        )

        result = await loop._process_message(msg, on_progress=on_progress)

        assert result is not None
        assert result.content == "摘要：存在 3 处差异。"
        assert result.reply_to == "u-req-123"
        assert result.metadata["request_msg_id"] == "u-req-123"
        assert result.metadata["client_id"] == "web_abc"
        assert result.metadata["interaction"]["type"] == "otp"
        assert all(item.get("status_event") != "interaction" for item in progress)

    @pytest.mark.asyncio
    async def test_skill_compliance_request_routes_to_check_skill_instead_of_doc_compare(self, tmp_path: Path, monkeypatch) -> None:
        class CheckSkillTool(Tool):
            @property
            def name(self) -> str:
                return "check_skill"

            @property
            def description(self) -> str:
                return "check skill compliance"

            @property
            def parameters(self) -> dict[str, object]:
                return {"type": "object", "properties": {"skill_name": {"type": "string"}}}

            async def execute(self, **kwargs):
                return f"Skill: {kwargs.get('skill_name')}\nPASS:\n- ok\nWARN:\n- (none)\nFAIL:\n- (none)\n\nFix Plan:\n1) (none)"

        class DocCompareTool(Tool):
            @property
            def name(self) -> str:
                return "doc_compare"

            @property
            def description(self) -> str:
                return "compare docs"

            @property
            def parameters(self) -> dict[str, object]:
                return {"type": "object", "properties": {"path1": {"type": "string"}, "path2": {"type": "string"}}}

            async def execute(self, **kwargs):
                return "不应命中 doc_compare"

        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="不应触发", tool_calls=[]))
        loop.tools.register(CheckSkillTool())
        loop.tools.register(DocCompareTool())
        progress: list[dict[str, object]] = []
        trace_events: list[dict[str, object]] = []

        def capture_trace_event(**kwargs) -> None:
            trace_events.append(kwargs)

        monkeypatch.setattr(loop_module, "record_trace_event", capture_trace_event)

        async def on_progress(content: str, **kwargs) -> None:
            progress.append({"content": content, **kwargs})

        msg = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            chat_id="chat123",
            content="帮我检查一下doc_compare skill是否符合规范？",
        )
        result = await loop._process_message(msg, on_progress=on_progress)

        assert result is not None
        assert result.content.startswith("Skill: doc_compare")
        assert any(
            item.get("progress_stage") == "skill_call"
            and item.get("skill_name") == "check_skill"
            and item.get("call_type") == "skill"
            for item in progress
        )
        graph_events = [item["event_name"] for item in trace_events if str(item.get("event_name", "")).startswith("graph.")]
        assert graph_events == [
            "graph.run.start",
            "graph.node.done",
            "graph.run.done",
        ]
        loop.provider.chat_with_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_file_summary_request_routes_to_file_summary_skill(self, tmp_path: Path) -> None:
        class FileSummaryTool(Tool):
            @property
            def name(self) -> str:
                return "file_summary_skill"

            @property
            def description(self) -> str:
                return "summarize a file by file_id"

            @property
            def parameters(self) -> dict[str, object]:
                return {"type": "object", "properties": {"file_id": {"type": "string"}}}

            async def execute(self, **kwargs):
                source_file_id = str(kwargs.get("file_id") or "")
                return (
                    '{"status":"success","source_file_id":"'
                    + source_file_id
                    + '","new_file_id":"f_summary_123"}'
                )

        class CheckSkillTool(Tool):
            @property
            def name(self) -> str:
                return "check_skill"

            @property
            def description(self) -> str:
                return "check skill compliance"

            @property
            def parameters(self) -> dict[str, object]:
                return {"type": "object", "properties": {"skill_name": {"type": "string"}}}

            async def execute(self, **kwargs):
                return "不应命中 check_skill"

        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="不应触发", tool_calls=[]))
        loop.tools.register(FileSummaryTool())
        loop.tools.register(CheckSkillTool())
        loop.file_service.get = AsyncMock(
            return_value={
                "file_id": "f_summary_123",
                "name": "summary.txt",
                "mime": "text/plain",
                "size": 10,
                "download_url": "https://files.test/f_summary_123",
                "storage": {
                    "bucket": "tenant-files",
                    "object_key": "tenant-1/account-1/bot-1/chat-1/req-1/f_summary_123/summary.txt",
                },
            }
        )
        progress: list[dict[str, object]] = []

        async def on_progress(content: str, **kwargs) -> None:
            progress.append({"content": content, **kwargs})

        msg = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            chat_id="chat123",
            content="总结一下刚刚的文件内容",
            metadata={
                "interaction_response": {
                    "context": {"tool": "file_summary_skill", "file_id": "f_source_001"},
                }
            },
        )
        result = await loop._process_message(msg, on_progress=on_progress)

        assert result is not None
        assert "f_summary_123" in result.content
        assert result.metadata.get("content_type") == "file"
        assert result.metadata.get("files")[0]["file_id"] == "f_summary_123"
        assert result.metadata.get("files")[0]["storage"]["path"]
        assert any(
            item.get("progress_stage") == "skill_call"
            and item.get("skill_name") == "file_summary_skill"
            for item in progress
        )
        loop.provider.chat_with_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_file_summary_request_matches_filename_hint_from_recent_files(self, tmp_path: Path) -> None:
        class FileSummaryTool(Tool):
            @property
            def name(self) -> str:
                return "file_summary_skill"

            @property
            def description(self) -> str:
                return "summarize a file by file_id"

            @property
            def parameters(self) -> dict[str, object]:
                return {"type": "object", "properties": {"file_id": {"type": "string"}}}

            async def execute(self, **kwargs):
                source_file_id = str(kwargs.get("file_id") or "")
                return (
                    '{"status":"success","source_file_id":"'
                    + source_file_id
                    + '","new_file_id":"f_summary_by_name"}'
                )

        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="不应触发", tool_calls=[]))
        loop.tools.register(FileSummaryTool())
        loop.file_service.list = AsyncMock(
            return_value=[{"file_id": "f_from_name_001", "name": "AI 驱动研发变革.docx"}]
        )
        loop.file_service.get = AsyncMock(
            return_value={
                "file_id": "f_summary_by_name",
                "name": "summary.txt",
                "mime": "text/plain",
                "size": 10,
                "download_url": "https://files.test/f_summary_by_name",
                "storage": {
                    "bucket": "tenant-files",
                    "object_key": "winlmp/user1/bot_A/chat123/req-1/f_summary_by_name/summary.txt",
                },
            }
        )

        msg = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            account_id="user1",
            chat_id="chat123",
            bot_id="bot_A",
            tenant_id="winlmp",
            content="总结一下这个文档：AI 驱动研发变革.docx",
            metadata={},
        )
        result = await loop._process_message(msg)

        assert result is not None
        assert "f_summary_by_name" in result.content
        assert result.metadata.get("files")[0]["file_id"] == "f_summary_by_name"
        assert result.metadata.get("files")[0]["storage"]["path"]
        loop.provider.chat_with_retry.assert_not_awaited()
        loop.file_service.list.assert_awaited()

    @pytest.mark.asyncio
    async def test_file_summary_request_prefers_latest_duplicate_original_file_name(self, tmp_path: Path) -> None:
        class FileSummaryTool(Tool):
            @property
            def name(self) -> str:
                return "file_summary_skill"

            @property
            def description(self) -> str:
                return "summarize a file by file_id"

            @property
            def parameters(self) -> dict[str, object]:
                return {"type": "object", "properties": {"file_id": {"type": "string"}}}

            async def execute(self, **kwargs):
                source_file_id = str(kwargs.get("file_id") or "")
                return (
                    '{"status":"success","source_file_id":"'
                    + source_file_id
                    + '","new_file_id":"f_summary_latest_dup"}'
                )

        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="不应触发", tool_calls=[]))
        loop.tools.register(FileSummaryTool())
        loop.file_service.list = AsyncMock(
            return_value=[
                {
                    "file_id": "f_latest_duplicate",
                    "name": "activity-latest.docx",
                    "original_file_name": "活动通知.docx",
                    "uploaded_at": "2026-04-23T09:21:15Z",
                },
                {
                    "file_id": "f_old_duplicate",
                    "name": "activity-old.docx",
                    "original_file_name": "活动通知.docx",
                    "uploaded_at": "2026-04-23T08:18:00Z",
                },
            ]
        )
        loop.file_service.get = AsyncMock(
            return_value={
                "file_id": "f_summary_latest_dup",
                "name": "summary.txt",
                "mime": "text/plain",
                "size": 10,
                "download_url": "https://files.test/f_summary_latest_dup",
                "storage": {
                    "bucket": "tenant-files",
                    "object_key": "winlmp/user1/bot_A/chat123/req-1/f_summary_latest_dup/summary.txt",
                },
            }
        )

        msg = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            account_id="user1",
            chat_id="chat123",
            bot_id="bot_A",
            tenant_id="winlmp",
            content="总结一下这个文档：活动通知.docx",
            metadata={},
        )
        result = await loop._process_message(msg)

        assert result is not None
        assert "f_summary_latest_dup" in result.content
        loop.provider.chat_with_retry.assert_not_awaited()
        loop.file_service.list.assert_awaited()

    @pytest.mark.asyncio
    async def test_file_summary_request_without_file_context_returns_explicit_prompt(self, tmp_path: Path) -> None:
        class FileSummaryTool(Tool):
            @property
            def name(self) -> str:
                return "file_summary_skill"

            @property
            def description(self) -> str:
                return "summarize a file by file_id"

            @property
            def parameters(self) -> dict[str, object]:
                return {"type": "object", "properties": {"file_id": {"type": "string"}}}

            async def execute(self, **kwargs):
                return "不应触发 file_summary_skill"

        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="不应触发", tool_calls=[]))
        loop.tools.register(FileSummaryTool())
        loop.file_service.list = AsyncMock(return_value=[])

        msg = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            account_id="user1",
            chat_id="chat123",
            bot_id="bot_A",
            tenant_id="winlmp",
            content="总结一下这个文档",
            metadata={},
        )
        result = await loop._process_message(msg)

        assert result is not None
        assert "未找到可摘要的文件" in result.content
        loop.provider.chat_with_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_file_summary_request_with_attachment_context_routes_to_direct_skill(self, tmp_path: Path) -> None:
        class FileSummaryTool(Tool):
            @property
            def name(self) -> str:
                return "file_summary_skill"

            @property
            def description(self) -> str:
                return "summarize a file by file_id"

            @property
            def parameters(self) -> dict[str, object]:
                return {"type": "object", "properties": {"file_id": {"type": "string"}}}

            async def execute(self, **kwargs):
                source_file_id = str(kwargs.get("file_id") or "")
                return (
                    '{"status":"success","source_file_id":"'
                    + source_file_id
                    + '","new_file_id":"f_summary_from_attachment"}'
                )

        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="不应触发", tool_calls=[]))
        loop.tools.register(FileSummaryTool())
        loop.file_service.resolve_file_id_from_legacy = AsyncMock(return_value="f_legacy_from_attachment")
        loop.file_service.get = AsyncMock(
            return_value={
                "file_id": "f_summary_from_attachment",
                "name": "summary.txt",
                "mime": "text/plain",
                "size": 10,
                "download_url": "https://files.test/f_summary_from_attachment",
                "storage": {
                    "bucket": "tenant-files",
                    "object_key": "winlmp/user1/bot_A/chat123/req-1/f_summary_from_attachment/summary.txt",
                },
            }
        )

        msg = InboundMessage(
            channel="icatmsg",
            sender_id="user1",
            account_id="user1",
            chat_id="chat123",
            bot_id="bot_A",
            tenant_id="winlmp",
            content="总结一下文件内容",
            metadata={
                "attachments": [
                    {
                        "name": "AI 驱动研发变革.docx",
                        "rel_path": "u123/c1/AI 驱动研发变革.docx",
                        "minio_uri": "minio://ithqbot-storage/u123/c1/AI 驱动研发变革.docx",
                    }
                ]
            },
        )
        result = await loop._process_message(msg)

        assert result is not None
        assert "f_summary_from_attachment" in result.content
        assert result.metadata.get("files")[0]["file_id"] == "f_summary_from_attachment"
        loop.provider.chat_with_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_forces_continuation_when_interim_only_text_without_tool_calls(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        calls = iter([
            LLMResponse(content="我将帮助您分析附件。正在检查文件结构...\n\n(以下为工具调用)", tool_calls=[]),
            LLMResponse(content="分析完成：已统计各部门录用人数。", tool_calls=[]),
        ])
        loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])

        msg = InboundMessage(channel="icatmsg", sender_id="user1", chat_id="chat123", content="请分析")
        result = await loop._process_message(msg)

        assert result is not None
        assert "分析完成" in result.content

    @pytest.mark.asyncio
    async def test_recovers_textual_function_call_and_executes_tool(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        calls = iter([
            LLMResponse(
                content="""<function>web_search
```json
{"query":"三分钟后提醒我吃饭怎么表达更自然"}
```
</function>""",
                tool_calls=[],
            ),
            LLMResponse(content="已完成工具检索。", tool_calls=[]),
        ])
        loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.execute = AsyncMock(return_value="✅ 已添加提醒")

        msg = InboundMessage(channel="icatmsg", sender_id="user1", chat_id="chat123", content="三分钟后提醒我吃饭")
        result = await loop._process_message(msg)

        assert result is not None
        assert "已完成工具检索" in result.content
        loop.tools.execute.assert_awaited()
        args = loop.tools.execute.await_args.args
        assert args[0] == "web_search"
        assert args[1]["query"] == "三分钟后提醒我吃饭怎么表达更自然"

    @pytest.mark.asyncio
    async def test_retries_with_repaired_history_after_messages_illegal(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        seen_messages: list[list[dict]] = []

        async def fake_chat_with_retry(*, messages, **kwargs):
            seen_messages.append([dict(m) for m in messages])
            if len(seen_messages) == 1:
                return LLMResponse(
                    content='Error calling LLM: provider.BadRequestError: "messages" in request are illegal.',
                    tool_calls=[],
                    finish_reason="error",
                )
            return LLMResponse(content="现在是21:33", tool_calls=[])

        loop.provider.chat_with_retry = AsyncMock(side_effect=fake_chat_with_retry)
        loop.tools.get_definitions = MagicMock(return_value=[])

        initial_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "tool_calls": [{"id": "call_a", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_a", "name": "x", "content": "ok"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "现在是几点？"},
        ]
        final_content, _, _ = await loop._run_agent_loop(initial_messages)

        assert final_content == "现在是21:33"
        assert len(seen_messages) == 2
        assert any(m.get("role") == "tool" for m in seen_messages[0])
        assert all(m.get("role") != "tool" for m in seen_messages[1])
        assert all(not (m.get("role") == "assistant" and m.get("tool_calls")) for m in seen_messages[1])

    @pytest.mark.asyncio
    async def test_retries_with_minimal_context_when_repaired_history_unchanged(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        seen_messages: list[list[dict]] = []

        async def fake_chat_with_retry(*, messages, **kwargs):
            seen_messages.append([dict(m) for m in messages])
            if len(seen_messages) == 1:
                return LLMResponse(
                    content='Error calling LLM: provider.BadRequestError: "messages" in request are illegal.',
                    tool_calls=[],
                    finish_reason="error",
                )
            return LLMResponse(content="纽约明天多云，10°C~16°C", tool_calls=[])

        loop.provider.chat_with_retry = AsyncMock(side_effect=fake_chat_with_retry)
        loop.tools.get_definitions = MagicMock(return_value=[])

        initial_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "第一轮问题"},
            {"role": "assistant", "content": "第一轮回答"},
            {"role": "user", "content": "纽约明天天气如何？"},
        ]
        final_content, _, _ = await loop._run_agent_loop(initial_messages)

        assert final_content == "纽约明天多云，10°C~16°C"
        assert len(seen_messages) == 2
        assert seen_messages[0] == initial_messages
        assert seen_messages[1] == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "纽约明天天气如何？"},
        ]

    @pytest.mark.asyncio
    async def test_retries_illegal_messages_again_after_tool_turn(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        seen_messages: list[list[dict]] = []
        search_call = ToolCallRequest(id="call_search", name="web_search", arguments={"query": "合肥天气如何？"})

        async def fake_chat_with_retry(*, messages, **kwargs):
            seen_messages.append([dict(m) for m in messages])
            if len(seen_messages) == 1:
                return LLMResponse(
                    content='Error calling LLM: provider.BadRequestError: "messages" in request are illegal.',
                    tool_calls=[],
                    finish_reason="error",
                )
            if len(seen_messages) == 2:
                return LLMResponse(content="", tool_calls=[search_call], finish_reason="tool_calls")
            if len(seen_messages) == 3:
                return LLMResponse(
                    content='Error calling LLM: provider.BadRequestError: "messages" in request are illegal.',
                    tool_calls=[],
                    finish_reason="error",
                )
            return LLMResponse(content="合肥当前晴，16°C", tool_calls=[])

        loop.provider.chat_with_retry = AsyncMock(side_effect=fake_chat_with_retry)
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.execute = AsyncMock(return_value="合肥当前晴，16°C")

        initial_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "tool_calls": [{"id": "call_a", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_a", "name": "x", "content": "ok"},
            {"role": "user", "content": "合肥天气如何？"},
        ]
        final_content, _, _ = await loop._run_agent_loop(initial_messages)

        assert final_content == "合肥当前晴，16°C"
        assert len(seen_messages) == 4
        assert any(m.get("role") == "tool" for m in seen_messages[2])
        assert all(m.get("role") != "tool" for m in seen_messages[3])

    @pytest.mark.asyncio
    async def test_blocks_repeated_identical_tool_batch(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        repeated_calls = [
            ToolCallRequest(
                id="call_add_1",
                name="cron",
                arguments={
                    "action": "add",
                    "at": "2026-04-20T10:00:00",
                    "tz": "Asia/Shanghai",
                    "message": "组织下等开会事宜",
                },
            ),
            ToolCallRequest(
                id="call_list_1",
                name="cron",
                arguments={"action": "list"},
            ),
        ]
        repeated_calls_again = [
            ToolCallRequest(
                id="call_add_2",
                name="cron",
                arguments={
                    "action": "add",
                    "at": "2026-04-20T10:00:00",
                    "tz": "Asia/Shanghai",
                    "message": "组织下等开会事宜",
                },
            ),
            ToolCallRequest(
                id="call_list_2",
                name="cron",
                arguments={"action": "list"},
            ),
        ]
        calls = iter([
            LLMResponse(content="", tool_calls=repeated_calls),
            LLMResponse(content="", tool_calls=repeated_calls_again),
            LLMResponse(content="已为你设置明天上午10点的提醒，并整理了当前提醒。", tool_calls=[]),
        ])
        loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.execute = AsyncMock(side_effect=[
            "Created job '组织下等开会事宜' (id: cron-1)",
            "Active reminders:\n- 组织下等开会事宜",
        ])

        final_content, _, all_msgs = await loop._run_agent_loop(
            [{"role": "user", "content": "我是谁？我有哪些提醒？明天上午10点提醒组织下等开会事宜。"}]
        )

        assert final_content == "已为你设置明天上午10点的提醒，并整理了当前提醒。"
        assert loop.tools.execute.await_count == 2
        duplicate_tool_results = [
            msg["content"]
            for msg in all_msgs
            if msg.get("role") == "tool" and "检测到重复工具调用" in str(msg.get("content"))
        ]
        assert len(duplicate_tool_results) == 2

    @pytest.mark.asyncio
    async def test_does_not_persist_error_response_into_messages(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
            content="Error calling LLM: provider.BadRequestError: invalid request.",
            tool_calls=[],
            finish_reason="error",
        ))
        loop.tools.get_definitions = MagicMock(return_value=[])

        initial_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]
        final_content, _, all_msgs = await loop._run_agent_loop(initial_messages)

        assert final_content is not None
        assert all_msgs == initial_messages

    async def test_progress_hides_internal_reasoning(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(id="call1", name="read_file", arguments={"path": "foo.txt"})
        calls = iter([
            LLMResponse(
                content="Visible<think>hidden</think>",
                tool_calls=[tool_call],
                reasoning_content="secret reasoning",
                thinking_blocks=[{"signature": "sig", "thought": "secret thought"}],
            ),
            LLMResponse(content="Done", tool_calls=[]),
        ])
        loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.execute = AsyncMock(return_value="ok")

        progress: list[tuple[str, bool]] = []

        async def on_progress(content: str, *, tool_hint: bool = False) -> None:
            progress.append((content, tool_hint))

        final_content, _, _ = await loop._run_agent_loop([], on_progress=on_progress)

        assert final_content == "Done"
        assert progress == [
            ("Visible", False),
            ("正在执行第 1 步：read_file", True),
        ]

    async def test_progress_accepts_stage_and_percent_for_rich_callbacks(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(id="call1", name="read_file", arguments={"path": "foo.txt"})
        calls = iter([
            LLMResponse(content="Visible", tool_calls=[tool_call]),
            LLMResponse(content="Done", tool_calls=[]),
        ])
        loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.execute = AsyncMock(return_value="ok")

        progress: list[dict[str, object]] = []

        async def on_progress(content: str, **kwargs) -> None:
            progress.append({"content": content, **kwargs})

        final_content, _, _ = await loop._run_agent_loop([], on_progress=on_progress)

        assert final_content == "Done"
        assert len(progress) == 2
        assert progress[0]["content"] == "Visible"
        assert progress[0]["progress_stage"] == "extracting"
        assert progress[0]["progress_percent"] == 30
        assert progress[1]["content"] == "正在执行第 1 步：read_file"
        assert progress[1]["progress_stage"] == "tool_call"
        assert progress[1]["progress_percent"] == 45
        assert progress[1]["tool_name"] == "read_file"
        assert progress[1]["status_details"]["steps"][0]["title"] == "第 1 步：read_file"


class TestMessageToolTurnTracking:

    def test_sent_in_turn_tracks_same_target(self) -> None:
        tool = MessageTool()
        tool.set_context("feishu", "chat1")
        assert not tool._sent_in_turn
        tool._sent_in_turn = True
        assert tool._sent_in_turn

    def test_start_turn_resets(self) -> None:
        tool = MessageTool()
        tool._sent_in_turn = True
        tool.start_turn()
        assert not tool._sent_in_turn

    def test_should_suppress_when_final_is_empty(self) -> None:
        tool = MessageTool()
        tool._sent_in_turn = True
        assert tool.should_suppress_auto_reply("")
        assert tool.should_suppress_auto_reply("   ")

    def test_should_suppress_when_final_is_duplicate(self) -> None:
        tool = MessageTool()
        tool._sent_in_turn = True
        tool._sent_contents_in_turn = ["Hello"]
        assert tool.should_suppress_auto_reply("Hello")
        assert not tool.should_suppress_auto_reply("Done")


@pytest.mark.asyncio
async def test_bot_guardrails_blocks_tool_by_allowlist(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop.bot_guardrails_config = BotGuardrailsConfig(
        enabled=True,
        bots={
            "bot_finance": BotGuardrailsPolicy(
                tool_allowlist=["web_search"],
            )
        },
    )
    tool_call = ToolCallRequest(
        id="call1",
        name="read_file",
        arguments={"path": "secrets.txt"},
    )
    calls = iter([
        LLMResponse(content="", tool_calls=[tool_call]),
        LLMResponse(content="done", tool_calls=[]),
    ])
    loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
    loop.tools.execute = AsyncMock(return_value="ok")
    route_metadata = {"bot_id": "bot_finance", "channel": "icatmsg", "chat_id": "chat1"}

    final_content, _, all_msgs = await loop._run_agent_loop(
        [],
        route_text="读取文件",
        route_metadata=route_metadata,
    )

    assert final_content == "done"
    loop.tools.execute.assert_not_awaited()
    tool_results = [m for m in all_msgs if m.get("role") == "tool"]
    assert len(tool_results) == 1
    assert "因安全策略限制" in str(tool_results[0].get("content"))
    hits = route_metadata.get("_bot_guardrails_turn_hits") or []
    assert any(hit.get("policy") == "tool_allowlist" for hit in hits)


@pytest.mark.asyncio
async def test_bot_guardrails_blocks_instruction_before_model_call(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop.bot_guardrails_config = BotGuardrailsConfig(
        enabled=True,
        bots={
            "bot_ops": BotGuardrailsPolicy(
                blocked_instruction_patterns=["删除生产数据库", r"drop\s+database"],
                blocked_instruction_message="命中越权策略，已拒绝执行。",
            )
        },
    )
    loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="should-not-run", tool_calls=[]))
    route_metadata = {"bot_id": "bot_ops", "channel": "icatmsg", "chat_id": "chat1"}

    final_content, _, _ = await loop._run_agent_loop(
        [],
        route_text="请删除生产数据库",
        route_metadata=route_metadata,
    )

    assert final_content == "命中越权策略，已拒绝执行。"
    loop.provider.chat_with_retry.assert_not_awaited()
    hits = route_metadata.get("_bot_guardrails_turn_hits") or []
    assert any(hit.get("policy") == "blocked_instruction_patterns" for hit in hits)


@pytest.mark.asyncio
async def test_bot_guardrails_blocks_sensitive_output(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop.bot_guardrails_config = BotGuardrailsConfig(
        enabled=True,
        bots={
            "bot_hr": BotGuardrailsPolicy(
                sensitive_words=["身份证号"],
                sensitive_word_message="输出命中敏感信息策略，已拦截。",
            )
        },
    )
    loop.provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="员工A身份证号为110101199001011234", tool_calls=[])
    )
    route_metadata = {"bot_id": "bot_hr", "channel": "icatmsg", "chat_id": "chat1"}

    final_content, _, _ = await loop._run_agent_loop(
        [],
        route_text="返回员工证件信息",
        route_metadata=route_metadata,
    )

    assert final_content == "输出命中敏感信息策略，已拦截。"
    hits = route_metadata.get("_bot_guardrails_turn_hits") or []
    assert any(isinstance(hit, dict) and hit.get("policy") == "sensitive_words" for hit in hits)


def test_tool_registration_filter_respects_enabled_and_disabled(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        tools_config=ToolsConfig(enabled=["web_search", "message", "mcp_*"], disabled=["message"]),
    )

    assert loop.tools.has("web_search")
    assert not loop.tools.has("message")
    assert not loop.tools.has("minio_push")


def test_tool_workspace_can_be_configured_independently(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        tools_config=ToolsConfig(
            file_root="files",
            minio_workspace="uploads",
        ),
    )

    minio_push = loop.tools.get("minio_push")

    # web_search doesn't use _workspace typically, but we can verify minio_push
    assert getattr(minio_push, "_workspace") == (tmp_path / "uploads").resolve()


def test_builtin_check_skill_is_classified_as_skill_call(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    assert loop.tools.has("check_skill")
    assert loop._tool_call_type("check_skill") == "skill"


@pytest.mark.asyncio
async def test_routing_metadata_contains_decision_fields(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))
    route_metadata = {"bot_id": "bot_a", "tenant_id": "tenant_x"}

    final_content, _, _ = await loop._run_agent_loop(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}],
        route_text="hello",
        route_metadata=route_metadata,
    )

    assert final_content == "ok"
    routing = route_metadata.get("_routing")
    assert isinstance(routing, dict)
    assert routing.get("source")
    assert routing.get("tier")
    assert routing.get("model")
    assert routing.get("final_model")
    assert routing.get("success") is True
