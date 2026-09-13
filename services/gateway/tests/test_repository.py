"""PostgreSQL 数据访问层的单元测试（在 SQLite 上跑，模型跨方言兼容）。"""

from __future__ import annotations

import pytest

from app import repository
from app.security import hash_password


@pytest.fixture()
def other_user(db_session):
    user = repository.create_user(
        db_session,
        username="bob",
        password_hash=hash_password("bob-pass", iterations=1000),
        display_name="Bob",
    )
    db_session.commit()
    return user


def test_create_and_fetch_user(db_session, seeded_user):
    fetched = repository.get_user(db_session, seeded_user.id)
    assert fetched is not None
    assert fetched.username == "alice"
    assert repository.get_user_by_username(db_session, "alice").id == seeded_user.id
    assert repository.get_user_by_username(db_session, "nobody") is None
    assert repository.get_user(db_session, "") is None
    assert repository.count_users(db_session) >= 1


def test_default_conversation_is_stable(db_session, seeded_user):
    first = repository.get_or_create_default_conversation(db_session, user_id=seeded_user.id, bot_id="bot_A")
    db_session.commit()
    second = repository.get_or_create_default_conversation(db_session, user_id=seeded_user.id, bot_id="bot_A")
    assert first.id == second.id


def test_append_and_list_messages_paging(db_session, default_conversation):
    conversation = default_conversation
    for index in range(5):
        repository.append_message(
            db_session, conversation=conversation, sender="user", content=f"m{index}"
        )
    db_session.commit()

    page1, paging1 = repository.list_messages(db_session, conversation_id=conversation.id, limit=3)
    assert [m["content"] for m in page1] == ["m2", "m3", "m4"]  # 返回升序
    assert paging1["has_more"] is True
    assert paging1["oldest_message_id"] == page1[0]["message_id"]

    page2, paging2 = repository.list_messages(
        db_session, conversation_id=conversation.id, limit=3, before_id=page1[0]["message_id"]
    )
    assert [m["content"] for m in page2] == ["m0", "m1"]
    assert paging2["has_more"] is False


def test_append_message_is_idempotent_by_external_id(db_session, default_conversation):
    conversation = default_conversation
    first = repository.append_message(
        db_session,
        conversation=conversation,
        sender="bot",
        content="hello",
        external_id="m-dup-1",
    )
    db_session.commit()
    assert first is not None

    second = repository.append_message(
        db_session,
        conversation=conversation,
        sender="bot",
        content="hello again",
        external_id="m-dup-1",
    )
    db_session.commit()
    assert second is None  # 唯一约束命中

    messages, _ = repository.list_messages(db_session, conversation_id=conversation.id, limit=50)
    assert [m["content"] for m in messages] == ["hello"]


def test_append_message_increments_unread(db_session, default_conversation):
    conversation = default_conversation
    repository.append_message(
        db_session, conversation=conversation, sender="bot", content="pong", increment_unread=True
    )
    db_session.commit()
    assert conversation.unread_count == 1
    assert repository.total_unread(db_session, conversation.user_id) == 1

    repository.mark_read(db_session, conversation)
    db_session.commit()
    assert conversation.unread_count == 0
    assert repository.total_unread(db_session, conversation.user_id) == 0


def test_owned_conversation_enforces_ownership(db_session, default_conversation, other_user):
    # 本人可访问
    assert (
        repository.get_owned_conversation(
            db_session, user_id=default_conversation.user_id, conversation_id=default_conversation.id
        ).id
        == default_conversation.id
    )
    # 他人不可访问，且错误信息不区分"不存在"与"不属于你"
    with pytest.raises(repository.NotFound):
        repository.get_owned_conversation(
            db_session, user_id=other_user.id, conversation_id=default_conversation.id
        )
    with pytest.raises(repository.NotFound):
        repository.get_owned_conversation(db_session, user_id=other_user.id, conversation_id="missing")


def test_rename_conversation_only_when_default(db_session, default_conversation):
    conversation = default_conversation
    repository.rename_conversation_if_default(db_session, conversation, "帮我总结这份文档")
    db_session.commit()
    assert conversation.title == "帮我总结这份文档"

    # 已有标题就不再覆盖
    repository.rename_conversation_if_default(db_session, conversation, "另一句话")
    assert conversation.title == "帮我总结这份文档"


def test_delete_messages(db_session, default_conversation):
    conversation = default_conversation
    first = repository.append_message(db_session, conversation=conversation, sender="user", content="a")
    second = repository.append_message(db_session, conversation=conversation, sender="bot", content="b")
    db_session.commit()

    deleted = repository.delete_messages(
        db_session, conversation_id=conversation.id, message_ids=[first.id, "does-not-exist"]
    )
    db_session.commit()
    assert deleted == 1

    remaining, _ = repository.list_messages(db_session, conversation_id=conversation.id, limit=50)
    assert [m["message_id"] for m in remaining] == [second.id]
    assert repository.delete_messages(db_session, conversation_id=conversation.id, message_ids=[]) == 0


def test_list_conversations_includes_preview(db_session, seeded_user, default_conversation):
    repository.append_message(
        db_session, conversation=default_conversation, sender="user", content="  你好   世界  "
    )
    db_session.commit()
    rows = repository.list_conversations(db_session, seeded_user.id)
    assert len(rows) == 1
    assert rows[0]["last_message_preview"] == "你好 世界"
    assert rows[0]["conversation_id"] == default_conversation.id


def test_merge_meta_is_deep_copied(db_session):
    original = {"a": 1}
    merged = repository.merge_meta(original, {"b": 2})
    assert merged == {"a": 1, "b": 2}
    assert original == {"a": 1}
