from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ithqbot.agent.memory import MemoryConsolidator
from ithqbot.bus.events import InboundMessage, OutboundMessage
from ithqbot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from ithqbot.agent.loop import AgentLoop

DOC_COMPARE_TOOL_NAME = "doc_compare"
DOC_COMPARE_MISSING_FILES_MESSAGE = "未检测到两个可用于比较的文档，请先上传两个文件后再试。"


def _is_skill_compliance_request(text: str | None) -> bool:
    raw = (text or "").strip().lower()
    if not raw:
        return False
    has_skill_scope = bool(
        re.search(
            r"(?:(?<![a-z0-9_])skill(?![a-z0-9_])|技能|skill_standards|skill\.md|规范文档)",
            raw,
            flags=re.IGNORECASE,
        )
    )
    if not has_skill_scope:
        return False
    return bool(
        re.search(
            r"(?:符合规范|是否符合|合规|规范检查|标准检查|审计|验收|compliance|standard)",
            raw,
            flags=re.IGNORECASE,
        )
    )


def _is_doc_compare_request(text: str | None) -> bool:
    raw = (text or "").strip().lower()
    if not raw:
        return False
    if _is_skill_compliance_request(raw):
        return False
    has_compare = bool(re.search(r"(比较|对比|\bcompare\b|\bdiff\b|\bdifference\b)", raw, flags=re.IGNORECASE))
    has_doc = bool(re.search(r"(文档|文件|\bdoc\b|\bdocs\b|\bdocument\b|\bpdf\b|\bword\b)", raw, flags=re.IGNORECASE))
    return has_compare and has_doc


def _attachment_path_from_item(item: Any) -> str | None:
    if isinstance(item, str):
        return item.strip() or None
    if not isinstance(item, dict):
        return None
    storage = item.get("storage")
    if isinstance(storage, dict):
        path = storage.get("path") or storage.get("url")
        bucket = storage.get("bucket")
        if isinstance(path, str) and path:
            if path.startswith(("minio://", "http://", "https://", "/")):
                return path
            if isinstance(bucket, str) and bucket:
                return f"minio://{bucket}/{path}"
            if "/" in path:
                return f"minio://ithqbot-storage/{path}"
            return path
    rel_path = item.get("rel_path")
    if isinstance(rel_path, str) and rel_path:
        bucket = item.get("bucket") or "ithqbot-storage"
        return rel_path if rel_path.startswith(("minio://", "http://", "https://", "/")) else f"minio://{bucket}/{rel_path}"
    return None


def _extract_doc_paths_from_text(text: str | None) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    paths: list[str] = []
    seen: set[str] = set()
    patterns = (
        r"minio://[^\s\"']+\.(?:docx|doc|pdf|txt|md)\b",
        r"/[^\s\"']+\.(?:docx|doc|pdf|txt|md)\b",
        r"feishu_attachments/[^\s\"']+\.(?:docx|doc|pdf|txt|md)\b",
    )
    for pattern in patterns:
        for match in re.findall(pattern, raw, flags=re.IGNORECASE):
            item = match.strip().rstrip(".,;)")
            if not item:
                continue
            if item.startswith("feishu_attachments/"):
                item = f"minio://ithqbot-storage/{item}"
            elif item.startswith("/") and "/feishu_attachments/" in item and not item.startswith("minio://"):
                item = f"minio://ithqbot-storage{item[item.find('/feishu_attachments/'):]}"
            if item in seen:
                continue
            seen.add(item)
            paths.append(item)
    return paths


def _collect_recent_doc_compare_paths(
    history: list[dict[str, Any]],
    current_metadata: dict[str, Any] | None,
    current_text: str | None = None,
    session_metadata: dict[str, Any] | None = None,
) -> list[str]:
    metas: list[dict[str, Any]] = []
    paths: list[str] = []
    seen: set[str] = set()

    for path in _extract_doc_paths_from_text(current_text):
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
        if len(paths) >= 8:
            return paths

    cached_paths = (session_metadata or {}).get("last_doc_compare_paths")
    if isinstance(cached_paths, list):
        for raw in cached_paths:
            path = raw.strip() if isinstance(raw, str) else ""
            if not path or path in seen:
                continue
            seen.add(path)
            paths.append(path)
            if len(paths) >= 8:
                return paths

    if isinstance(current_metadata, dict):
        metas.append(current_metadata)
    for msg in reversed(history[-12:]):
        if msg.get("role") != "user":
            continue
        meta = msg.get("metadata")
        if isinstance(meta, dict):
            metas.append(meta)
    for meta in metas:
        file_meta = meta.get("file_meta")
        candidate_items: list[Any] = []
        if isinstance(file_meta, dict):
            candidate_items.append(file_meta)
        attachments = meta.get("attachments")
        if isinstance(attachments, list):
            candidate_items.extend(attachments)
        for item in candidate_items:
            path = _attachment_path_from_item(item)
            if not isinstance(path, str) or not path:
                continue
            if path in seen:
                continue
            seen.add(path)
            paths.append(path)
            if len(paths) >= 8:
                return paths
    for msg in reversed(history[-16:]):
        content = msg.get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for chunk in content:
                if isinstance(chunk, dict):
                    text = chunk.get("text")
                    if isinstance(text, str):
                        texts.append(text)
        for text in texts:
            for path in _extract_doc_paths_from_text(text):
                if path in seen:
                    continue
                seen.add(path)
                paths.append(path)
                if len(paths) >= 8:
                    return paths
    return paths


def _looks_like_file_not_found_error(text: str | None) -> bool:
    raw = (text or "").lower()
    if not raw:
        return False
    return ("file not found" in raw) or ("文件不存在" in raw) or ("未找到文件" in raw)


def _has_recent_doc_compare_context(history: list[dict[str, Any]]) -> bool:
    for item in reversed(history[-10:]):
        if item.get("role") not in {"assistant", "tool"}:
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        text = content.lower()
        if any(token in text for token in ("doc_compare", "文档比对", "对比结果", "验证码", "otp")):
            return True
    return False


def _extract_otp_code(msg: InboundMessage) -> str | None:
    metadata = msg.metadata or {}
    interaction_response = metadata.get("interaction_response")
    if isinstance(interaction_response, dict):
        values = interaction_response.get("values")
        if isinstance(values, dict):
            otp = values.get("otp_code")
            if isinstance(otp, str) and otp.strip():
                return otp.strip()
        otp = interaction_response.get("otp_code")
        if isinstance(otp, str) and otp.strip():
            return otp.strip()
    text = (msg.content or "").strip()
    if re.fullmatch(r"\d{4,8}", text):
        return text
    return None


async def handle_direct_doc_compare(
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
    tool_name = DOC_COMPARE_TOOL_NAME
    direct_doc_compare = (
        isinstance(interaction_context, dict)
        and interaction_context.get("tool") == tool_name
    )
    otp_code = _extract_otp_code(msg)
    should_try_doc_compare = (
        direct_doc_compare
        or _is_doc_compare_request(msg.content)
        or (bool(otp_code) and _has_recent_doc_compare_context(history))
    )
    if not should_try_doc_compare or not loop.tools.get(tool_name):
        return None

    selected = _collect_recent_doc_compare_paths(
        history,
        msg.metadata if isinstance(msg.metadata, dict) else None,
        current_text=msg.content,
        session_metadata=session.metadata if isinstance(session.metadata, dict) else None,
    )
    route_metadata = dict(msg.metadata or {})
    route_metadata.setdefault("channel", msg.channel)
    route_metadata.setdefault("chat_id", msg.chat_id)
    route_metadata.setdefault("tenant_id", msg.tenant_id)
    route_metadata.setdefault("account_id", msg.account_id or msg.sender_id)
    route_metadata.setdefault("bot_id", msg.bot_id or route_metadata.get("bot_id") or loop._resolve_active_bot_id(route_metadata))

    candidate_pairs: list[tuple[str, str]] = []
    path1 = interaction_context.get("path1") if isinstance(interaction_context, dict) else None
    path2 = interaction_context.get("path2") if isinstance(interaction_context, dict) else None
    if isinstance(path1, str) and path1 and isinstance(path2, str) and path2:
        candidate_pairs.append((path1, path2))
    if len(selected) >= 2:
        for i in range(len(selected)):
            for j in range(i + 1, len(selected)):
                pair = (selected[i], selected[j])
                if pair not in candidate_pairs:
                    candidate_pairs.append(pair)
                if len(candidate_pairs) >= 8:
                    break
            if len(candidate_pairs) >= 8:
                break

    if candidate_pairs:
        download = bool(isinstance(interaction_context, dict) and interaction_context.get("download")) or bool(otp_code)
        final_content = "未能找到可用于对比的两个文档，请重新上传后再试。"
        tool_payload: dict[str, Any] | None = None
        selected_pair: tuple[str, str] | None = None
        for c_path1, c_path2 in candidate_pairs:
            tool_args: dict[str, Any] = {
                "path1": c_path1,
                "path2": c_path2,
                "download": download,
            }
            if otp_code:
                tool_args["otp_code"] = otp_code
            tool_raw_result, tool_error = await loop._execute_direct_tool_with_trace(
                tool_name=tool_name,
                tool_args=tool_args,
                route_metadata=route_metadata,
                on_progress=on_progress,
                fallback_progress=fallback_progress,
            )
            if tool_error:
                final_content = f"文档对比执行失败：{tool_error}"
                tool_payload = None
                continue
            parsed_content, parsed_payload = loop._parse_tool_event_payload(tool_raw_result)
            final_content, tool_payload = parsed_content, parsed_payload
            if not _looks_like_file_not_found_error(parsed_content):
                selected_pair = (c_path1, c_path2)
                break
        if selected_pair:
            session.metadata["last_doc_compare_paths"] = [selected_pair[0], selected_pair[1]]
        outbound_files: list[dict[str, Any]] | None = None
        outbound_interaction: dict[str, Any] | None = None
        if isinstance(tool_payload, dict):
            interaction = tool_payload.get("interaction")
            if isinstance(interaction, dict):
                outbound_interaction = interaction
                interaction_message = str(tool_payload.get("interaction_message") or "").strip()
                if not str(tool_payload.get("llm_result") or "").strip() and interaction_message:
                    final_content = interaction_message
            files = tool_payload.get("files")
            if isinstance(files, list) and files:
                outbound_files = files
                final_content = str(
                    tool_payload.get("llm_result")
                    or tool_payload.get("file_message")
                    or "结果文件已生成。"
                )
        reply_to = (
            msg.reply_to
            if hasattr(msg, "reply_to")
            else None
        ) or route_metadata.get("request_msg_id") or route_metadata.get("parent_msg_id")
        all_msgs = [
            *history,
            {"role": "user", "content": msg.content, "metadata": msg.metadata},
            {"role": "tool", "tool_call_id": f"direct_{tool_name}", "name": tool_name, "content": final_content},
            {"role": "assistant", "content": final_content},
        ]
        outbound_metadata = build_clean_outbound_metadata()
        if isinstance(outbound_interaction, dict):
            outbound_metadata["interaction"] = outbound_interaction
        if isinstance(outbound_files, list) and outbound_files:
            outbound_metadata["files"] = outbound_files
            outbound_metadata["content_type"] = "file"
        for key in ("_bot_guardrails_turn_hits", "_bot_guardrails_last_hit", "_bot_guardrails_blocked", "_routing"):
            if key in route_metadata:
                outbound_metadata[key] = route_metadata[key]
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

    fallback_content = DOC_COMPARE_MISSING_FILES_MESSAGE
    reply_to = (
        msg.reply_to
        if hasattr(msg, "reply_to")
        else None
    ) or (msg.metadata or {}).get("request_msg_id") or (msg.metadata or {}).get("parent_msg_id")
    all_msgs = [
        *history,
        {"role": "user", "content": msg.content, "metadata": msg.metadata},
        {"role": "assistant", "content": fallback_content},
    ]
    loop._save_turn(session, all_msgs, len(history))
    loop._persist_session_state(sessions, memory_consolidator, session)
    loop._schedule_background(memory_consolidator.maybe_consolidate_by_tokens(session))
    return OutboundMessage(
        channel=msg.channel,
        chat_id=msg.chat_id,
        content=fallback_content,
        account_id=msg.account_id,
        tenant_id=msg.tenant_id,
        bot_id=msg.bot_id,
        reply_to=reply_to,
        metadata=build_clean_outbound_metadata(),
    )
