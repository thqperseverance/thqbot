from contextvars import ContextVar, Token
from typing import Any

account_id: ContextVar[str | None] = ContextVar("account_id", default=None)
tenant_id: ContextVar[str | None] = ContextVar("tenant_id", default=None)
bot_id: ContextVar[str | None] = ContextVar("bot_id", default=None)
channel: ContextVar[str | None] = ContextVar("channel", default=None)
chat_id: ContextVar[str | None] = ContextVar("chat_id", default=None)
source_channel: ContextVar[str | None] = ContextVar("source_channel", default=None)
client_id: ContextVar[str | None] = ContextVar("client_id", default=None)
message_id: ContextVar[str | None] = ContextVar("message_id", default=None)
parent_msg_id: ContextVar[str | None] = ContextVar("parent_msg_id", default=None)
request_msg_id: ContextVar[str | None] = ContextVar("request_msg_id", default=None)
trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
event_type: ContextVar[str | None] = ContextVar("event_type", default=None)
contract_version: ContextVar[str | None] = ContextVar("contract_version", default=None)
content_type: ContextVar[str | None] = ContextVar("content_type", default=None)
inbound_metadata: ContextVar[dict[str, Any]] = ContextVar("inbound_metadata", default={})
outbound_media: ContextVar[list[Any]] = ContextVar("outbound_media", default=[])
llm_call_source: ContextVar[str] = ContextVar("llm_call_source", default="bot")
llm_call_component: ContextVar[str | None] = ContextVar("llm_call_component", default=None)


def _pick_str(meta: dict[str, Any], key: str) -> str | None:
    value = meta.get(key)
    return value if isinstance(value, str) and value else None


def set_runtime_context(
    *,
    account: str | None,
    tenant: str | None,
    bot: str | None,
    channel_name: str | None,
    chat: str | None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Token]:
    meta = dict(metadata or {})
    return {
        "account_id": account_id.set(account),
        "tenant_id": tenant_id.set(tenant),
        "bot_id": bot_id.set(bot),
        "channel": channel.set(channel_name),
        "chat_id": chat_id.set(chat),
        "source_channel": source_channel.set(_pick_str(meta, "source_channel") or channel_name),
        "client_id": client_id.set(_pick_str(meta, "client_id")),
        "message_id": message_id.set(_pick_str(meta, "message_id")),
        "parent_msg_id": parent_msg_id.set(_pick_str(meta, "parent_msg_id")),
        "request_msg_id": request_msg_id.set(_pick_str(meta, "request_msg_id")),
        "trace_id": trace_id.set(_pick_str(meta, "trace_id")),
        "event_type": event_type.set(_pick_str(meta, "event_type")),
        "contract_version": contract_version.set(_pick_str(meta, "contract_version")),
        "content_type": content_type.set(_pick_str(meta, "content_type")),
        "inbound_metadata": inbound_metadata.set(meta),
        "outbound_media": outbound_media.set([]),
    }


def reset_runtime_context(tokens: dict[str, Token]) -> None:
    outbound_media.reset(tokens["outbound_media"])
    inbound_metadata.reset(tokens["inbound_metadata"])
    content_type.reset(tokens["content_type"])
    contract_version.reset(tokens["contract_version"])
    event_type.reset(tokens["event_type"])
    trace_id.reset(tokens["trace_id"])
    request_msg_id.reset(tokens["request_msg_id"])
    parent_msg_id.reset(tokens["parent_msg_id"])
    message_id.reset(tokens["message_id"])
    client_id.reset(tokens["client_id"])
    source_channel.reset(tokens["source_channel"])
    chat_id.reset(tokens["chat_id"])
    channel.reset(tokens["channel"])
    bot_id.reset(tokens["bot_id"])
    tenant_id.reset(tokens["tenant_id"])
    account_id.reset(tokens["account_id"])


def get_runtime_context() -> dict[str, Any]:
    meta = dict(inbound_metadata.get() or {})
    return {
        "account_id": account_id.get(),
        "tenant_id": tenant_id.get(),
        "bot_id": bot_id.get(),
        "channel": channel.get(),
        "chat_id": chat_id.get(),
        "source_channel": source_channel.get(),
        "client_id": client_id.get(),
        "message_id": message_id.get(),
        "parent_msg_id": parent_msg_id.get(),
        "request_msg_id": request_msg_id.get(),
        "trace_id": trace_id.get(),
        "event_type": event_type.get(),
        "contract_version": contract_version.get(),
        "content_type": content_type.get(),
        "attachments": meta.get("attachments"),
        "file_meta": meta.get("file_meta"),
        "metadata": meta,
    }


def set_llm_call_context(*, source: str, component: str | None = None) -> dict[str, Token]:
    src = source if source in {"bot", "skill", "tool", "mcp"} else "bot"
    return {
        "llm_call_source": llm_call_source.set(src),
        "llm_call_component": llm_call_component.set(component),
    }


def reset_llm_call_context(tokens: dict[str, Token]) -> None:
    llm_call_component.reset(tokens["llm_call_component"])
    llm_call_source.reset(tokens["llm_call_source"])


def get_llm_call_context() -> dict[str, Any]:
    return {
        "source": llm_call_source.get(),
        "component": llm_call_component.get(),
    }
