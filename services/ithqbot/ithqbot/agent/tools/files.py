"""``read_file`` 工具：让 prompt 型技能真正可用。

背景（D13=A）：`agent/context.py` 与 `agent/skills/loader.py` 都会指示模型
"用 read_file 读 SKILL.md"，但仓库里**从未注册过这个工具**，导致纯 Prompt 技能与
Composite 技能拿不到正文。本模块补上该工具。

沙箱策略：
- 允许读取的根目录 = 工作区 + 显式声明的 ``extra_readable_roots``
  （内置技能目录必须列入，否则读不到 ``ithqbot/skills/*/SKILL.md``）。
- ``restrict_to_workspace=True`` 时，任何越界路径（含 ``..`` 穿越、符号链接逃逸）
  一律拒绝；``False`` 时不做限制，但仍会解析真实路径。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from ithqbot.agent.tools.base import Tool, ToolResult

DEFAULT_MAX_BYTES = 200_000

# 视为文本、可直接读取的扩展名；其余按二进制处理（除非显式允许）
TEXT_SUFFIXES = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".text",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".csv",
        ".tsv",
        ".log",
        ".py",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".sh",
        ".bash",
        ".sql",
        ".html",
        ".htm",
        ".xml",
        ".css",
        ".env",
        ".gitignore",
    }
)


class ReadFileTool(Tool):
    """读取文本文件内容。"""

    def __init__(
        self,
        workspace: Path | str,
        *,
        restrict_to_workspace: bool = True,
        extra_readable_roots: Iterable[Path | str] | None = None,
    ) -> None:
        self._workspace = Path(workspace).expanduser()
        self._restrict = bool(restrict_to_workspace)
        self._extra_roots = [Path(item).expanduser() for item in (extra_readable_roots or [])]

    # ------------------------------------------------------------------ 契约

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "读取一个文本文件的内容并返回。"
            "当需要查阅技能的 SKILL.md 说明、读取工作区内的文档/配置/代码时使用。"
            "支持可选的行号区间与读取字节上限。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径。相对路径按工作区解析；也可传允许根目录下的绝对路径。",
                },
                "start_line": {
                    "type": "integer",
                    "description": "可选，起始行号（从 1 开始，含该行）。",
                },
                "end_line": {
                    "type": "integer",
                    "description": "可选，结束行号（含该行）。",
                },
                "max_bytes": {
                    "type": "integer",
                    "description": f"可选，最多返回的字节数，默认 {DEFAULT_MAX_BYTES}。",
                },
            },
            "required": ["path"],
        }

    # ------------------------------------------------------------------ 路径

    def _allowed_roots(self) -> list[Path]:
        roots: list[Path] = []
        seen: set[Path] = set()
        for raw in [self._workspace, *self._extra_roots]:
            try:
                resolved = raw.expanduser().resolve()
            except OSError:
                continue
            if resolved not in seen:
                seen.add(resolved)
                roots.append(resolved)
        return roots

    @staticmethod
    def _is_within(candidate: Path, root: Path) -> bool:
        return candidate == root or root in candidate.parents

    def resolve_path(self, raw_path: str) -> Path:
        """把用户给的路径解析为绝对路径，越界时抛 PermissionError。"""
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("path 不能为空")
        candidate = Path(raw_path.strip()).expanduser()
        if not candidate.is_absolute():
            candidate = self._workspace / candidate
        try:
            resolved = candidate.resolve()
        except OSError as exc:  # pragma: no cover - 极少见
            raise PermissionError(f"无法解析路径：{raw_path}（{exc}）") from exc

        if self._restrict and not any(self._is_within(resolved, root) for root in self._allowed_roots()):
            allowed = "、".join(str(root) for root in self._allowed_roots())
            raise PermissionError(f"路径越界，只允许读取：{allowed}")
        return resolved

    # ------------------------------------------------------------------ 执行

    async def execute(  # type: ignore[override]
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        max_bytes: int | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        limit = DEFAULT_MAX_BYTES if max_bytes is None else max(1, int(max_bytes))

        try:
            resolved = self.resolve_path(path)
        except (ValueError, PermissionError) as exc:
            return ToolResult(content=str(exc), success=False, error=str(exc))

        if not resolved.exists():
            message = f"文件不存在：{resolved}"
            return ToolResult(content=message, success=False, error=message)
        if resolved.is_dir():
            message = f"这是一个目录，不是文件：{resolved}"
            return ToolResult(content=message, success=False, error=message)

        suffix = resolved.suffix.lower()
        if suffix and suffix not in TEXT_SUFFIXES:
            message = f"暂不支持读取该类型的文件：{suffix}（仅支持文本文件）"
            return ToolResult(content=message, success=False, error=message)

        try:
            raw = resolved.read_bytes()
        except OSError as exc:
            message = f"读取失败：{resolved}（{exc}）"
            return ToolResult(content=message, success=False, error=message)

        truncated_bytes = len(raw) > limit
        if truncated_bytes:
            raw = raw[:limit]
        text = raw.decode("utf-8", errors="replace")

        total_lines = text.count("\n") + (0 if text.endswith("\n") else 1)
        lines = text.splitlines()
        start = 1 if start_line is None else max(1, int(start_line))
        end = len(lines) if end_line is None else max(start, int(end_line))
        selected = lines[start - 1 : end]

        header = f"# {resolved}"
        header += f"\n# 共 {total_lines} 行，本次返回第 {start}-{min(end, len(lines))} 行"
        if truncated_bytes:
            header += f"\n# 注意：文件超过 {limit} 字节，内容已截断"
        body = "\n".join(selected)
        content = f"{header}\n\n{body}"

        return ToolResult(
            content=content,
            success=True,
            metadata={
                "path": str(resolved),
                "bytes_read": len(raw),
                "truncated": truncated_bytes,
                "line_start": start,
                "line_end": min(end, len(lines)),
                "total_lines": total_lines,
            },
        )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ReadFileTool workspace={self._workspace} restrict={self._restrict} extra={len(self._extra_roots)}>"


def dumps_metadata(result: ToolResult) -> str:
    """便捷方法：把 metadata 序列化（供测试/调试）。"""
    return json.dumps(result.metadata, ensure_ascii=False, sort_keys=True)
