from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ithqbot.agent.memory import MemoryConsolidator
from ithqbot.bus.events import InboundMessage, OutboundMessage
from ithqbot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from ithqbot.agent.loop import AgentLoop

OFFICIAL_DOC_REVIEW_TOOL_NAME = "official_doc_review"


def _is_official_doc_review_request(text: str | None) -> bool:
    raw = (text or "").strip().lower()
    if not raw:
        return False
    has_review_intent = bool(
        re.search(
            r"(公文智审|智审|审查|审核|审校|审阅|校对|质检|检查|排查|格式审查|格式检查|错别字|敏感词|合规|规范|符合.*公文要求|公文要求)",
            raw,
            flags=re.IGNORECASE,
        )
    )
    if not has_review_intent:
        return False
    return bool(
        re.search(
            r"(公文|oa\s*文|oa文|文档|附件|请示|通知|报告|纪要|这个|该|上传|刚发|刚传|刚上传)",
            raw,
            flags=re.IGNORECASE,
        )
    )


def _is_report_download_request(text: str | None) -> bool:
    """Detect follow-up requests asking for the full report or download link."""
    raw = (text or "").strip().lower()
    if not raw:
        return False
    return bool(
        re.search(
            r"(下载|导出|生成报告|完整报告|审查报告|全部问题|剩余问题|全部展开|提供下载|报告.*下载|下载.*报告)",
            raw,
            flags=re.IGNORECASE,
        )
    )


def _entry_to_review_target(loop: "AgentLoop", entry: dict[str, Any]) -> dict[str, Any] | None:
    file_id = str(entry.get("file_id") or "").strip()
    storage_uri = str(entry.get("storage_uri") or entry.get("s3_uri") or entry.get("minio_uri") or "").strip()
    rel_path = str(entry.get("rel_path") or "").strip().strip("/")
    file_name = str(entry.get("name") or entry.get("file_name") or "").strip()

    if not storage_uri:
        storage = entry.get("storage")
        if isinstance(storage, dict):
            default_backend = str(getattr(getattr(loop.file_service, "_config", None), "backend", "minio") or "minio").strip()
            default_bucket = str(getattr(loop.file_service, "_bucket", "") or "").strip()
            backend = str(storage.get("backend") or default_backend).strip() or default_backend
            bucket = str(storage.get("bucket") or default_bucket).strip()
            path = str(storage.get("path") or storage.get("object_key") or "").strip().strip("/")
            if bucket and path:
                storage_uri = f"{backend}://{bucket}/{path}"

    if not (file_id or storage_uri or rel_path):
        return None

    payload: dict[str, Any] = {}
    if file_id:
        payload["file_id"] = file_id
    if storage_uri:
        payload["storage_uri"] = storage_uri
    elif rel_path:
        payload["rel_path"] = rel_path
    if file_name:
        payload["file_name"] = file_name
    return payload


def _coerce_uploaded_timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(value, (int, float)):
        ts = float(value)
        return ts / 1000.0 if ts > 1e12 else ts
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return float("-inf")
    return float("-inf")


def _extract_entry_uploaded_timestamp(entry: dict[str, Any], metadata: dict[str, Any] | None) -> float:
    if isinstance(entry, dict):
        for key in ("uploaded_at", "upload_time", "timestamp"):
            ts = _coerce_uploaded_timestamp(entry.get(key))
            if ts != float("-inf"):
                return ts
        storage = entry.get("storage")
        if isinstance(storage, dict):
            for key in ("uploaded_at", "upload_time", "timestamp"):
                ts = _coerce_uploaded_timestamp(storage.get(key))
                if ts != float("-inf"):
                    return ts
    if isinstance(metadata, dict):
        for key in ("uploaded_at", "upload_time", "timestamp"):
            ts = _coerce_uploaded_timestamp(metadata.get(key))
            if ts != float("-inf"):
                return ts
    return float("-inf")


def _collect_recent_review_targets(
    loop: "AgentLoop",
    history: list[dict[str, Any]],
    current_metadata: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    def _targets_from_meta(meta: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(meta, dict):
            return []
        ranked_targets: list[tuple[float, int, dict[str, Any]]] = []
        current_seen: set[str] = set()
        for index, entry in enumerate(loop._collect_file_entries_from_metadata(meta)):
            target = _entry_to_review_target(loop, entry)
            if not isinstance(target, dict):
                continue
            key = str(target.get("file_id") or target.get("storage_uri") or target.get("rel_path") or "").strip()
            if not key or key in current_seen:
                continue
            current_seen.add(key)
            ranked_targets.append((_extract_entry_uploaded_timestamp(entry, meta), index, target))
        ranked_targets.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in ranked_targets]

    current_targets = _targets_from_meta(current_metadata)
    if current_targets:
        return current_targets

    latest_history_targets: list[dict[str, Any]] = []
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        meta = item.get("metadata")
        history_targets = _targets_from_meta(meta if isinstance(meta, dict) else None)
        if history_targets:
            latest_history_targets = history_targets
            break

    return latest_history_targets


async def _collect_storage_review_targets(
    loop: "AgentLoop",
    *,
    query: str,
    route_metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    selected = await loop._select_recent_file_from_storage(
        tenant_id=str(route_metadata.get("tenant_id") or ""),
        account_id=str(route_metadata.get("account_id") or ""),
        bot_id=str(route_metadata.get("bot_id") or ""),
        query=query,
        limit=30,
        default_to_latest=True,
    )
    if not isinstance(selected, dict):
        return []
    target: dict[str, Any] = {}
    file_id = str(selected.get("file_id") or "").strip()
    storage_uri = str(selected.get("storage_uri") or selected.get("s3_uri") or selected.get("minio_uri") or "").strip()
    rel_path = str(selected.get("rel_path") or "").strip()
    file_name = str(selected.get("original_file_name") or selected.get("name") or selected.get("file_name") or "").strip()
    uploaded_at = str(selected.get("uploaded_at") or selected.get("created_at") or "").strip()
    if file_id:
        target["file_id"] = file_id
    if storage_uri:
        target["storage_uri"] = storage_uri
    elif rel_path:
        target["rel_path"] = rel_path
    if file_name:
        target["file_name"] = file_name
    if uploaded_at:
        target["uploaded_at"] = uploaded_at
    return [target] if target else []


async def handle_direct_official_doc_review(
    *,
    loop: "AgentLoop",
    msg: InboundMessage,
    history: list[dict[str, Any]],
    session: Session,
    sessions: SessionManager,
    memory_consolidator: MemoryConsolidator,
    interaction_context: dict[str, Any] | None,
    on_progress: Callable[[str], Awaitable[None]] | None,
    fallback_progress: Callable[..., Awaitable[None]],
    build_clean_outbound_metadata: Callable[[], dict[str, Any]],
) -> OutboundMessage | None:
    tool_name = OFFICIAL_DOC_REVIEW_TOOL_NAME
    direct_official_doc_review = (
        isinstance(interaction_context, dict)
        and interaction_context.get("tool") == tool_name
    )
    should_try_review = direct_official_doc_review or _is_official_doc_review_request(msg.content)
    is_report_request = _is_report_download_request(msg.content)
    should_try_review = should_try_review or is_report_request
    if not should_try_review or not loop.tools.get(tool_name):
        return None

    from loguru import logger
    logger.info("[OfficialDocReview] Handling direct review request. User query: {}", msg.content)

    route_metadata = dict(msg.metadata or {})
    route_metadata.setdefault("channel", msg.channel)
    route_metadata.setdefault("chat_id", msg.chat_id)
    route_metadata.setdefault("tenant_id", msg.tenant_id)
    route_metadata.setdefault("account_id", msg.account_id or msg.sender_id)
    route_metadata.setdefault("bot_id", msg.bot_id or route_metadata.get("bot_id") or loop._resolve_active_bot_id(route_metadata))

    selected_targets = _collect_recent_review_targets(
        loop,
        history,
        msg.metadata if isinstance(msg.metadata, dict) else None,
    )
    if not selected_targets:
        selected_targets = await _collect_storage_review_targets(
            loop,
            query=msg.content,
            route_metadata=route_metadata,
        )
    if not selected_targets:
        return None

    tool_args = dict(selected_targets[0])
    if is_report_request:
        tool_args["generate_report"] = True
    if isinstance(interaction_context, dict):
        review_dimensions = interaction_context.get("review_dimensions")
        if isinstance(review_dimensions, list) and review_dimensions:
            tool_args["review_dimensions"] = review_dimensions
        if "generate_report" in interaction_context:
            tool_args["generate_report"] = bool(interaction_context.get("generate_report"))

    await loop._emit_progress(
        on_progress or fallback_progress,
        "识别到公文审查请求，正在进行智审",
        progress_percent=35,
        progress_kind="tool_hint",
        progress_stage="skill_call",
        skill_name=tool_name,
        call_type="skill",
        status_details={
            "execution": {
                "executor_skill": tool_name,
                "mode": "direct",
                "source_ref": tool_args.get("file_id") or tool_args.get("storage_uri") or tool_args.get("rel_path"),
            }
        },
    )

    tool_raw_result, tool_error = await loop._execute_direct_tool_with_trace(
        tool_name=tool_name,
        tool_args=tool_args,
        route_metadata=route_metadata,
        on_progress=on_progress,
        fallback_progress=fallback_progress,
    )

    outbound_files = None
    if tool_error:
        final_content = f"公文智审执行失败：{tool_error}"
    else:
        final_content, parsed_obj = loop._parse_tool_event_payload(tool_raw_result)
        if final_content == "工具调用已处理完成。":
            final_content = "公文智审已完成。"

        if isinstance(parsed_obj, dict):
            outbound_files = await loop._build_outbound_file_entries(
                parsed_obj.get("files"),
                tenant_id=str(route_metadata.get("tenant_id") or ""),
                account_id=str(route_metadata.get("account_id") or ""),
                bot_id=str(route_metadata.get("bot_id") or ""),
            )

    reply_to = (
        msg.reply_to
        if hasattr(msg, "reply_to")
        else None
    ) or route_metadata.get("request_msg_id") or route_metadata.get("parent_msg_id")
    all_msgs = [
        *history,
        {"role": "user", "content": msg.content, "metadata": msg.metadata},
        {
            "role": "tool",
            "tool_call_id": "direct_official_doc_review",
            "name": tool_name,
            "content": str(tool_raw_result or final_content),
        },
        {"role": "assistant", "content": final_content},
    ]
    outbound_metadata = build_clean_outbound_metadata()
    if isinstance(outbound_files, list) and outbound_files:
        outbound_metadata["files"] = outbound_files
        outbound_metadata["content_type"] = "file"
    loop._save_turn(session, all_msgs, len(history))
    loop._persist_session_state(sessions, memory_consolidator, session)
    loop._schedule_background(memory_consolidator.maybe_consolidate_by_tokens(session))
    return OutboundMessage(
        channel=msg.channel,
        chat_id=msg.chat_id,
        content=final_content,
        account_id=msg.account_id,
        tenant_id=msg.tenant_id,
        bot_id=msg.bot_id,
        reply_to=reply_to,
        metadata=outbound_metadata,
    )
