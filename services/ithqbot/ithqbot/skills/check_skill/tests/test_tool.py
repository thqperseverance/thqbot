import asyncio
import textwrap
import zipfile

from ithqbot.skills.check_skill.tool.tool import CheckSkillTool


def test_check_skill_tool_happy_path(tmp_path):
    skill_dir = tmp_path / "ithqbot" / "skills" / "demo_skill"
    (skill_dir / "tool").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: demo_skill
            description: 用于演示检查流程；当用户要求合规检查时触发。
            ---

            # Demo Skill

            ## 目标
            demo

            ## 何时使用
            demo

            ## 输入约束
            demo

            ## 执行步骤
            demo

            ## 输出约定
            demo

            ## 风险与边界
            demo
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "tool" / "tool.py").write_text(
        textwrap.dedent(
            """\
            from typing import Any
            from ithqbot.agent.tools.base import Tool


            class DemoTool(Tool):
                @property
                def name(self) -> str:
                    return "demo_tool"

                @property
                def description(self) -> str:
                    return "demo"

                @property
                def parameters(self) -> dict[str, Any]:
                    return {"type": "object", "properties": {}}

                async def execute(self, **kwargs: Any) -> str:
                    return "ok"
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "tool" / "tool_def.json").write_text(
        textwrap.dedent(
            """\
            {
              "name": "demo_tool",
              "description": "demo",
              "input_schema": {
                "type": "object",
                "properties": {}
              }
            }
            """
        ),
        encoding="utf-8",
    )

    tool = CheckSkillTool(workspace=tmp_path)
    result = asyncio.run(tool.execute(skill_name="demo_skill"))

    assert "Skill: demo_skill" in result
    assert "FAIL:" in result
    assert "- (none)" in result


def test_check_skill_tool_missing_param(tmp_path):
    tool = CheckSkillTool(workspace=tmp_path)
    result = asyncio.run(tool.execute())
    assert "缺少必要参数 skill_name 或 bundle_path" in result


def test_check_skill_tool_validates_composable_metadata_and_contract(tmp_path):
    skill_dir = tmp_path / "ithqbot" / "skills" / "composable_skill"
    (skill_dir / "tool").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: composable_skill
            description: 生成会议摘要并给下游流程消费；当用户要求自动规划会议处理流程时触发。
            metadata:
              ithqbot:
                capability: ["document", "analysis"]
                tags: ["summary", "meeting"]
                level: atomic
                idempotent: true
                retryable: true
                cost:
                  level: low
                latency:
                  expected_ms: 1200
                planner:
                  output_to: ["task_generate"]
            ---

            ## 目标
            demo

            ## 何时使用
            demo

            ## 输入约束
            demo

            ## 执行步骤
            demo

            ## 输出约定
            demo

            ## 风险与边界
            demo
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "tool" / "tool.py").write_text(
        textwrap.dedent(
            """\
            from typing import Any
            from ithqbot.agent.tools.base import Tool


            class DemoTool(Tool):
                @property
                def name(self) -> str:
                    return "composable_skill"

                @property
                def description(self) -> str:
                    return "demo"

                @property
                def parameters(self) -> dict[str, Any]:
                    return {"type": "object", "properties": {}}

                async def execute(self, **kwargs: Any) -> str:
                    return "ok"
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "tool" / "tool_def.json").write_text(
        textwrap.dedent(
            """\
            {
              "name": "composable_skill",
              "description": "demo",
              "input_schema": {
                "type": "object",
                "properties": {}
              },
              "output_schema": {
                "type": "object",
                "properties": {
                  "summary": {"type": "string"}
                }
              },
              "semantic": {
                "produces": ["meeting_summary"],
                "consumes": ["meeting_transcript"]
              }
            }
            """
        ),
        encoding="utf-8",
    )

    tool = CheckSkillTool(workspace=tmp_path)
    result = asyncio.run(tool.execute(skill_name="composable_skill", strict=True))

    assert "metadata.ithqbot.capability 格式合法" in result
    assert "metadata.ithqbot.level 合法" in result
    assert "metadata.ithqbot.idempotent 合法" in result
    assert "metadata.ithqbot.cost.level 合法" in result
    assert "契约 output_schema.type=object" in result
    assert "契约 semantic.produces 格式合法" in result
    assert "FAIL:\n- (none)" in result


def test_check_skill_tool_handles_null_metadata_block(tmp_path):
    skill_dir = tmp_path / "ithqbot" / "skills" / "null_meta_skill"
    (skill_dir / "tool").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: null_meta_skill
            description: 用于校验 metadata.ithqbot 为空时不崩溃；当用户要求合规检查时触发。
            metadata:
              ithqbot:
            ---

            ## 目标
            demo

            ## 何时使用
            demo

            ## 输入约束
            demo

            ## 执行步骤
            demo

            ## 输出约定
            demo

            ## 风险与边界
            demo
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "tool" / "tool.py").write_text(
        textwrap.dedent(
            """\
            from typing import Any
            from ithqbot.agent.tools.base import Tool


            class DemoTool(Tool):
                @property
                def name(self) -> str:
                    return "null_meta_tool"

                @property
                def description(self) -> str:
                    return "demo"

                @property
                def parameters(self) -> dict[str, Any]:
                    return {"type": "object", "properties": {}}

                async def execute(self, **kwargs: Any) -> str:
                    return "ok"
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "tool" / "tool_def.json").write_text(
        textwrap.dedent(
            """\
            {
              "name": "null_meta_tool",
              "description": "demo",
              "input_schema": {
                "type": "object",
                "properties": {}
              }
            }
            """
        ),
        encoding="utf-8",
    )

    tool = CheckSkillTool(workspace=tmp_path)
    result = asyncio.run(tool.execute(skill_name="null_meta_skill"))

    assert "Skill: null_meta_skill" in result
    assert "建议在 frontmatter.metadata.ithqbot 中声明 capability/tags/level/planner" in result
    assert "错误：执行工具“check_skill”失败" not in result


def test_check_skill_tool_support_bundle_archive(tmp_path):
    bundle = tmp_path / "demo_skill.zip"
    with zipfile.ZipFile(bundle, mode="w") as zf:
        zf.writestr(
            "demo_skill/SKILL.md",
            textwrap.dedent(
                """\
                ---
                name: demo_skill
                description: 用于演示检查流程；当用户要求合规检查时触发。
                ---

                ## 目标
                demo

                ## 何时使用
                demo

                ## 输入约束
                demo

                ## 执行步骤
                demo

                ## 输出约定
                demo

                ## 风险与边界
                demo
                """
            ),
        )
        zf.writestr(
            "demo_skill/tool/tool.py",
            textwrap.dedent(
                """\
                from typing import Any
                from ithqbot.agent.tools.base import Tool

                class DemoTool(Tool):
                    @property
                    def name(self) -> str:
                        return "demo_tool"

                    @property
                    def description(self) -> str:
                        return "demo"

                    @property
                    def parameters(self) -> dict[str, Any]:
                        return {"type": "object", "properties": {}}

                    async def execute(self, **kwargs: Any) -> str:
                        return "ok"
                """
            ),
        )
        zf.writestr(
            "demo_skill/tool/tool_def.json",
            textwrap.dedent(
                """\
                {
                  "name": "demo_tool",
                  "description": "demo",
                  "input_schema": {
                    "type": "object",
                    "properties": {}
                  }
                }
                """
            ),
        )

    tool = CheckSkillTool(workspace=tmp_path)
    result = asyncio.run(tool.execute(bundle_path=str(bundle)))

    assert "Skill: demo_skill" in result
    assert "来源：bundle:demo_skill.zip" in result
    assert "FAIL:" in result
    assert "- (none)" in result


def test_check_skill_tool_detects_security_risk(tmp_path):
    skill_dir = tmp_path / "ithqbot" / "skills" / "risk_skill"
    (skill_dir / "tool").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: risk_skill
            description: 风险扫描用例。
            ---

            ## 目标
            demo

            ## 何时使用
            demo

            ## 输入约束
            demo

            ## 执行步骤
            demo

            ## 输出约定
            demo

            ## 风险与边界
            demo
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "tool" / "tool.py").write_text(
        textwrap.dedent(
            """\
            from typing import Any
            from ithqbot.agent.tools.base import Tool
            import jwt
            import ssl
            import subprocess
            import tempfile

            SECRET_TOKEN = "hardcoded-token"
            WEBHOOK_URL = "http://8.8.8.8/hook"
            DOCS_URL = "https://malicious.example.net/guide"

            class RiskTool(Tool):
                @property
                def name(self) -> str:
                    return "risk_tool"

                @property
                def description(self) -> str:
                    return "risk"

                @property
                def parameters(self) -> dict[str, Any]:
                    return {"type": "object", "properties": {}}

                async def execute(self, **kwargs: Any) -> str:
                    _ = ssl._create_unverified_context()
                    _ = jwt.decode("token", options={"verify_signature": False})
                    _ = tempfile.mktemp(prefix="risk-")
                    subprocess.run("echo test", shell=True, check=False)
                    return "ok"
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "tool" / "tool_def.json").write_text(
        textwrap.dedent(
            """\
            {
              "name": "risk_tool",
              "description": "risk",
              "input_schema": {
                "type": "object",
                "properties": {}
              }
            }
            """
        ),
        encoding="utf-8",
    )

    tool = CheckSkillTool(workspace=tmp_path)
    result = asyncio.run(tool.execute(skill_name="risk_skill"))
    assert "高危风险：检测到 subprocess shell=True" in result
    assert "高危风险：检测到疑似硬编码密钥" in result
    assert "高危风险：检测到明文 HTTP 外链" in result
    assert "高危风险：检测到 IP 直连外链" in result
    assert "高危风险：检测到 ssl._create_unverified_context" in result
    assert "高危风险：检测到 JWT 关闭签名校验" in result
    assert "中风险：检测到 tempfile.mktemp 调用" in result
    assert "中风险：检测到硬编码第三方外链（需评审）" in result


def test_check_skill_tool_ignores_test_directory_in_risk_scan(tmp_path):
    skill_dir = tmp_path / "ithqbot" / "skills" / "risk_from_tests_only"
    (skill_dir / "tool").mkdir(parents=True)
    (skill_dir / "tests").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: risk_from_tests_only
            description: 风险扫描忽略 tests 目录用例。
            ---

            ## 目标
            demo

            ## 何时使用
            demo

            ## 输入约束
            demo

            ## 执行步骤
            demo

            ## 输出约定
            demo

            ## 风险与边界
            demo
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "tool" / "tool.py").write_text(
        textwrap.dedent(
            """\
            from typing import Any
            from ithqbot.agent.tools.base import Tool

            class SafeTool(Tool):
                @property
                def name(self) -> str:
                    return "safe_tool"

                @property
                def description(self) -> str:
                    return "safe"

                @property
                def parameters(self) -> dict[str, Any]:
                    return {"type": "object", "properties": {}}

                async def execute(self, **kwargs: Any) -> str:
                    return "ok"
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "tool" / "tool_def.json").write_text(
        textwrap.dedent(
            """\
            {
              "name": "safe_tool",
              "description": "safe",
              "input_schema": {
                "type": "object",
                "properties": {}
              }
            }
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "tests" / "test_bad_case.py").write_text(
        textwrap.dedent(
            """\
            import subprocess

            WEBHOOK = "http://8.8.8.8/notify"

            def test_demo():
                subprocess.run("echo bad", shell=True, check=False)
                assert True
            """
        ),
        encoding="utf-8",
    )

    tool = CheckSkillTool(workspace=tmp_path)
    result = asyncio.run(tool.execute(skill_name="risk_from_tests_only"))
    assert "shell=True" not in result
    assert "明文 HTTP 外链" not in result
    assert "IP 直连外链" not in result
    assert "FAIL:\n- (none)" in result


def test_check_skill_tool_can_locate_builtin_skill_from_nested_workspace(tmp_path):
    tool = CheckSkillTool(workspace=tmp_path / "services" / "ithqbot" / "ithqbot" / "skills")
    result = asyncio.run(tool.execute(skill_name="doc_compare"))
    assert "未找到技能目录：doc_compare" not in result


def test_check_skill_tool_warns_when_progress_has_no_tracking_fields(tmp_path):
    skill_dir = tmp_path / "ithqbot" / "skills" / "trace_warn_skill"
    (skill_dir / "tool").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: trace_warn_skill
            description: 用于校验内部状态追踪字段。
            ---

            ## 目标
            demo

            ## 何时使用
            demo

            ## 输入约束
            demo

            ## 执行步骤
            demo

            ## 输出约定
            demo

            ## 风险与边界
            demo
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "tool" / "tool.py").write_text(
        textwrap.dedent(
            """\
            from typing import Any
            from ithqbot.agent.tools.base import Tool

            class TraceWarnTool(Tool):
                @property
                def name(self) -> str:
                    return "trace_warn_tool"

                @property
                def description(self) -> str:
                    return "trace_warn"

                @property
                def parameters(self) -> dict[str, Any]:
                    return {"type": "object", "properties": {}}

                async def execute(self, **kwargs: Any) -> str:
                    context = kwargs.get("context")
                    if context:
                        await context.emit_progress(20, "processing", "running")
                    return "ok"
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "tool" / "tool_def.json").write_text(
        textwrap.dedent(
            """\
            {
              "name": "trace_warn_tool",
              "description": "trace_warn",
              "input_schema": {
                "type": "object",
                "properties": {}
              }
            }
            """
        ),
        encoding="utf-8",
    )

    tool = CheckSkillTool(workspace=tmp_path)
    result = asyncio.run(tool.execute(skill_name="trace_warn_skill"))
    assert "tool.py 建议补充内部状态追踪字段" in result
