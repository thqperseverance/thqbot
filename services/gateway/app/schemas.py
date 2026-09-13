"""请求/响应模型（前端契约）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class UserOut(BaseModel):
    user_id: str
    username: str
    display_name: str = ""


class LoginResponse(BaseModel):
    user: UserOut


class ConversationOut(BaseModel):
    conversation_id: str
    title: str
    bot_id: str
    unread_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    last_message_at: str | None = None
    last_message_preview: str = ""


class CreateConversationRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    bot_id: str | None = Field(default=None, max_length=64)


class MessageOut(BaseModel):
    message_id: str
    conversation_id: str
    sender: Literal["user", "bot"]
    content: str = ""
    content_type: str = "text"
    status: str = "sent"
    request_msg_id: str | None = None
    reply_to: str | None = None
    created_at: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class MessagePage(BaseModel):
    messages: list[MessageOut]
    paging: dict[str, Any] = Field(default_factory=dict)


class SendMessageRequest(BaseModel):
    # 允许只发附件不带文字，因此 content 可为空；"两者都空"由路由层拒绝。
    content: str = Field(default="", max_length=32_000)
    client_msg_id: str | None = Field(default=None, max_length=64)
    file_ids: list[str] = Field(default_factory=list, max_length=10)


class FileOut(BaseModel):
    file_id: str
    name: str
    mime: str
    size: int
    storage_uri: str
    download_url: str
    created_at: str | None = None


class DeleteMessagesRequest(BaseModel):
    message_ids: list[str] = Field(..., min_length=1, max_length=200)


class SendMessageResponse(BaseModel):
    message: MessageOut
    queued: bool = True
    degraded: bool = False
    warning: str | None = None


class SessionContextOut(BaseModel):
    user: UserOut
    bot_id: str
    tenant_id: str
    total_unread_count: int = 0
    server_time: str | None = None


class RuntimeConfigOut(BaseModel):
    """前端运行配置（模型下拉、权限提示、附件上限）。"""

    model: str
    models: list[str] = Field(default_factory=list)
    bot_id: str
    tenant_id: str
    workspace_restricted: bool = True
    max_upload_mb: int = 20


class HealthResponse(BaseModel):
    status: str
    checks: dict[str, Any] = Field(default_factory=dict)
