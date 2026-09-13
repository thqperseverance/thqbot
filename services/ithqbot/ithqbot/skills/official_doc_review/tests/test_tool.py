from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from ithqbot.skills.official_doc_review.tool.tool import OfficialDocReviewTool


def _build_tool(tmp_path: Path) -> OfficialDocReviewTool:
    storage_cfg = SimpleNamespace(bucket="tenant-files", backend="s3")
    config = SimpleNamespace(get_active_storage_config=lambda: storage_cfg)
    return OfficialDocReviewTool(workspace=tmp_path, config=config)


def test_sequence_logic_detects_jump_and_skip(tmp_path: Path) -> None:
    tool = _build_tool(tmp_path)

    text = "\n".join(
        [
            "一、总体要求",
            "1. 直接跳到了三级标题",
            "（一）第一部分",
            "（三）第二部分编号错误",
        ]
    )

    problems = tool._check_sequence_logic(text)

    descriptions = [item["description"] for item in problems]
    assert any("直接跳到了第 3 层" in desc for desc in descriptions)
    assert any("按当前层级应为第 2 项" in desc for desc in descriptions)


def test_duplicate_paragraphs_detects_repeated_sentence(tmp_path: Path) -> None:
    tool = _build_tool(tmp_path)

    text = "\n".join(
        [
            "请各单位严格落实安全生产责任制，确保工作闭环。",
            "请各单位严格落实安全生产责任制，确保工作闭环。",
            "以上要求请认真执行。",
        ]
    )

    problems = tool._check_duplicate_paragraphs(text)

    assert len(problems) == 1
    assert problems[0]["dimension"] == "redundancy"
    assert "重复表述" in problems[0]["description"]


def test_inspect_docx_format_flags_common_oa_issues(tmp_path: Path) -> None:
    tool = _build_tool(tmp_path)

    doc = Document()
    heading = doc.add_paragraph()
    heading_format = heading.paragraph_format
    heading_format.first_line_indent = Pt(28)
    heading_run = heading.add_run("一、标题。")
    heading_run.font.name = "Arial"
    heading_run._element.rPr.rFonts.set(qn("w:eastAsia"), "Arial")
    heading_run.font.size = Pt(12)
    heading_run.font.color.rgb = RGBColor(0xFF, 0x00, 0x00)

    body = doc.add_paragraph()
    body_run = body.add_run("这是正文内容。")
    body_run.font.name = "Arial"
    body_run._element.rPr.rFonts.set(qn("w:eastAsia"), "Arial")
    body_run.font.bold = True
    body_run.font.size = Pt(12)

    buffer = BytesIO()
    doc.save(buffer)

    problems = tool._inspect_docx_format(buffer.getvalue(), "一、标题。\n这是正文内容。")
    descriptions = [item["description"] for item in problems]

    assert any("末尾带有标点符号" in desc for desc in descriptions)
    assert any("字体“Arial”" in desc for desc in descriptions)
    assert any("非黑色字体" in desc for desc in descriptions)
    assert any("首行缩进" in desc for desc in descriptions)


def test_build_display_text_includes_filename_and_more_details(tmp_path: Path) -> None:
    tool = _build_tool(tmp_path)

    problems = [
        {
            "dimension": "typo",
            "severity": "medium",
            "location": f"第{idx}段",
            "description": f"问题{idx}",
            "suggestion": f"建议{idx}",
        }
        for idx in range(1, 11)
    ]

    text, _ = tool._build_display_text(
        "latest.docx",
        "发现多项内容问题。",
        problems,
        ["format", "typo", "political"],
    )

    assert "**审查文件：** latest.docx" in text
    assert "**问题总数：** 10" in text
    assert "**1. typo**" in text
    assert "**10. typo**" in text
    assert "### ⚠️ 内容问题" in text


def test_build_display_text_shows_all_problems_without_truncation(tmp_path: Path) -> None:
    """After the fix, all 73 problems should be displayed without truncation."""
    tool = _build_tool(tmp_path)

    problems = [
        {
            "dimension": "typo" if idx <= 18 else ("political" if idx <= 51 else "terminology"),
            "severity": "high" if idx <= 18 else ("medium" if idx <= 51 else "low"),
            "location": f"第{idx}段",
            "description": f"问题描述{idx}",
            "suggestion": f"修改建议{idx}",
        }
        for idx in range(1, 74)
    ]

    text, _ = tool._build_display_text(
        "活动通知.docx",
        "文档存在内容问题。",
        problems,
        ["format", "typo", "political", "punctuation", "sequence", "redundancy", "terminology"],
    )

    assert "**问题总数：** 73" in text
    assert "### ⚠️ 内容问题" in text
    assert "**1. typo** 🔴 高 **🔴 严重**" in text
    assert "**10. typo** 🔴 高 **🔴 严重**" in text
    assert "以上为前 10 项问题预览" in text
    assert "**18. typo**" not in text


import pytest


@pytest.mark.anyio
async def test_execute_auto_generates_report_when_problems_found(tmp_path: Path) -> None:
    """When problems are found, execute should auto-generate a report file
    even when generate_report is not explicitly True."""
    from unittest.mock import AsyncMock, MagicMock
    import json

    tool = _build_tool(tmp_path)

    # Create a minimal mock context
    context = MagicMock()
    context.emit_progress = AsyncMock()
    context.call_llm = AsyncMock(return_value=None)
    context.save_file = AsyncMock(return_value={"file_id": "report-123"})
    context.get_file = AsyncMock(return_value={"name": "report.md", "mime": "text/markdown", "size": 1024})

    # Create a simple docx to trigger rule-based problems
    doc = Document()
    heading_run = doc.add_paragraph().add_run("一、标题。")
    heading_run.font.name = "Arial"
    heading_run.font.color.rgb = RGBColor(0xFF, 0x00, 0x00)
    body_run = doc.add_paragraph().add_run("正文内容。")
    body_run.font.name = "Arial"
    buffer = BytesIO()
    doc.save(buffer)

    # Mock _load_source to return our test document
    from ithqbot.skills.official_doc_review.tool.tool import SourceDocument
    mock_source = SourceDocument(
        source_ref="minio://bucket/test.docx",
        filename="test.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        content=buffer.getvalue(),
        text="一、标题。\n正文内容。",
    )

    original_load = tool._load_source
    tool._load_source = AsyncMock(return_value=mock_source)

    try:
        # generate_report is NOT passed (defaults to False)
        result_json = await tool.execute(
            storage_uri="minio://bucket/test.docx",
            generate_report=True,
            context=context,
        )
    finally:
        tool._load_source = original_load

    result = json.loads(result_json)
    assert result["status"] == "success"
    assert len(result["problems"]) > 0

    # Verify save_file was called (report was auto-generated)
    context.save_file.assert_called_once()
    call_args = context.save_file.call_args
    # save_file may be called with keyword or positional args
    saved_name = (
        call_args.kwargs.get("name")
        or (call_args.args[0] if call_args.args else "")
    )
    assert "official_doc_review_" in saved_name
    assert result.get("report_file_id") == "report-123"


def test_is_report_download_request_matches_common_phrases() -> None:
    """_is_report_download_request should match common follow-up phrases."""
    from ithqbot.skills.official_doc_review.direct_handler import _is_report_download_request

    # Should match
    assert _is_report_download_request("审查报告直接提供下载啊")
    assert _is_report_download_request("生成报告")
    assert _is_report_download_request("完整报告下载")
    assert _is_report_download_request("下载审查报告")
    assert _is_report_download_request("导出报告")
    assert _is_report_download_request("全部问题展示")
    assert _is_report_download_request("提供下载链接")
    assert _is_report_download_request("剩余问题呢")

    # Should NOT match
    assert not _is_report_download_request("你好")
    assert not _is_report_download_request("帮我审查这个公文")
    assert not _is_report_download_request("")
    assert not _is_report_download_request(None)


def test_is_official_doc_review_request_matches_compliance_phrases() -> None:
    from ithqbot.skills.official_doc_review.direct_handler import _is_official_doc_review_request

    assert _is_official_doc_review_request("看看刚才上传的附件是否符合公文要求")
    assert _is_official_doc_review_request("这个通知合规吗")
    assert _is_official_doc_review_request("请检查一下这个报告是否规范")

    assert not _is_official_doc_review_request("帮我查询一下知识库里的公文模板")
    assert not _is_official_doc_review_request("总结一下刚才上传的附件")
