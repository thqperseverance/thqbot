"""编排层单元测试：信封契约、进度提取、入站投递、出站消费与幂等。"""

from __future__ import annotations

import asyncio

import pytest

from app import orchestrator, repository


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- 信封契约


def test_build_inbound_envelope_satisfies_ithqbot_contract(seeded_user, default_conversation):
    envelope = orchestrator.build_inbound_envelope(
        user=seeded_user,
        conversation=default_conversation,
        content="你好",
        request_msg_id="u-1",
    )
    header, payload, metadata = envelope["header"], envelope["payload"], envelope["metadata"]

    # ithqbot 的 _decode_contract 强制要求：header.msg_id、payload.tenant_id/client_id
    assert header["version"] == "v1.2"
    assert header["msg_id"] == "u-1"
    assert header["trace_id"] == "u-1"
    assert header["event"] == "message.user"
    assert payload["tenant_id"] == "test-tenant"
    assert payload["client_id"]
    # icatmsg 适配器另外要求 account_id / chat_id
    assert payload["account_id"] == seeded_user.id
    assert payload["chat_id"] == default_conversation.id
    assert payload["channel"] == "icatmsg"
    assert payload["bot_id"] == default_conversation.bot_id
    assert payload["content"] == "你好"
    assert payload["content_type"] == "text"
    assert metadata["request_msg_id"] == "u-1"


def test_inbound_envelope_key_orders_by_conversation(seeded_user, default_conversation):
    """同一会话的消息必须落到同一分区（否则 agent 侧上下文会乱序）。"""
    from app.source_adapter import _inbound_partition_key

    first = orchestrator.build_inbound_envelope(
        user=seeded_user, conversation=default_conversation, content="a", request_msg_id="u-1"
    )
    second = orchestrator.build_inbound_envelope(
        user=seeded_user, conversation=default_conversation, content="b", request_msg_id="u-2"
    )
    assert _inbound_partition_key(first) == _inbound_partition_key(second)


# --------------------------------------------------------------------------- 进度提取


def test_collect_progress_fields_walks_nested_structures():
    payload = {
        "status_details": {
            "execution": {"skill_name": "summarize", "tool_name": "read_file"},
            "graph": {"node_id": "n1", "graph_id": "g1"},
        },
        "list": [{"tool_name": "read_file"}, {"skill_name": "doc_compare"}],
    }
    found = orchestrator.collect_progress_fields(payload)
    assert found["skill_name"] == ["summarize", "doc_compare"]
    assert found["tool_name"] == ["read_file"]  # 去重
    assert found["node_id"] == ["n1"]
    assert found["graph_id"] == ["g1"]


def test_collect_progress_fields_tolerates_junk():
    assert orchestrator.collect_progress_fields(None) == {}
    assert orchestrator.collect_progress_fields({"skill_name": ""}) == {}
    assert orchestrator.collect_progress_fields(["a", 1, None]) == {}
    assert orchestrator.collect_progress_fields({"skill_name": 123}) == {}


def test_collect_progress_fields_respects_depth_limit():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"skill_name": "too-deep"}}}}}}}
    assert "skill_name" not in orchestrator.collect_progress_fields(deep, max_depth=3)


def test_build_status_summary_extracts_skills_and_percent():
    summary = orchestrator.build_status_summary(
        body={"content": "正在分析"},
        metadata={
            "_progress_percent": 35,
            "_progress_stage": "processing",
            "_status_details": {"execution": {"skill_name": "summarize"}},
        },
        event_type="status.processing",
    )
    assert summary["percent"] == 35
    assert summary["stage"] == "processing"
    assert summary["skills"] == ["summarize"]
    assert summary["message"] == "正在分析"
    assert summary["updated_at"]


# --------------------------------------------------------------------------- 入站


def test_send_user_message_persists_and_publishes(
    db_session, seeded_user, default_conversation, inbound_calls, fake_redis
):
    result = _run(
        orchestrator.send_user_message(
            db_session,
            user=seeded_user,
            conversation=default_conversation,
            content="  你好，帮我总结  ",
        )
    )
    assert result["queued"] is True
    assert result["degraded"] is False
    assert len(inbound_calls) == 1
    assert inbound_calls[0]["payload"]["content"] == "你好，帮我总结"  # 已 strip

    messages, _ = repository.list_messages(db_session, conversation_id=default_conversation.id, limit=10)
    assert len(messages) == 1
    assert messages[0]["sender"] == "user"
    assert fake_redis.events_of_type("message")


def test_send_user_message_uses_client_msg_id_for_idempotency(
    db_session, seeded_user, default_conversation, inbound_calls
):
    first = _run(
        orchestrator.send_user_message(
            db_session,
            user=seeded_user,
            conversation=default_conversation,
            content="一次",
            client_msg_id="u-dup",
        )
    )
    second = _run(
        orchestrator.send_user_message(
            db_session,
            user=seeded_user,
            conversation=default_conversation,
            content="一次",
            client_msg_id="u-dup",
        )
    )
    assert first["request_msg_id"] == "u-dup"
    assert second["request_msg_id"] == "u-dup"
    # 第二条因 external_id 冲突未落库
    messages, _ = repository.list_messages(db_session, conversation_id=default_conversation.id, limit=10)
    assert len(messages) == 1
    assert second["message"] is None


def test_send_user_message_rejects_blank_content(db_session, seeded_user, default_conversation):
    with pytest.raises(ValueError):
        _run(
            orchestrator.send_user_message(
                db_session, user=seeded_user, conversation=default_conversation, content="   "
            )
        )


def test_send_user_message_degrades_when_publish_fails(
    db_session, seeded_user, default_conversation, monkeypatch
):
    async def boom(_envelope):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(orchestrator, "publish_inbound_message", boom)

    result = _run(
        orchestrator.send_user_message(
            db_session, user=seeded_user, conversation=default_conversation, content="在吗"
        )
    )
    assert result["queued"] is False
    assert result["degraded"] is True
    assert "broker unreachable" in result["warning"]

    messages, _ = repository.list_messages(db_session, conversation_id=default_conversation.id, limit=10)
    assert [m["sender"] for m in messages] == ["user", "bot"]
    assert messages[1]["status"] == "failed"


# --------------------------------------------------------------------------- 出站


def test_handle_status_processing_caches_and_pushes_without_persisting(
    db_session, default_conversation, fake_redis
):
    conversation = default_conversation
    result = _run(
        orchestrator.handle_outbound_event(
            db_session,
            {"account_id": conversation.user_id, "chat_id": conversation.id, "content": "思考中"},
            {"msg_id": "m-1", "event": "status.processing"},
            {"_progress_percent": 10, "_progress_stage": "parsing"},
            "status.processing",
        )
    )
    assert result is None
    messages, _ = repository.list_messages(db_session, conversation_id=conversation.id, limit=10)
    assert messages == []
    statuses = fake_redis.events_of_type("status")
    assert len(statuses) == 1
    assert statuses[0]["status"]["percent"] == 10
    assert fake_redis.status[(conversation.user_id, conversation.id)]["stage"] == "parsing"


def test_handle_reply_persists_increments_unread_and_is_idempotent(
    db_session, default_conversation, fake_redis
):
    conversation = default_conversation
    body = {"account_id": conversation.user_id, "chat_id": conversation.id, "content": "这是回复"}
    header = {"msg_id": "m-reply-1", "event": "message.reply"}
    metadata = {"request_msg_id": "u-1", "trace_id": "u-1"}

    first = _run(
        orchestrator.handle_outbound_event(db_session, body, header, metadata, "message.reply")
    )
    assert first is not None
    assert first["sender"] == "bot"
    assert first["request_msg_id"] == "u-1"
    assert conversation.unread_count == 1

    # 同一条 Kafka 消息重复投递：Redis 去重 + DB 唯一约束都不应产生第二条
    second = _run(
        orchestrator.handle_outbound_event(db_session, body, header, metadata, "message.reply")
    )
    assert second is None
    assert conversation.unread_count == 1

    messages, _ = repository.list_messages(db_session, conversation_id=conversation.id, limit=10)
    assert len(messages) == 1
    # 本用例只走编排层，没有用户侧发消息，因此只有这一条 SSE 事件；重复投递不再产生事件
    assert len(fake_redis.events_of_type("message")) == 1


def test_handle_reply_attaches_cached_progress(db_session, default_conversation, fake_redis):
    conversation = default_conversation
    _run(
        orchestrator.handle_outbound_event(
            db_session,
            {"account_id": conversation.user_id, "chat_id": conversation.id, "content": "处理中"},
            {"msg_id": "m-p", "event": "status.processing"},
            {"_status_details": {"skill_name": "summarize"}, "_progress_percent": 50},
            "status.processing",
        )
    )
    _run(
        orchestrator.handle_outbound_event(
            db_session,
            {"account_id": conversation.user_id, "chat_id": conversation.id, "content": "结果"},
            {"msg_id": "m-r", "event": "message.reply"},
            {"request_msg_id": "u-9"},
            "message.reply",
        )
    )
    messages, _ = repository.list_messages(db_session, conversation_id=conversation.id, limit=10)
    assert len(messages) == 1
    assert messages[0]["meta"]["progress"]["skills"] == ["summarize"]


def test_handle_interaction_persists_meta(db_session, default_conversation):
    conversation = default_conversation
    interaction = {"version": "v1", "interaction_id": "i-1", "type": "confirm", "title": "确认?"}
    result = _run(
        orchestrator.handle_outbound_event(
            db_session,
            {
                "account_id": conversation.user_id,
                "chat_id": conversation.id,
                "content": "请确认",
                "interaction": interaction,
            },
            {"msg_id": "m-i", "event": "message.interaction"},
            {},
            "message.interaction",
        )
    )
    assert result is not None
    assert result["meta"]["interaction"] == interaction


def test_handle_unknown_conversation_is_ignored_and_releases_claim(
    db_session, seeded_user, fake_redis
):
    result = _run(
        orchestrator.handle_outbound_event(
            db_session,
            {"account_id": seeded_user.id, "chat_id": "no-such-conversation", "content": "x"},
            {"msg_id": "m-orphan", "event": "message.reply"},
            {},
            "message.reply",
        )
    )
    assert result is None
    # 占位已释放，Kafka 重投时仍可被处理
    assert (seeded_user.id, "m-orphan") not in fake_redis.keys


def test_handle_event_without_routing_fields_is_ignored(db_session):
    for body in ({"content": "x"}, {"account_id": "u", "content": "x"}, {"chat_id": "c", "content": "x"}):
        assert (
            _run(
                orchestrator.handle_outbound_event(
                    db_session, body, {"msg_id": "m"}, {}, "message.reply"
                )
            )
            is None
        )


def test_handle_empty_reply_is_skipped(db_session, default_conversation, fake_redis):
    conversation = default_conversation
    result = _run(
        orchestrator.handle_outbound_event(
            db_session,
            {"account_id": conversation.user_id, "chat_id": conversation.id, "content": ""},
            {"msg_id": "m-empty", "event": "message.reply"},
            {},
            "message.reply",
        )
    )
    assert result is None
    messages, _ = repository.list_messages(db_session, conversation_id=conversation.id, limit=10)
    assert messages == []
    assert (conversation.user_id, "m-empty") not in fake_redis.keys


# --------------------------------------------------------- 技能/工具可见性（D14/D15）


def test_status_summary_reads_first_class_tool_and_skill_fields():
    """ithqbot 的进度元数据把工具/技能名放在 _tool_name / _skill_name 一等字段上。"""
    summary = orchestrator.build_status_summary(
        body={"content": "正在执行第 1 步：text_stats"},
        metadata={
            "_progress_percent": 60,
            "_progress_stage": "tool_call",
            "_tool_name": "text_stats",
            "_skill_name": "text_stats",
            "_call_type": "skill",
        },
        event_type="status.processing",
    )
    assert summary["tools"] == ["text_stats"]
    assert summary["skills"] == ["text_stats"]
    assert summary["call_types"] == ["skill"]
    assert summary["stage"] == "tool_call"
    assert summary["percent"] == 60


def test_merge_status_unions_names_and_keeps_latest_stage():
    previous = {
        "stage": "tool_call",
        "percent": 60,
        "skills": ["text_stats"],
        "tools": ["text_stats"],
        "call_types": ["skill"],
    }
    current = {"stage": "finalizing", "percent": 95, "skills": [], "tools": [], "call_types": []}
    merged = orchestrator.merge_status(previous, current)
    # 阶段/百分比取最新
    assert merged["stage"] == "finalizing"
    assert merged["percent"] == 95
    # 技能/工具取并集，不会被 finalizing 事件抹掉
    assert merged["skills"] == ["text_stats"]
    assert merged["tools"] == ["text_stats"]
    assert merged["call_types"] == ["skill"]


def test_merge_status_falls_back_to_previous_when_current_is_empty():
    previous = {"stage": "tool_call", "percent": 30, "skills": ["text_stats"], "tools": [], "call_types": []}
    current = {"stage": None, "percent": None, "skills": [], "tools": [], "call_types": []}
    merged = orchestrator.merge_status(previous, current)
    assert merged["stage"] == "tool_call"
    assert merged["percent"] == 30
    assert merged["skills"] == ["text_stats"]


def test_status_events_accumulate_tool_names(db_session, default_conversation, fake_redis):
    """回归：连续两次进度事件后，缓存里仍能看到被调用的工具。"""
    conversation = default_conversation
    common = {"account_id": conversation.user_id, "chat_id": conversation.id}

    _run(
        orchestrator.handle_outbound_event(
            db_session,
            {**common, "content": "正在执行第 1 步：text_stats"},
            {"msg_id": "m-s1", "event": "status.processing"},
            {"_progress_percent": 60, "_progress_stage": "tool_call", "_tool_name": "text_stats"},
            "status.processing",
        )
    )
    _run(
        orchestrator.handle_outbound_event(
            db_session,
            {**common, "content": "整理结果"},
            {"msg_id": "m-s2", "event": "status.processing"},
            {"_progress_percent": 95, "_progress_stage": "finalizing"},
            "status.processing",
        )
    )

    cached = fake_redis.status[(conversation.user_id, conversation.id)]
    assert cached["stage"] == "finalizing"
    assert cached["tools"] == ["text_stats"]  # 未被 finalizing 覆盖


def test_reply_meta_carries_accumulated_tool_names(db_session, default_conversation, fake_redis):
    conversation = default_conversation
    common = {"account_id": conversation.user_id, "chat_id": conversation.id}
    _run(
        orchestrator.handle_outbound_event(
            db_session,
            {**common, "content": "执行技能"},
            {"msg_id": "m-t1", "event": "status.processing"},
            {"_progress_stage": "skill_call", "_skill_name": "text_stats", "_tool_name": "text_stats"},
            "status.processing",
        )
    )
    _run(
        orchestrator.handle_outbound_event(
            db_session,
            {**common, "content": "统计完成"},
            {"msg_id": "m-t2", "event": "message.reply"},
            {"request_msg_id": "u-tool-1"},
            "message.reply",
        )
    )
    messages, _ = repository.list_messages(db_session, conversation_id=conversation.id, limit=10)
    assert len(messages) == 1
    progress = messages[0]["meta"]["progress"]
    assert progress["skills"] == ["text_stats"]
    assert progress["tools"] == ["text_stats"]


def test_new_user_message_clears_accumulated_status(
    db_session, seeded_user, default_conversation, inbound_calls, fake_redis
):
    """新一轮对话不应继承上一轮暴露出的技能。"""
    conversation = default_conversation
    _run(
        orchestrator.handle_outbound_event(
            db_session,
            {"account_id": conversation.user_id, "chat_id": conversation.id, "content": "执行"},
            {"msg_id": "m-c1", "event": "status.processing"},
            {"_progress_stage": "skill_call", "_tool_name": "text_stats"},
            "status.processing",
        )
    )
    assert (conversation.user_id, conversation.id) in fake_redis.status

    _run(
        orchestrator.send_user_message(
            db_session, user=seeded_user, conversation=conversation, content="再来一次"
        )
    )
    assert (conversation.user_id, conversation.id) not in fake_redis.status
