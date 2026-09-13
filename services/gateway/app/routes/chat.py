"""会话与消息 API（前端契约）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from .. import orchestrator, repository, schemas
from ..config import get_settings
from ..db import get_db
from ..deps import current_user
from ..models import User

router = APIRouter(prefix="/api", tags=["chat"])


def _user_out(user: User) -> schemas.UserOut:
    return schemas.UserOut(
        user_id=user.id, username=user.username, display_name=user.display_name or user.username
    )


@router.get("/session/context", response_model=schemas.SessionContextOut)
def session_context(user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    # 保证用户至少有一个会话，前端进来就能发消息
    repository.get_or_create_default_conversation(
        session, user_id=user.id, bot_id=settings.agent_bot_id
    )
    session.commit()
    return schemas.SessionContextOut(
        user=_user_out(user),
        bot_id=settings.agent_bot_id,
        tenant_id=settings.agent_tenant_id,
        total_unread_count=repository.total_unread(session, user.id),
        server_time=orchestrator._now_iso(),
    )


@router.get("/config", response_model=schemas.RuntimeConfigOut)
def runtime_config(user: User = Depends(current_user)):
    """前端运行配置：模型清单、租户、权限提示与附件上限。

    模型名只用于展示（真实调用在 worker 侧读 ``APP_LLM_MODEL``）；
    ``APP_LLM_MODELS`` 给出可选清单，留空则下拉里只有当前模型。
    """
    settings = get_settings()
    models = [item.strip() for item in (settings.llm_models or "").split(",") if item.strip()]
    # 当前模型固定排第一并去重，前端下拉的默认项就是它
    if settings.llm_model:
        models = [settings.llm_model] + [item for item in models if item != settings.llm_model]
    return schemas.RuntimeConfigOut(
        model=settings.llm_model,
        models=models,
        bot_id=settings.agent_bot_id,
        tenant_id=settings.agent_tenant_id,
        workspace_restricted=settings.agent_workspace_restricted,
        max_upload_mb=max(1, settings.max_upload_bytes // (1024 * 1024)),
    )


@router.get("/conversations", response_model=list[schemas.ConversationOut])
def list_conversations(
    bot_id: str | None = Query(default=None),
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
):
    settings = get_settings()
    target_bot = bot_id or settings.agent_bot_id
    repository.get_or_create_default_conversation(session, user_id=user.id, bot_id=target_bot)
    session.commit()
    rows = repository.list_conversations(session, user.id, bot_id=target_bot)
    return [schemas.ConversationOut(**row) for row in rows]


@router.post("/conversations", response_model=schemas.ConversationOut, status_code=status.HTTP_201_CREATED)
def create_conversation(
    payload: schemas.CreateConversationRequest,
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
):
    settings = get_settings()
    conversation = repository.create_conversation(
        session,
        user_id=user.id,
        bot_id=payload.bot_id or settings.agent_bot_id,
        title=payload.title,
    )
    session.commit()
    return schemas.ConversationOut(
        conversation_id=conversation.id,
        title=conversation.title,
        bot_id=conversation.bot_id,
        unread_count=conversation.unread_count,
        last_message_preview="",
    )


@router.get("/conversations/{conversation_id}/messages", response_model=schemas.MessagePage)
def list_messages(
    conversation_id: str,
    limit: int = Query(default=40, ge=1, le=200),
    before_id: str | None = Query(default=None),
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
):
    try:
        repository.get_owned_conversation(session, user_id=user.id, conversation_id=conversation_id)
    except repository.NotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    messages, paging = repository.list_messages(
        session, conversation_id=conversation_id, limit=limit, before_id=before_id
    )
    return schemas.MessagePage(messages=[schemas.MessageOut(**m) for m in messages], paging=paging)


@router.post("/conversations/{conversation_id}/messages", response_model=schemas.SendMessageResponse)
async def send_message(
    conversation_id: str,
    payload: schemas.SendMessageRequest,
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
):
    content = (payload.content or "").strip()
    file_ids = [str(item).strip() for item in (payload.file_ids or []) if str(item or "").strip()]
    if not content and not file_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="消息内容与附件不能同时为空",
        )
    if len(file_ids) > get_settings().max_attachments_per_message:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"单条消息最多 {get_settings().max_attachments_per_message} 个附件",
        )

    try:
        conversation = repository.get_owned_conversation(
            session, user_id=user.id, conversation_id=conversation_id
        )
    except repository.NotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")

    try:
        result = await orchestrator.send_user_message(
            session,
            user=user,
            conversation=conversation,
            content=content,
            client_msg_id=payload.client_msg_id,
            file_ids=file_ids,
        )
    except repository.NotFound:
        # 附件不存在或不属于当前用户
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="附件不存在或不属于当前用户"
        )

    message = result.get("message")
    if message:
        message_out = schemas.MessageOut(**message)
    else:
        message_out = _placeholder_message(
            conversation_id, content, attachments=[{"file_id": fid} for fid in file_ids]
        )
    return schemas.SendMessageResponse(
        message=message_out,
        queued=bool(result.get("queued")),
        degraded=bool(result.get("degraded")),
        warning=result.get("warning"),
    )


def _placeholder_message(
    conversation_id: str, content: str, attachments: list[dict[str, Any]] | None = None
) -> schemas.MessageOut:
    """极端情况下（用户消息被去重丢弃）也返回一个可渲染的对象。"""
    return schemas.MessageOut(
        message_id="",
        conversation_id=conversation_id,
        sender="user",
        content=content,
        status="duplicate",
        meta={"attachments": attachments} if attachments else {},
    )


@router.post("/conversations/{conversation_id}/read", response_model=schemas.ConversationOut)
def mark_read(
    conversation_id: str,
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
):
    try:
        conversation = repository.get_owned_conversation(
            session, user_id=user.id, conversation_id=conversation_id
        )
    except repository.NotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    payload = repository.mark_read(session, conversation)
    session.commit()
    return schemas.ConversationOut(last_message_preview="", **payload)


@router.delete("/conversations/{conversation_id}/messages")
def delete_messages(
    conversation_id: str,
    payload: schemas.DeleteMessagesRequest,
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
):
    try:
        repository.get_owned_conversation(session, user_id=user.id, conversation_id=conversation_id)
    except repository.NotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    deleted = repository.delete_messages(
        session, conversation_id=conversation_id, message_ids=payload.message_ids
    )
    session.commit()
    return {"status": "ok", "deleted": deleted}
