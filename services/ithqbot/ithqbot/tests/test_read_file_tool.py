"""``read_file`` 工具的单元测试（D13=A）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ithqbot.agent.skills.loader import BUILTIN_SKILLS_DIR
from ithqbot.agent.tools.files import DEFAULT_MAX_BYTES, ReadFileTool


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "skills" / "demo").mkdir(parents=True)
    (ws / "notes.md").write_text("第一行\n第二行\n第三行\n", encoding="utf-8")
    (ws / "data.json").write_text('{"a": 1}', encoding="utf-8")
    (ws / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 演示技能\n---\n技能正文\n", encoding="utf-8"
    )
    (ws / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    return ws


def _tool(ws: Path, **kwargs) -> ReadFileTool:
    return ReadFileTool(workspace=ws, **kwargs)


async def test_reads_relative_file_inside_workspace(workspace: Path):
    result = await _tool(workspace).execute(path="notes.md")
    assert result.success is True
    assert "第一行" in result.content
    assert str(workspace / "notes.md") in result.content
    assert result.metadata["total_lines"] >= 3


async def test_reads_file_in_subdirectory(workspace: Path):
    result = await _tool(workspace).execute(path="skills/demo/SKILL.md")
    assert result.success is True
    assert "技能正文" in result.content


async def test_reads_builtin_skill_md_outside_workspace(workspace: Path):
    """内置技能在 Python 包内（工作区之外），必须能读到 —— 这是 D13 的核心诉求。"""
    assert BUILTIN_SKILLS_DIR.exists(), "内置技能目录应存在"
    target = BUILTIN_SKILLS_DIR / "summarize" / "SKILL.md"
    assert target.exists(), target

    tool = _tool(workspace, restrict_to_workspace=True, extra_readable_roots=[BUILTIN_SKILLS_DIR])
    result = await tool.execute(path=str(target))
    assert result.success is True
    assert "summarize" in result.content


async def test_builtin_skill_denied_without_extra_root(workspace: Path):
    """没把内置目录列入白名单时，越界必须被拒绝。"""
    target = BUILTIN_SKILLS_DIR / "summarize" / "SKILL.md"
    result = await _tool(workspace, restrict_to_workspace=True).execute(path=str(target))
    assert result.success is False
    assert "越界" in (result.error or "")


async def test_relative_path_traversal_is_blocked(workspace: Path):
    result = await _tool(workspace).execute(path="../../etc/passwd")
    assert result.success is False
    assert "越界" in (result.error or "")


async def test_absolute_path_outside_workspace_is_blocked(workspace: Path, tmp_path: Path):
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret", encoding="utf-8")
    result = await _tool(workspace).execute(path=str(outside))
    assert result.success is False
    assert "越界" in (result.error or "")


async def test_unrestricted_tool_allows_outside_paths(workspace: Path, tmp_path: Path):
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret", encoding="utf-8")
    result = await _tool(workspace, restrict_to_workspace=False).execute(path=str(outside))
    assert result.success is True
    assert "top secret" in result.content


async def test_missing_file_reports_failure(workspace: Path):
    result = await _tool(workspace).execute(path="nope.md")
    assert result.success is False
    assert "不存在" in (result.error or "")


async def test_directory_is_rejected(workspace: Path):
    result = await _tool(workspace).execute(path="skills")
    assert result.success is False
    assert "目录" in (result.error or "")


async def test_binary_suffix_is_rejected(workspace: Path):
    result = await _tool(workspace).execute(path="blob.bin")
    assert result.success is False
    assert "不支持" in (result.error or "")


async def test_empty_path_is_rejected(workspace: Path):
    result = await _tool(workspace).execute(path="   ")
    assert result.success is False


async def test_line_range_selection(workspace: Path):
    result = await _tool(workspace).execute(path="notes.md", start_line=2, end_line=3)
    assert result.success is True
    body = result.content.split("\n\n", 1)[1]
    assert body == "第二行\n第三行"
    assert result.metadata["line_start"] == 2
    assert result.metadata["line_end"] == 3


async def test_truncation_is_reported(workspace: Path):
    big = "x" * (DEFAULT_MAX_BYTES + 100)
    (workspace / "big.txt").write_text(big, encoding="utf-8")
    result = await _tool(workspace).execute(path="big.txt")
    assert result.success is True
    assert result.metadata["truncated"] is True
    assert "已截断" in result.content


async def test_explicit_max_bytes(workspace: Path):
    result = await _tool(workspace).execute(path="notes.md", max_bytes=3)
    assert result.metadata["truncated"] is True
    assert result.metadata["bytes_read"] == 3


def test_tool_contract_shape(workspace: Path):
    tool = _tool(workspace)
    assert tool.name == "read_file"
    assert tool.description
    schema = tool.parameters
    assert schema["type"] == "object"
    assert schema["required"] == ["path"]
    assert set(schema["properties"]) == {"path", "start_line", "end_line", "max_bytes"}


def test_tool_exposes_workspace_attribute_for_loop_sync(workspace: Path):
    """loop._set_tool_workspace 依赖 tool._workspace 属性。"""
    tool = _tool(workspace)
    assert isinstance(tool._workspace, Path)


def test_resolve_path_rejects_empty_string(workspace: Path):
    tool = _tool(workspace)
    with pytest.raises(ValueError):
        tool.resolve_path("")
