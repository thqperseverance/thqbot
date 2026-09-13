"""Tool wrapper for the summarize CLI."""

import asyncio
import shutil
from typing import TYPE_CHECKING, Any

from loguru import logger

from ithqbot.agent.tools.base import Tool
from ithqbot.observability import record_trace_event

if TYPE_CHECKING:
    pass


_SUMMARIZE_TIMEOUT_SECONDS = 180.0
_PROCESS_KILL_WAIT_SECONDS = 5.0

class SummarizeTool(Tool):
    """Tool to summarize URLs, files, and YouTube videos via CLI."""

    def __init__(self, workspace: Any = None, config: Any = None, **kwargs: Any):
        super().__init__()
        self._workspace = workspace
        self._config = config

    @property
    def name(self) -> str:
        return "summarize"

    @property
    def description(self) -> str:
        return "Summarize a URL, local file, or YouTube video content using the summarize CLI."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "The URL, file path, or YouTube link to summarize."
                },
                "length": {
                    "type": "string",
                    "enum": ["short", "medium", "long", "xl", "xxl"],
                    "description": "Desired length of the summary."
                },
                "extract_only": {
                    "type": "boolean",
                    "description": "If true, only extract the content without summarizing."
                },
                "youtube": {
                    "type": "string",
                    "enum": ["auto", "off", "always"],
                    "description": "YouTube extraction strategy."
                }
            },
            "required": ["target"]
        }

    async def execute(self, *args: Any, **kwargs: Any) -> str:
        """Execute the summarize CLI command."""
        cancellation_token = kwargs.get("cancellation_token")
        self.throw_if_cancelled(cancellation_token)
        target = str(kwargs.get("target") or "").strip()
        if not target and len(args) >= 1:
            target = str(args[0] or "").strip()
        if not target:
            return "错误：缺少必要参数 target。"
        length = str(kwargs.get("length") or "medium").strip() or "medium"
        if len(args) >= 2 and "length" not in kwargs:
            length = str(args[1] or "medium").strip() or "medium"
        extract_only = bool(kwargs.get("extract_only", False))
        if len(args) >= 3 and "extract_only" not in kwargs:
            extract_only = bool(args[2])
        youtube = str(kwargs.get("youtube") or "auto").strip() or "auto"
        if len(args) >= 4 and "youtube" not in kwargs:
            youtube = str(args[3] or "auto").strip() or "auto"
        context = kwargs.get("context")

        # 1. Initialization
        if context:
            await context.emit_progress(
                5, "initializing", f"识别总结目标: {target}",
                content_preview=f"识别总结目标: {target}"
            )
        else:
            record_trace_event(
                event_name="skill.progress",
                phase="running",
                status="running",
                component="summarize",
                source="skill",
                content_preview=f"识别总结目标: {target}",
                details={"percent": 5, "stage": "initializing", "status_text": f"识别总结目标: {target}"}
            )

        binary = shutil.which("summarize")
        if not binary:
            return "错误：未发现 'summarize' 可执行文件。请先安装 summarize CLI。"
        self.throw_if_cancelled(cancellation_token)

        # 2. Build command
        cmd_args: list[str] = [binary, target]
        if length:
            cmd_args.extend(["--length", length])
        if extract_only:
            cmd_args.append("--extract-only")
        if youtube:
            cmd_args.extend(["--youtube", youtube])

        # Try to pass the model from config if available
        model = None
        if self._config and hasattr(self._config, "agents"):
            model = self._config.agents.defaults.model
        if model:
            cmd_args.extend(["--model", model])

        if context:
            await context.emit_progress(
                25, "extracting", "正在提取远端网页或文件内容...",
                content_preview="正在提取内容..."
            )
        else:
            record_trace_event(
                event_name="skill.progress",
                phase="running",
                status="running",
                component="summarize",
                source="skill",
                content_preview="正在提取远端网页或文件内容...",
                details={"percent": 25, "stage": "extracting", "status_text": "正在提取远端网页或文件内容..."}
            )
        self.throw_if_cancelled(cancellation_token)

        process: asyncio.subprocess.Process | None = None
        try:
            # 3. Execute
            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            if context:
                await context.emit_progress(
                    65, "summarizing", "正在生成摘要并提炼重点...",
                    content_preview="正在生成摘要..."
                )
            else:
                record_trace_event(
                    event_name="skill.progress",
                    phase="running",
                    status="running",
                    component="summarize",
                    source="skill",
                    content_preview="正在生成摘要并提炼重点...",
                    details={"percent": 65, "stage": "summarizing", "status_text": "正在生成摘要并提炼重点..."}
                )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=_SUMMARIZE_TIMEOUT_SECONDS,
            )

            if process.returncode != 0:
                err_msg = stderr.decode("utf-8", errors="ignore").strip()
                logger.error(f"Summarize CLI failed: {err_msg}")
                return f"错误：总结失败。具体原因：{err_msg}"

            result = stdout.decode("utf-8", errors="ignore").strip()

            if context:
                await context.emit_progress(
                    95, "finalizing", "摘要处理完成，正在整理输出。",
                    content_preview="摘要处理完成"
                )
            else:
                record_trace_event(
                    event_name="skill.progress",
                    phase="running",
                    status="running",
                    component="summarize",
                    source="skill",
                    content_preview="摘要处理完成，正在整理输出。",
                    details={"percent": 95, "stage": "finalizing", "status_text": "摘要处理完成，正在整理输出。"}
                )

            return result
        except asyncio.TimeoutError:
            logger.warning("Summarize CLI timed out after {}s for target {}", _SUMMARIZE_TIMEOUT_SECONDS, target)
            if process is not None and process.returncode is None:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=_PROCESS_KILL_WAIT_SECONDS)
                except Exception:
                    logger.debug("Timed out waiting for summarize CLI to exit after kill")
            return f"错误：总结超时，已在 {_SUMMARIZE_TIMEOUT_SECONDS:.0f} 秒后终止执行。"
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=_PROCESS_KILL_WAIT_SECONDS)
                except Exception:
                    logger.debug("Timed out waiting for summarize CLI to exit after cancellation")
            raise
        except Exception as e:
            logger.exception(f"SummarizeTool failed: {e}")
            return f"错误：总结过程中发生异常：{str(e)}"
