"""Tests for cache-friendly prompt construction."""

from __future__ import annotations

from datetime import datetime as real_datetime
from importlib.resources import files as pkg_files
from pathlib import Path
import datetime as datetime_module

from ithqbot.agent.context import ContextBuilder


class _FakeDatetime(real_datetime):
    current = real_datetime(2026, 2, 24, 13, 59)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls.current


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    return workspace


def test_bootstrap_files_are_backed_by_templates() -> None:
    template_dir = pkg_files("ithqbot") / "templates"

    for filename in ContextBuilder.BOOTSTRAP_FILES:
        assert (template_dir / filename).is_file(), f"missing bootstrap template: {filename}"


def test_system_prompt_stays_stable_when_clock_changes(tmp_path, monkeypatch) -> None:
    """System prompt should not change just because wall clock minute changes."""
    monkeypatch.setattr(datetime_module, "datetime", _FakeDatetime)

    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    _FakeDatetime.current = real_datetime(2026, 2, 24, 13, 59)
    prompt1 = builder.build_system_prompt()

    _FakeDatetime.current = real_datetime(2026, 2, 24, 14, 0)
    prompt2 = builder.build_system_prompt()

    assert prompt1 == prompt2


def test_runtime_context_is_separate_untrusted_user_message(tmp_path) -> None:
    """Runtime metadata should be merged with the user message."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[],
        current_message="Return exactly: OK",
        channel="cli",
        chat_id="direct",
    )

    assert messages[0]["role"] == "system"
    assert "## Current Session" not in messages[0]["content"]

    # Runtime context is now merged with user message into a single message
    assert messages[-1]["role"] == "user"
    user_content = messages[-1]["content"]
    assert isinstance(user_content, str)
    assert ContextBuilder._RUNTIME_CONTEXT_TAG in user_content
    assert "Current Time:" in user_content
    assert "Channel: cli" in user_content
    assert "Chat ID: direct" in user_content
    assert "Return exactly: OK" in user_content


def test_build_messages_accepts_attachment_dict_media_without_crash(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[],
        current_message="check files",
        media=[
            {"kind": "file", "storage": {"path": "user_B/bot_A/a.xlsx"}},
            {"rel_path": "user_B/bot_A/b.xlsx"},
        ],
        channel="icatmsg",
        chat_id="bot_A",
    )

    assert messages[-1]["role"] == "user"
    assert isinstance(messages[-1]["content"], str)
    assert "check files" in messages[-1]["content"]


def test_build_messages_includes_all_attachment_paths_from_metadata(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[],
        current_message="请比较附件",
        media=[],
        metadata={
            "attachments": [
                {"kind": "file", "name": "附件1.xlsx", "storage": {"path": "user_B/bot_A/a1.xlsx"}},
                {"kind": "file", "name": "附件2.xlsx", "storage": {"path": "user_B/bot_A/a2.xlsx"}},
            ]
        },
    )

    content = messages[-1]["content"]
    assert isinstance(content, str)
    assert "[Attached Files]" in content
    assert "a1.xlsx" in content
    assert "a2.xlsx" in content
    assert "[Doc Compare Auto Selection]" in content


def test_build_messages_doc_compare_prefers_latest_same_name(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    history = [
        {
            "role": "user",
            "content": "上传老版本",
            "timestamp": "2026-03-27T10:00:00+00:00",
            "metadata": {
                "attachments": [
                    {"kind": "file", "name": "1.docx", "uploaded_at": "2999-01-01T10:00:00+00:00", "storage": {"path": "u1/c1/old/1.docx"}},
                ]
            },
        },
        {
            "role": "user",
            "content": "上传新版本",
            "timestamp": "2026-03-27T11:00:00+00:00",
            "metadata": {
                "attachments": [
                    {"kind": "file", "name": "1.docx", "uploaded_at": "2999-01-01T11:00:00+00:00", "storage": {"path": "u1/c1/new/1.docx"}},
                    {"kind": "file", "name": "2.docx", "uploaded_at": "2999-01-01T12:00:00+00:00", "storage": {"path": "u1/c1/new/2.docx"}},
                ]
            },
        },
    ]

    messages = builder.build_messages(
        history=history,
        current_message="比较两个文档",
        metadata={"account_id": "u1", "tenant_id": "t1"},
    )
    content = messages[-1]["content"]
    assert isinstance(content, str)
    assert "UploadedAt:" in content
    assert "Default Path 1: minio://ithqbot-storage/u1/c1/new/2.docx" in content
    assert "Default Path 2: minio://ithqbot-storage/u1/c1/new/1.docx" in content


def test_build_messages_doc_compare_window_and_latest_per_name(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ITHQBOT_DOC_COMPARE_WINDOW_NO_INTERACTION_MINUTES", "60")
    monkeypatch.setenv("ITHQBOT_DOC_COMPARE_WINDOW_WITH_INTERACTION_MINUTES", "20")
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    history = [
        {
            "role": "user",
            "content": "上传文件",
            "timestamp": "2026-03-27T10:00:00+00:00",
            "metadata": {
                "attachments": [
                    {"kind": "file", "name": "1.docx", "uploaded_at": "2999-01-01T10:00:00+00:00", "storage": {"path": "u1/c1/old/1.docx"}},
                    {"kind": "file", "name": "1.docx", "uploaded_at": "2999-01-01T11:00:00+00:00", "storage": {"path": "u1/c1/new/1.docx"}},
                    {"kind": "file", "name": "2.docx", "uploaded_at": "2999-01-01T12:00:00+00:00", "storage": {"path": "u1/c1/new/2.docx"}},
                ]
            },
        },
        {
            "role": "user",
            "content": "先帮我总结一下差异维度",
            "timestamp": "2026-03-27T10:30:00+00:00",
            "metadata": {},
        },
    ]

    messages = builder.build_messages(
        history=history,
        current_message="比较两个文档",
        metadata={"account_id": "u1", "tenant_id": "t1"},
    )
    content = messages[-1]["content"]
    assert isinstance(content, str)
    assert "Only keep recent files uploaded within 20 minutes." in content
    assert "u1/c1/new/1.docx" in content
    assert "u1/c1/old/1.docx" not in content


def test_system_prompt_always_uses_builtin_templates(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    marker = "WORKSPACE_ONLY_MARKER_ABC123"
    (workspace / "AGENTS.md").write_text(marker, encoding="utf-8")
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt()
    builtin_agents = (pkg_files("ithqbot") / "templates" / "AGENTS.md").read_text(encoding="utf-8")

    assert builtin_agents in prompt
    assert marker not in prompt
