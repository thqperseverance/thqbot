"""Skill tool: summarize a stored file and save summary as a new file."""

from __future__ import annotations

import asyncio
import json
from io import BytesIO
from typing import TYPE_CHECKING, Any

from ithqbot.agent.tools.base import Tool
from pypdf import PdfReader
from docx import Document

if TYPE_CHECKING:
    from ithqbot.agent.skills.base import SkillContext


class FileSummarySkillTool(Tool):
    _MAX_SOURCE_CHARS = 12000
    _LLM_TIMEOUT_SECONDS = 120

    @property
    def name(self) -> str:
        return "file_summary_skill"

    @property
    def description(self) -> str:
        return "Summarize a file by file_id and save summary as a new file."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "Source file id."},
                "output_name": {"type": "string", "description": "Output summary filename."},
                "output_mime": {"type": "string", "description": "Output summary mime."},
            },
            "required": ["file_id"],
        }

    async def execute(
        self,
        file_id: str = "",
        output_name: str = "summary.txt",
        output_mime: str = "text/plain",
        context: "SkillContext | None" = None,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> str:
        _ = kwargs
        self.throw_if_cancelled(cancellation_token)
        source_file_id = str(file_id or "").strip()
        if not source_file_id:
            return "错误：缺少必要参数 file_id。"
        if context is None:
            return "错误：缺少 skill context，无法读取文件。"

        await context.emit_progress(10, "reading", f"正在读取源文件 {source_file_id}")
        file_meta = await context.get_file(source_file_id)
        file_content = await context.read_file(source_file_id)
        self.throw_if_cancelled(cancellation_token)
        await context.emit_progress(25, "parsing", "正在解析文件内容")
        source_text = self._extract_text(
            content=file_content,
            mime=str(file_meta.get("mime") or ""),
            name=str(file_meta.get("name") or ""),
        )
        source_text = source_text.strip()
        if not source_text:
            return "错误：文件内容为空或暂不支持解析该文件格式。"
        clipped_source_text = source_text[: self._MAX_SOURCE_CHARS]

        await context.emit_progress(45, "summarizing", "正在调用模型生成摘要")
        try:
            route_profile = await context.resolve_llm_route_profile(
                task="final_answer",
                prompt_template=(
                    "请对以下文本生成结构化摘要。\n"
                    "要求：\n"
                    "1) 输出中文\n"
                    "2) 包含主题、关键要点、风险或待办\n"
                    "3) 保持简洁\n\n"
                    "原文：\n{text}"
                ),
                template_vars={"text": clipped_source_text},
                max_tokens=1200,
            )
            summary_text = await asyncio.wait_for(
                context.call_llm(
                    task="final_answer",
                    prompt_template=(
                        "请对以下文本生成结构化摘要。\n"
                        "要求：\n"
                        "1) 输出中文\n"
                        "2) 包含主题、关键要点、风险或待办\n"
                        "3) 保持简洁\n\n"
                        "原文：\n{text}"
                    ),
                    template_vars={"text": clipped_source_text},
                    temperature=0.2,
                    model=route_profile.active_model,
                    max_tokens=route_profile.max_tokens,
                    reasoning_effort=route_profile.reasoning_effort,
                ),
                timeout=self._LLM_TIMEOUT_SECONDS,
            )
            self.throw_if_cancelled(cancellation_token)
        except TimeoutError:
            return "错误：摘要生成超时，请稍后重试。"

        await context.emit_progress(80, "saving", "正在保存摘要文件")
        saved = await context.save_file(
            name=str(output_name or "summary.txt"),
            content=str(summary_text),
            mime=str(output_mime or "text/plain"),
        )
        self.throw_if_cancelled(cancellation_token)
        new_file_id = str(saved.get("file_id") or "")
        await context.emit_progress(100, "done", f"摘要文件已生成：{new_file_id}")
        return json.dumps(
            {
                "status": "success",
                "source_file_id": source_file_id,
                "new_file_id": new_file_id,
            },
            ensure_ascii=False,
        )

    def _extract_text(self, *, content: str | bytes, mime: str, name: str) -> str:
        if isinstance(content, str):
            return content
        normalized_mime = mime.lower()
        lower_name = name.lower()
        if "wordprocessingml.document" in normalized_mime or lower_name.endswith(".docx"):
            return self._extract_docx_text(content)
        if normalized_mime == "application/pdf" or lower_name.endswith(".pdf"):
            return self._extract_pdf_text(content)
        return content.decode("utf-8", errors="replace")

    @staticmethod
    def _extract_docx_text(raw: bytes) -> str:
        doc = Document(BytesIO(raw))
        parts = [paragraph.text.strip() for paragraph in doc.paragraphs if paragraph.text and paragraph.text.strip()]
        return "\n".join(parts)

    @staticmethod
    def _extract_pdf_text(raw: bytes) -> str:
        reader = PdfReader(BytesIO(raw))
        texts: list[str] = []
        for page in reader.pages:
            page_text = page.extract_text() or ""
            if page_text.strip():
                texts.append(page_text.strip())
        return "\n".join(texts)
