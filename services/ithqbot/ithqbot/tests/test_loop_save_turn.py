import json

from ithqbot.agent.context import ContextBuilder
from ithqbot.agent.loop import AgentLoop
from ithqbot.agent.tools.base import ToolResult
from ithqbot.session.manager import Session
from ithqbot.skills.doc_compare.direct_handler import (
    _attachment_path_from_item,
    _collect_recent_doc_compare_paths,
    _extract_doc_paths_from_text,
    _has_recent_doc_compare_context,
    _is_doc_compare_request,
)


def _mk_loop() -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop._TOOL_RESULT_MAX_CHARS = AgentLoop._TOOL_RESULT_MAX_CHARS
    return loop


def test_save_turn_skips_multimodal_user_when_only_runtime_context() -> None:
    loop = _mk_loop()
    session = Session(key="test:runtime-only")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{"role": "user", "content": [{"type": "text", "text": runtime}]}],
        skip=0,
    )
    assert session.messages == []


def test_save_turn_keeps_image_placeholder_after_runtime_strip() -> None:
    loop = _mk_loop()
    session = Session(key="test:image")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": runtime},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }],
        skip=0,
    )
    assert session.messages[0]["content"] == [{"type": "text", "text": "[image]"}]


def test_save_turn_keeps_tool_results_under_16k() -> None:
    loop = _mk_loop()
    session = Session(key="test:tool-result")
    content = "x" * 12_000

    loop._save_turn(
        session,
        [{"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": content}],
        skip=0,
    )

    assert session.messages[0]["content"] == content


def test_looks_like_interim_only_catches_waiting_phrases() -> None:
    assert AgentLoop._looks_like_interim_only("请稍候，我正在获取最新天气数据。") is True
    assert AgentLoop._looks_like_interim_only("稍后给您结果") is True
    assert AgentLoop._looks_like_interim_only("合肥今天多云，最高 21°C。") is False


def test_normalize_progress_text_suppresses_long_markdown() -> None:
    text = "# 文档比较结果\n\n## 差异\n- A\n- B\n" + ("x" * 300)
    assert AgentLoop._normalize_progress_text(text) == "正在执行工具并汇总结果"
    assert AgentLoop._normalize_progress_text("短提示") == "短提示"


def test_parse_tool_event_payload_for_doc_compare() -> None:
    payload = {
        "_ithqbot_event": "doc_compare",
        "llm_result": "对比完成",
        "interaction": {"type": "otp"},
    }
    llm_result, parsed = AgentLoop._parse_tool_event_payload(json.dumps(payload, ensure_ascii=False))
    assert llm_result == "对比完成"
    assert isinstance(parsed, dict)
    assert parsed["interaction"]["type"] == "otp"


def test_parse_tool_event_payload_supports_tool_result() -> None:
    payload = ToolResult(
        content=json.dumps(
            {
                "_ithqbot_event": "doc_compare",
                "llm_result": "对比完成",
                "files": [{"file_id": "f1"}],
            },
            ensure_ascii=False,
        )
    )
    llm_result, parsed = AgentLoop._parse_tool_event_payload(payload)
    assert llm_result == "对比完成"
    assert isinstance(parsed, dict)
    assert parsed["files"][0]["file_id"] == "f1"


def test_extract_json_object_supports_fenced_json() -> None:
    text = """```json
{"action":"add","message":"吃饭时间到了！","every_seconds":180,"one_time":true}
```"""
    parsed = AgentLoop._extract_json_object(text)
    assert isinstance(parsed, dict)
    assert parsed["action"] == "add"


def test_collect_recent_doc_compare_paths_prefers_latest_unique_paths() -> None:
    history = [
        {
            "role": "user",
            "content": "upload 1",
            "metadata": {
                "attachments": [
                    {"storage": {"bucket": "ithqbot-storage", "path": "a/1.docx"}}
                ]
            },
        },
        {
            "role": "user",
            "content": "upload 2",
            "metadata": {
                "attachments": [
                    {"storage": {"bucket": "ithqbot-storage", "path": "a/2.docx"}}
                ]
            },
        },
    ]
    paths = _collect_recent_doc_compare_paths(history, None)
    assert paths == [
        "minio://ithqbot-storage/a/2.docx",
        "minio://ithqbot-storage/a/1.docx",
    ]


def test_attachment_path_from_item_adds_default_bucket_for_rel_storage_path() -> None:
    path = _attachment_path_from_item({"storage": {"path": "feishu_attachments/x/1.docx"}})
    assert path == "minio://ithqbot-storage/feishu_attachments/x/1.docx"


def test_has_recent_doc_compare_context_detects_compare_flow() -> None:
    history = [
        {"role": "assistant", "content": "请先输入验证码后下载对比结果"},
        {"role": "tool", "content": "doc_compare"},
    ]
    assert _has_recent_doc_compare_context(history) is True
    assert _has_recent_doc_compare_context([{"role": "assistant", "content": "普通回复"}]) is False


def test_extract_doc_paths_from_text_supports_minio_and_relative_attachment_path() -> None:
    text = (
        '{"path1":"minio://ithqbot-storage/feishu_attachments/a/1.docx",'
        '"path2":"feishu_attachments/b/2.docx"}'
    )
    paths = _extract_doc_paths_from_text(text)
    assert "minio://ithqbot-storage/feishu_attachments/a/1.docx" in paths
    assert "minio://ithqbot-storage/feishu_attachments/b/2.docx" in paths


def test_collect_recent_doc_compare_paths_reads_paths_from_recent_text() -> None:
    history = [
        {
            "role": "assistant",
            "content": '{"path1":"minio://ithqbot-storage/feishu_attachments/a/1.docx","path2":"minio://ithqbot-storage/feishu_attachments/b/2.docx"}',
        }
    ]
    paths = _collect_recent_doc_compare_paths(history, None)
    assert paths[:2] == [
        "minio://ithqbot-storage/feishu_attachments/a/1.docx",
        "minio://ithqbot-storage/feishu_attachments/b/2.docx",
    ]


def test_collect_recent_doc_compare_paths_reads_paths_from_current_message_text() -> None:
    paths = _collect_recent_doc_compare_paths(
        history=[],
        current_metadata=None,
        current_text='{"path1":"minio://ithqbot-storage/feishu_attachments/c/1.docx","path2":"minio://ithqbot-storage/feishu_attachments/d/2.docx"}',
    )
    assert paths[:2] == [
        "minio://ithqbot-storage/feishu_attachments/c/1.docx",
        "minio://ithqbot-storage/feishu_attachments/d/2.docx",
    ]


def test_collect_recent_doc_compare_paths_uses_cached_session_paths() -> None:
    paths = _collect_recent_doc_compare_paths(
        history=[],
        current_metadata=None,
        session_metadata={
            "last_doc_compare_paths": [
                "minio://ithqbot-storage/feishu_attachments/e/1.docx",
                "minio://ithqbot-storage/feishu_attachments/f/2.docx",
            ]
        },
    )
    assert paths[:2] == [
        "minio://ithqbot-storage/feishu_attachments/e/1.docx",
        "minio://ithqbot-storage/feishu_attachments/f/2.docx",
    ]


def test_is_doc_compare_request_matches_real_compare_intent() -> None:
    assert _is_doc_compare_request("请比较这两个文档的差异") is True
    assert _is_doc_compare_request("compare two pdf files") is True


def test_is_doc_compare_request_rejects_skill_compliance_intent() -> None:
    assert _is_doc_compare_request("帮我检查一下doc_compare skill是否符合规范？") is False
    assert _is_doc_compare_request("请做 doc_compare skill 的合规审计") is False
