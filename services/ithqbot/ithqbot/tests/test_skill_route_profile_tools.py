import json
from pathlib import Path

import pytest

from ithqbot.config.schema import RouteProfile, RoutePurpose
from ithqbot.skills.file_summary_skill.tool.tool import FileSummarySkillTool
from ithqbot.skills.knowledge_retrieval.tool.tool import KnowledgeRetrievalTool
from ithqbot.skills.official_doc_review.tool.tool import OfficialDocReviewTool, ReviewLLMOutput


class _StubSkillContext:
    def __init__(self) -> None:
        self.route_calls: list[dict] = []
        self.llm_calls: list[dict] = []
        self.progress_calls: list[tuple] = []

    async def resolve_llm_route_profile(self, **kwargs):
        self.route_calls.append(kwargs)
        task = str(kwargs.get("task") or "")
        purpose = {
            "final_answer": RoutePurpose.FINAL_ANSWER,
            "extraction": RoutePurpose.EXTRACTION,
        }.get(task, RoutePurpose.PLANNER)
        return RouteProfile(
            purpose=purpose,
            active_model=f"{task}-model",
            fallback_models=[],
            max_tokens=kwargs.get("max_tokens"),
            reasoning_effort="medium",
            source="test",
        )

    async def call_llm(self, **kwargs):
        self.llm_calls.append(kwargs)
        if kwargs.get("output_model") is ReviewLLMOutput:
            return ReviewLLMOutput(summary="审查完成", problems=[])
        return "摘要完成"

    async def emit_progress(self, *args, **kwargs):
        self.progress_calls.append((args, kwargs))

    async def get_file(self, file_id: str):
        return {"file_id": file_id, "mime": "text/plain", "name": "source.txt"}

    async def read_file(self, file_id: str):
        return "这里是一段待总结的文本内容"

    async def save_file(self, *, name: str, content: str, mime: str):
        return {"file_id": "summary-file-1", "name": name, "content": content, "mime": mime}


@pytest.mark.asyncio
async def test_knowledge_retrieval_uses_final_answer_route_profile() -> None:
    tool = KnowledgeRetrievalTool()
    context = _StubSkillContext()

    result = await tool._summarize_passages(
        [{"id": "1", "title": "doc", "page_content": "知识点"}],
        "请回答这个问题",
        context,
    )

    assert result == "摘要完成"
    assert context.route_calls[0]["task"] == "final_answer"
    assert context.llm_calls[0]["task"] == "final_answer"
    assert context.llm_calls[0]["model"] == "final_answer-model"
    assert context.llm_calls[0]["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_official_doc_review_uses_extraction_route_profile(tmp_path: Path) -> None:
    tool = OfficialDocReviewTool(workspace=tmp_path, config=object(), provider_factory=None)
    context = _StubSkillContext()

    output = await tool._run_llm_review(
        source=type("Source", (), {"text": "这是正文", "filename": "oa.docx"})(),
        dimensions=["format", "typo"],
        rule_problems=[],
        context=context,
    )

    assert output is not None
    assert context.route_calls[0]["task"] == "extraction"
    assert context.llm_calls[0]["task"] == "extraction"
    assert context.llm_calls[0]["model"] == "extraction-model"
    assert context.llm_calls[0]["max_tokens"] == 1800


@pytest.mark.asyncio
async def test_file_summary_uses_final_answer_route_profile() -> None:
    tool = FileSummarySkillTool()
    context = _StubSkillContext()

    result = await tool.execute(file_id="file-1", context=context)
    payload = json.loads(result)

    assert payload["status"] == "success"
    assert context.route_calls[0]["task"] == "final_answer"
    assert context.llm_calls[0]["task"] == "final_answer"
    assert context.llm_calls[0]["model"] == "final_answer-model"
    assert context.llm_calls[0]["max_tokens"] == 1200
