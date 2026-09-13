from typing import Any, Awaitable, Callable

from ithqbot import context as runtime_context
from ithqbot.agent.tools.base import Tool, ToolResult
from ithqbot.agent.tools.protocol import ToolRegistryProtocol
from ithqbot.observability import audit_call


class ToolRegistry(ToolRegistryProtocol):
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._direct_handlers: dict[str, Callable[..., Awaitable[Any]]] = {}

    def register(self, tool: Tool, direct_handler: Callable[..., Awaitable[Any]] | None = None) -> None:
        """Register a tool and its optional direct route handler."""
        self._tools[tool.name] = tool
        resolved_handler = direct_handler if direct_handler is not None else tool.direct_handler
        if callable(resolved_handler):
            self._direct_handlers[tool.name] = resolved_handler
        else:
            self._direct_handlers.pop(tool.name, None)

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)
        self._direct_handlers.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def get_tool(self, name: str) -> Tool | None:
        """Read-only accessor for a registered tool."""
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        """Read-only snapshot of all registered tools."""
        return list(self._tools.values())

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def get_direct_handler(self, name: str) -> Callable[..., Awaitable[Any]] | None:
        """Get a registered direct route handler by tool name."""
        return self._direct_handlers.get(name)

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

    async def execute(self, name: str, params: Any, context_obj: Any = None) -> str:
        """Execute a tool by name with given parameters."""
        hint = "\n\n请根据以上错误信息调整后重试，或改用其他可行方式。"

        tool = self.get_tool(name)
        if not tool:
            return f"错误：未找到工具“{name}”。可用工具：{', '.join(self.tool_names)}"

        module_name = getattr(tool.__class__, "__module__", "")
        if name.startswith("mcp_"):
            call_type = "mcp"
        elif module_name.startswith("ithqbot.skills.dynamic.") or module_name.startswith("ithqbot.skills."):
            call_type = "skill"
        else:
            call_type = "tool"
        normalized_params: dict[str, Any]
        if isinstance(params, dict):
            normalized_params = dict(params)
        else:
            normalized_params = {}
            if params not in (None, ""):
                normalized_params["input"] = params

        self._safe_audit(call_type=call_type, name=name, phase="start", input_data={"params": normalized_params})
        llm_tokens = runtime_context.set_llm_call_context(source=call_type, component=name)
        try:
            params = tool.cast_params(normalized_params)
            if not isinstance(params, dict):
                params = {}
            result = await tool.invoke(params, context_obj=context_obj)
            if not isinstance(result, ToolResult):
                result = ToolResult(content=str(result))
            if not result.success:
                self._safe_audit(
                    call_type=call_type,
                    name=name,
                    phase="error",
                    input_data={"params": params},
                    output_data={"result": result.content},
                )
                return result.content
            self._safe_audit(
                call_type=call_type,
                name=name,
                phase="end",
                input_data={"params": params},
                output_data={"result": result.content},
            )
            return result.content
        except Exception as e:
            result = f"错误：执行工具“{name}”失败：{str(e)}" + hint
            self._safe_audit(
                call_type=call_type,
                name=name,
                phase="error",
                input_data={"params": normalized_params},
                output_data={"result": result},
            )
            return result
        finally:
            runtime_context.reset_llm_call_context(llm_tokens)

    def _safe_audit(self, **payload: Any) -> None:
        try:
            audit_call(**payload)
        except Exception:
            return

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    @property
    def direct_route_names(self) -> list[str]:
        """Get tool names that have registered direct route handlers."""
        return list(self._direct_handlers.keys())
