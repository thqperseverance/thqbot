from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Protocol

from ithqbot.agent.skills.loader import BUILTIN_SKILLS_DIR
from ithqbot.agent.tools.cron import CronTool
from ithqbot.agent.tools.files import ReadFileTool
from ithqbot.agent.tools.message import MessageTool
from ithqbot.agent.tools.protocol import ToolRegistryProtocol
from ithqbot.agent.tools.web import WebFetchTool, WebSearchTool

if TYPE_CHECKING:
    from ithqbot.agent.loop import AgentLoop


class ToolRegistrar(Protocol):
    def __call__(self, registry: ToolRegistryProtocol) -> None: ...


def build_default_tool_registrars(loop: "AgentLoop") -> list[ToolRegistrar]:
    fs_workspace, minio_workspace = loop._resolve_tool_workspaces(loop.workspace)

    def register_builtin_tools(registry: ToolRegistryProtocol) -> None:
        def _register(tool) -> None:
            if loop._is_tool_enabled_by_config(tool.name):
                registry.register(tool)

        _register(WebSearchTool(config=loop.web_search_config, proxy=loop.web_proxy))
        _register(WebFetchTool(proxy=loop.web_proxy))
        _register(MessageTool(send_callback=loop.bus.publish_outbound))
        # D13=A：补齐 read_file，否则 prompt 型 / Composite 技能无法读取自己的 SKILL.md。
        # 可读根目录需要包含三类：
        #   1) fs_workspace —— 构建注册表时的基础工作区（运行期可能被切到按用户隔离的
        #      共享工作区，届时 _set_tool_workspace 会改写 tool._workspace，
        #      所以这里显式保留基础工作区，保证其中的文档仍可读）；
        #   2) 内置技能目录（位于 Python 包内，在工作区之外）；
        #   3) 额外的可读根（例如工作区内的示例文档目录）。
        _register(
            ReadFileTool(
                workspace=fs_workspace,
                restrict_to_workspace=loop.restrict_to_workspace,
                extra_readable_roots=[
                    fs_workspace,
                    BUILTIN_SKILLS_DIR,
                    getattr(loop.skills, "builtin_skills", None),
                ],
            )
        )
        if loop.cron_service:
            _register(CronTool(loop.cron_service))

    def register_skill_tools(registry: ToolRegistryProtocol) -> None:
        loop.skills.discover_python_tools(
            registry,
            workspace=loop.workspace,
            config=loop.config,
            provider_factory=loop.provider_factory,
        )

    def register_storage_tools(registry: ToolRegistryProtocol) -> None:
        from ithqbot.agent.tools.storage import StorageFetchTool, StoragePushTool

        storage_args = {
            "backend": getattr(loop.minio_config, "backend", "minio"),
            "endpoint": loop.minio_config.endpoint,
            "access_key": loop.minio_config.access_key,
            "secret_key": loop.minio_config.secret_key,
            "bucket": loop.minio_config.bucket,
            "secure": bool(getattr(loop.minio_config, "secure", False)),
            "region": getattr(loop.minio_config, "region", ""),
            "workspace": minio_workspace,
        }
        for tool in (StorageFetchTool(**storage_args), StoragePushTool(**storage_args)):
            if loop._is_tool_enabled_by_config(tool.name):
                registry.register(tool)

    return [
        register_builtin_tools,
        register_skill_tools,
        register_storage_tools,
    ]


def apply_tool_registrars(
    registry: ToolRegistryProtocol,
    registrars: list[ToolRegistrar],
) -> None:
    for registrar in registrars:
        registrar(registry)
