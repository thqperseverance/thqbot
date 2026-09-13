"""Message tool for sending messages to users."""

from typing import Any, Awaitable, Callable

from ithqbot import context
from ithqbot.agent.tools.base import Tool
from ithqbot.bus.events import OutboundMessage


class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
        default_metadata: dict[str, Any] | None = None,
    ):
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._default_message_id = default_message_id
        self._default_metadata = default_metadata or {}
        self._sent_in_turn: bool = False
        self._sent_contents_in_turn: list[str] = []

    def set_context(self, channel: str, chat_id: str, message_id: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        """Set the current message context."""
        self._default_channel = channel
        self._default_chat_id = chat_id
        self._default_message_id = message_id
        self._default_metadata = metadata or {}

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    def start_turn(self) -> None:
        """Reset per-turn send tracking."""
        self._sent_in_turn = False
        self._sent_contents_in_turn = []

    def should_suppress_auto_reply(self, final_content: str | None) -> bool:
        if not self._sent_in_turn:
            return False
        text = (final_content or "").strip()
        if not text:
            return True
        return text in self._sent_contents_in_turn

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return "Send a message to the user. Use this when you want to communicate something."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content to send"
                },
                "channel": {
                    "type": "string",
                    "description": "Optional: target channel (telegram, discord, etc.)"
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional: target chat/user ID"
                },
                "media": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: list of file paths to attach (images, audio, documents)"
                }
            },
            "required": ["content"]
        }

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        media: list[str] | None = None,
        cancellation_token: Any = None,
        **kwargs: Any
    ) -> str:
        self.throw_if_cancelled(cancellation_token)
        runtime_ctx = context.get_runtime_context()
        channel = channel or runtime_ctx.get("channel") or self._default_channel
        chat_id = chat_id or runtime_ctx.get("chat_id") or self._default_chat_id
        message_id = message_id or runtime_ctx.get("message_id") or self._default_message_id

        if not channel or not chat_id:
            return "错误：未指定目标频道或会话。"

        if not self._send_callback:
            return "错误：消息发送能力未配置。"

        meta = dict(runtime_ctx.get("metadata") or self._default_metadata)
        if message_id:
            meta["message_id"] = message_id

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            media=media or [],
            metadata=meta,
        )

        try:
            await self._send_callback(msg)
            self.throw_if_cancelled(cancellation_token)
            if channel == self._default_channel and chat_id == self._default_chat_id:
                self._sent_in_turn = True
                self._sent_contents_in_turn.append(content.strip())
            media_info = f" with {len(media)} attachments" if media else ""
            return f"Message sent to {channel}:{chat_id}{media_info}"
        except Exception as e:
            return f"错误：消息发送失败：{str(e)}"
