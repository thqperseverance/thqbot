import json
from pathlib import Path
from typing import Any

from ithqbot.agent.tools.base import Tool

class ExcelTool(Tool):
    """Tool to inspect Excel file structure and preview data."""

    def __init__(self, workspace: str | Path | None = None):
        self._workspace = Path(workspace) if workspace else None

    @property
    def name(self) -> str:
        return "excel_inspect"

    @property
    def description(self) -> str:
        return (
            "Inspect an Excel file (.xlsx, .xls) to learn its structure without loading the whole file. "
            "Returns sheet names and column headers for each sheet. "
            "Use this before performing deep analysis via code execution."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the Excel file (absolute or relative to workspace)",
                },
                "sheet_name": {
                    "type": "string",
                    "description": "Optional: Specific sheet to inspect. If omitted, returns structure for all sheets.",
                },
                "preview_rows": {
                    "type": "integer",
                    "description": "Optional: Number of rows to preview (default 0).",
                    "default": 0
                }
            },
            "required": ["file_path"],
        }

    async def execute(self, file_path: str, sheet_name: str | None = None, preview_rows: int = 0, cancellation_token: Any = None, **kwargs: Any) -> Any:
        try:
            self.throw_if_cancelled(cancellation_token)
            try:
                import pandas as pd
            except ImportError:
                return "错误：未安装 pandas，无法解析 Excel 文件。请先安装 `pandas`。"

            # Resolve path
            path = Path(file_path)
            if not path.is_absolute() and self._workspace:
                path = self._workspace / path
            
            if not path.exists():
                return f"错误：未找到文件 {path}"
            self.throw_if_cancelled(cancellation_token)

            # Use pandas for inspection
            xls = pd.ExcelFile(path)
            
            if sheet_name:
                if sheet_name not in xls.sheet_names:
                    return f"错误：未找到工作表“{sheet_name}”。可用工作表：{', '.join(xls.sheet_names)}"
                
                df = pd.read_excel(xls, sheet_name=sheet_name, nrows=0)
                result = {
                    "sheet": sheet_name,
                    "columns": df.columns.tolist(),
                    "total_sheets": len(xls.sheet_names)
                }
                if preview_rows > 0:
                    df_preview = pd.read_excel(xls, sheet_name=sheet_name, nrows=preview_rows)
                    self.throw_if_cancelled(cancellation_token)
                    result["preview"] = df_preview.to_dict(orient="records")
                return json.dumps(result, ensure_ascii=False)
            else:
                # Inspect all sheets
                summary = {
                    "filename": path.name,
                    "sheets": []
                }
                for name in xls.sheet_names:
                    self.throw_if_cancelled(cancellation_token)
                    df = pd.read_excel(xls, sheet_name=name, nrows=0)
                    summary["sheets"].append({
                        "name": name,
                        "columns": df.columns.tolist()
                    })
                return json.dumps(summary, ensure_ascii=False)

        except Exception as e:
            return f"错误：解析 Excel 文件失败：{str(e)}"
