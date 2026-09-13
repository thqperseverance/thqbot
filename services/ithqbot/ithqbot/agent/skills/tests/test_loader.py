import textwrap

from ithqbot.agent.skills import SkillsLoader
from ithqbot.scripts.validate_skill_compliance import check_skill_compliance


class _Registry:
    def __init__(self) -> None:
        self.items = []
        self.direct_handlers = {}

    def register(self, tool, direct_handler=None) -> None:
        self.items.append(tool)
        if callable(direct_handler):
            self.direct_handlers[tool.name] = direct_handler


def test_skills_loader_prefers_workspace_skill_and_surfaces_tool_def(tmp_path):
    workspace_skill_dir = tmp_path / "skills" / "demo"
    workspace_skill_dir.mkdir(parents=True)
    (workspace_skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: demo
            description: Workspace demo skill for local overrides.
            ---

            # Demo
            """
        ),
        encoding="utf-8",
    )
    (workspace_skill_dir / "tool_def.json").write_text(
        textwrap.dedent(
            """\
            {
              "name": "demo_tool",
              "description": "Workspace skill contract",
              "input_schema": {
                "type": "object",
                "properties": {
                  "foo": {"type": "string"},
                  "count": {"type": "integer"}
                },
                "required": ["foo"]
              }
            }
            """
        ),
        encoding="utf-8",
    )

    builtin_dir = tmp_path / "builtin-skills"
    builtin_skill_dir = builtin_dir / "demo"
    builtin_skill_dir.mkdir(parents=True)
    (builtin_skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: demo
            description: Builtin demo skill.
            ---

            # Builtin Demo
            """
        ),
        encoding="utf-8",
    )

    loader = SkillsLoader(tmp_path, builtin_skills_dir=builtin_dir)

    loaded = loader.load_skill("demo")
    summary = loader.build_skills_summary()
    listed = loader.list_skills(filter_unavailable=False)

    assert loaded is not None
    assert "Workspace demo skill" in loaded
    assert any(item["name"] == "demo" and item["source"] == "workspace" for item in listed)
    assert "<source>workspace</source>" in summary
    assert "<tool>demo_tool</tool>" in summary
    assert "<input_hint>foo:string required, count:integer</input_hint>" in summary


def test_skills_loader_prefers_capability_file_and_normalizes_semantic(tmp_path):
    workspace_skill_dir = tmp_path / "skills" / "demo_capability"
    workspace_skill_dir.mkdir(parents=True)
    (workspace_skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: demo_capability
            description: Workspace capability-first skill.
            ---

            # Demo Capability
            """
        ),
        encoding="utf-8",
    )
    (workspace_skill_dir / "capability.json").write_text(
        textwrap.dedent(
            """\
            {
              "name": "demo.run",
              "description": "Capability-first contract",
              "version": "1.0.0",
              "namespace": "demo",
              "category": "atomic",
              "tags": ["demo"],
              "input_schema": {
                "type": "object",
                "properties": {
                  "task": {"type": "string"},
                  "count": {"type": "integer"}
                },
                "required": ["task"]
              },
              "output_schema": {
                "type": "object",
                "properties": {
                  "status": {"type": "string"}
                }
              },
              "semantic": {
                "produces": [
                  {"name": "task_result"}
                ],
                "consumes": [
                  {"name": "task_input"}
                ]
              },
              "routing": {
                "type": "internal",
                "service": "demo"
              },
              "effects": {
                "type": "read",
                "resources": ["demo_store"]
              },
              "idempotent": true,
              "retryable": true
            }
            """
        ),
        encoding="utf-8",
    )
    (workspace_skill_dir / "tool_def.json").write_text(
        textwrap.dedent(
            """\
            {
              "name": "legacy_demo_tool",
              "description": "Legacy tool contract",
              "input_schema": {
                "type": "object",
                "properties": {
                  "legacy": {"type": "string"}
                }
              },
              "semantic": {
                "produces": ["legacy_output"],
                "consumes": ["legacy_input"]
              }
            }
            """
        ),
        encoding="utf-8",
    )

    loader = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtin-skills")

    tool_def = loader._load_tool_def("demo_capability")
    summary = loader.build_skills_summary()

    assert tool_def["name"] == "demo.run"
    assert tool_def["description"] == "Capability-first contract"
    assert tool_def["input_schema"]["required"] == ["task"]
    assert tool_def["semantic"] == {
        "produces": ["task_result"],
        "consumes": ["task_input"],
    }
    assert "<tool>demo.run</tool>" in summary
    assert "<input_hint>task:string required, count:integer</input_hint>" in summary


def test_skills_loader_discovers_workspace_tool_py(tmp_path):
    workspace_skill_dir = tmp_path / "skills" / "demo_runtime"
    workspace_skill_dir.mkdir(parents=True)
    (workspace_skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: demo_runtime
            description: Runtime demo skill.
            ---

            # Demo Runtime
            """
        ),
        encoding="utf-8",
    )
    (workspace_skill_dir / "tool.py").write_text(
        textwrap.dedent(
            """\
            from typing import Any
            from ithqbot.agent.tools.base import Tool


            class DemoRuntimeTool(Tool):
                def __init__(self, workspace):
                    self.workspace = workspace

                @property
                def name(self) -> str:
                    return "demo_runtime"

                @property
                def description(self) -> str:
                    return "demo runtime tool"

                @property
                def parameters(self) -> dict[str, Any]:
                    return {"type": "object", "properties": {}}

                async def execute(self, **kwargs: Any) -> str:
                    return str(self.workspace)
            """
        ),
        encoding="utf-8",
    )

    loader = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtin-skills")
    registry = _Registry()

    loader.discover_python_tools(registry, workspace=tmp_path)

    assert len(registry.items) == 1
    assert registry.items[0].name == "demo_runtime"
    assert registry.items[0].workspace == tmp_path


def test_skills_loader_registers_explicit_direct_handler(tmp_path):
    workspace_skill_dir = tmp_path / "skills" / "demo_direct"
    workspace_skill_dir.mkdir(parents=True)
    (workspace_skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: demo_direct
            description: Runtime demo skill with direct route.
            ---

            # Demo Direct
            """
        ),
        encoding="utf-8",
    )
    (workspace_skill_dir / "tool.py").write_text(
        textwrap.dedent(
            """\
            from typing import Any
            from ithqbot.agent.tools.base import Tool


            async def demo_direct_handler(**kwargs: Any) -> str:
                return "ok"


            class DemoDirectTool(Tool):
                @property
                def name(self) -> str:
                    return "demo_direct"

                @property
                def description(self) -> str:
                    return "demo direct tool"

                @property
                def parameters(self) -> dict[str, Any]:
                    return {"type": "object", "properties": {}}

                @property
                def direct_handler(self):
                    return demo_direct_handler

                async def execute(self, **kwargs: Any) -> str:
                    return "ok"
            """
        ),
        encoding="utf-8",
    )

    loader = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtin-skills")
    registry = _Registry()

    loader.discover_python_tools(registry, workspace=tmp_path)

    assert callable(registry.direct_handlers["demo_direct"])


def test_skills_loader_supports_dataclass_slots_tool_module(tmp_path):
    workspace_skill_dir = tmp_path / "skills" / "demo_dataclass_slots"
    workspace_skill_dir.mkdir(parents=True)
    (workspace_skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: demo_dataclass_slots
            description: Runtime demo skill with dataclass slots.
            ---

            # Demo Dataclass Slots
            """
        ),
        encoding="utf-8",
    )
    (workspace_skill_dir / "tool.py").write_text(
        textwrap.dedent(
            """\
            from dataclasses import dataclass
            from typing import Any
            from ithqbot.agent.tools.base import Tool


            @dataclass(slots=True)
            class Payload:
                name: str


            class DemoDataclassSlotsTool(Tool):
                def __init__(self) -> None:
                    self.payload = Payload(name="demo")

                @property
                def name(self) -> str:
                    return "demo_dataclass_slots"

                @property
                def description(self) -> str:
                    return "demo tool using dataclass slots"

                @property
                def parameters(self) -> dict[str, Any]:
                    return {"type": "object", "properties": {}}

                async def execute(self, **kwargs: Any) -> str:
                    return self.payload.name
            """
        ),
        encoding="utf-8",
    )

    loader = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtin-skills")
    registry = _Registry()

    loader.discover_python_tools(registry, workspace=tmp_path)

    assert len(registry.items) == 1
    assert registry.items[0].name == "demo_dataclass_slots"


def test_skills_loader_discovers_only_enabled_python_tools(tmp_path):
    enabled_dir = tmp_path / "skills" / "enabled_skill"
    enabled_dir.mkdir(parents=True)
    (enabled_dir / "SKILL.md").write_text("# Enabled\n", encoding="utf-8")
    (enabled_dir / "tool.py").write_text(
        textwrap.dedent(
            """\
            from typing import Any
            from ithqbot.agent.tools.base import Tool


            class EnabledTool(Tool):
                @property
                def name(self) -> str:
                    return "enabled_tool"

                @property
                def description(self) -> str:
                    return "enabled"

                @property
                def parameters(self) -> dict[str, Any]:
                    return {"type": "object", "properties": {}}

                async def execute(self, **kwargs: Any) -> str:
                    return "ok"
            """
        ),
        encoding="utf-8",
    )

    disabled_dir = tmp_path / "skills" / "disabled_skill"
    disabled_dir.mkdir(parents=True)
    (disabled_dir / "SKILL.md").write_text("# Disabled\n", encoding="utf-8")
    (disabled_dir / "tool.py").write_text(
        textwrap.dedent(
            """\
            raise RuntimeError("disabled skill should not be imported")
            """
        ),
        encoding="utf-8",
    )

    loader = SkillsLoader(
        tmp_path,
        builtin_skills_dir=tmp_path / "builtin-skills",
        enabled_skills=["enabled_skill"],
    )
    registry = _Registry()

    loader.discover_python_tools(registry)

    assert [tool.name for tool in registry.items] == ["enabled_tool"]


def test_skills_loader_continues_when_one_python_tool_fails(tmp_path):
    broken_dir = tmp_path / "skills" / "broken_skill"
    broken_dir.mkdir(parents=True)
    (broken_dir / "SKILL.md").write_text("# Broken\n", encoding="utf-8")
    (broken_dir / "tool.py").write_text(
        textwrap.dedent(
            """\
            raise RuntimeError("boom")
            """
        ),
        encoding="utf-8",
    )

    healthy_dir = tmp_path / "skills" / "healthy_skill"
    healthy_dir.mkdir(parents=True)
    (healthy_dir / "SKILL.md").write_text("# Healthy\n", encoding="utf-8")
    (healthy_dir / "tool.py").write_text(
        textwrap.dedent(
            """\
            from typing import Any
            from ithqbot.agent.tools.base import Tool


            class HealthyTool(Tool):
                @property
                def name(self) -> str:
                    return "healthy_tool"

                @property
                def description(self) -> str:
                    return "healthy"

                @property
                def parameters(self) -> dict[str, Any]:
                    return {"type": "object", "properties": {}}

                async def execute(self, **kwargs: Any) -> str:
                    return "ok"
            """
        ),
        encoding="utf-8",
    )

    loader = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtin-skills")
    registry = _Registry()

    loader.discover_python_tools(registry)

    assert [tool.name for tool in registry.items] == ["healthy_tool"]


def test_skills_loader_treats_null_ithqbot_metadata_as_empty_dict(tmp_path):
    skill_dir = tmp_path / "skills" / "null_meta"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: null_meta
            description: Skill with null ithqbot metadata.
            metadata:
              ithqbot:
            ---

            # Null Meta
            """
        ),
        encoding="utf-8",
    )

    loader = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtin-skills")

    assert loader._get_skill_meta("null_meta") == {}
    assert loader.list_skills(filter_unavailable=True)[0]["name"] == "null_meta"
    assert loader.get_always_skills() == []


def test_skills_loader_treats_null_requires_as_empty_dict(tmp_path):
    skill_dir = tmp_path / "skills" / "null_requires"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: null_requires
            description: Skill with null requires metadata.
            metadata:
              ithqbot:
                requires:
            ---

            # Null Requires
            """
        ),
        encoding="utf-8",
    )

    loader = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtin-skills")

    assert loader._get_skill_meta("null_requires") == {"requires": None}
    assert loader.list_skills(filter_unavailable=True)[0]["name"] == "null_requires"
    summary = loader.build_skills_summary()
    assert "null_requires" in summary
    assert 'available="true"' in summary


def test_skill_compliance_validator_accepts_tool_subclass(tmp_path):
    skill_file = tmp_path / "tool.py"
    skill_file.write_text(
        textwrap.dedent(
            """\
            from typing import Any
            from ithqbot.agent.tools.base import Tool


            class DemoTool(Tool):
                @property
                def name(self) -> str:
                    return "demo"

                @property
                def description(self) -> str:
                    return "demo"

                @property
                def parameters(self) -> dict[str, Any]:
                    return {"type": "object", "properties": {}}

                async def execute(self, value: str) -> str:
                    return value
            """
        ),
        encoding="utf-8",
    )

    assert check_skill_compliance(skill_file) == []


def test_skill_compliance_validator_accepts_base_python_skill(tmp_path):
    skill_file = tmp_path / "implementation.py"
    skill_file.write_text(
        textwrap.dedent(
            """\
            from typing import Any
            from ithqbot.agent.skills.base import BasePythonSkill


            class DemoSkill(BasePythonSkill):
                async def execute(self, context: Any, payload: dict[str, Any]) -> dict[str, Any]:
                    return {"status": "success", "data": payload}
            """
        ),
        encoding="utf-8",
    )

    assert check_skill_compliance(skill_file) == []
