"""内置技能的发现与工具注册回归测试。

这层测试保证：
1. ``SkillsLoader`` 能从内置目录发现技能（含新增的 ``text_stats``）；
2. ``discover_python_tools`` 会把 ``tool/tool.py`` 里的 ``Tool`` 子类注册成可调用工具
   —— 也就是"模型能调 skills"的机制本身。
"""

from __future__ import annotations

from pathlib import Path

from ithqbot.agent.skills.loader import SkillsLoader


class _FakeRegistry:
    """只记录注册结果，避免依赖 ToolRegistry 的内部实现。"""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}
        self.direct_handlers: dict[str, object] = {}

    def register(self, tool, direct_handler=None) -> None:  # noqa: ANN001
        self.tools[tool.name] = tool
        self.direct_handlers[tool.name] = direct_handler


def _loader(workspace: Path) -> SkillsLoader:
    return SkillsLoader(workspace, enabled_skills=["*"])


def test_builtin_skills_are_discovered(tmp_path: Path):
    loader = _loader(tmp_path)
    names = {item["name"] for item in loader.list_skills(filter_unavailable=False)}
    assert "text_stats" in names
    assert "check_skill" in names
    assert "doc_compare" in names


def test_text_stats_metadata_is_valid(tmp_path: Path):
    meta = _loader(tmp_path).get_skill_metadata("text_stats")
    assert meta is not None
    assert meta["name"] == "text_stats"
    assert "统计" in meta["description"]


def test_discover_python_tools_registers_text_stats(tmp_path: Path):
    registry = _FakeRegistry()
    _loader(tmp_path).discover_python_tools(registry)
    assert "text_stats" in registry.tools

    tool = registry.tools["text_stats"]
    assert tool.parameters["required"] == ["text"]  # type: ignore[attr-defined]
    assert tool.description  # type: ignore[attr-defined]


def test_discover_python_tools_registers_existing_skills(tmp_path: Path):
    registry = _FakeRegistry()
    _loader(tmp_path).discover_python_tools(registry)
    # 存量技能同样可被注册（回归保护）
    assert "check_skill" in registry.tools


def test_workspace_skills_take_precedence(tmp_path: Path):
    """工作区同名技能应覆盖内置技能。"""
    skill_dir = tmp_path / "skills" / "text_stats"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: text_stats\ndescription: 工作区覆盖版本\n---\n覆盖正文\n",
        encoding="utf-8",
    )
    loader = _loader(tmp_path)
    meta = loader.get_skill_metadata("text_stats")
    assert meta is not None
    assert meta["description"] == "工作区覆盖版本"
