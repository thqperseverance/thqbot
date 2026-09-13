"""Tests for document text extraction used by the doc_compare skill."""

import pytest
import zipfile
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import ithqbot.utils.helpers as helpers
from ithqbot.config.schema import AgentRoutingConfig, Config
from ithqbot.providers.base import LLMResponse
from ithqbot.skills.doc_compare.tool import DocCompareTool
from ithqbot.utils.helpers import extract_text_from_file_bytes


def _routing_matrix() -> dict:
    return {
        "classifier": {
            "small": {"activeModel": "router-classifier-model", "maxTokens": 256},
            "medium": {"activeModel": "router-classifier-model", "maxTokens": 256},
            "large": {"activeModel": "router-classifier-model", "maxTokens": 256},
        },
        "planner": {
            "small": {"activeModel": "planner-small", "maxTokens": 2048},
            "medium": {"activeModel": "planner-medium", "maxTokens": 4096},
            "large": {"activeModel": "planner-large", "maxTokens": 8192},
        },
        "finalAnswer": {
            "small": {"activeModel": "final-small", "maxTokens": 2048},
            "medium": {"activeModel": "final-medium", "maxTokens": 4096},
            "large": {"activeModel": "final-large", "maxTokens": 8192, "reasoningEffort": "high"},
        },
        "extraction": {
            "small": {"activeModel": "extract-small", "maxTokens": 1024},
            "medium": {"activeModel": "extract-medium", "maxTokens": 2048},
            "large": {"activeModel": "extract-large", "maxTokens": 4096},
        },
        "vision": {
            "small": {"activeModel": "vision-small", "maxTokens": 2048},
            "medium": {"activeModel": "vision-medium", "maxTokens": 4096},
            "large": {"activeModel": "vision-large", "maxTokens": 8192},
        },
    }


@pytest.fixture
def sample_docx(tmp_path) -> tuple[bytes, str]:
    path = tmp_path / "sample.docx"
    xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
        "<w:body>"
        "<w:p><w:r><w:t>First paragraph</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Second paragraph with special chars: 中文 &amp; &lt;tag&gt;</w:t></w:r></w:p>"
        "</w:body>"
        "</w:document>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return path.read_bytes(), "sample.docx"


def test_docx_extraction(sample_docx):
    data, filename = sample_docx
    text = extract_text_from_file_bytes(data, filename)
    assert "First paragraph" in text
    assert "Second paragraph" in text
    assert "中文" in text


def test_docx_extraction_without_python_docx_dependency(sample_docx, monkeypatch):
    data, filename = sample_docx
    monkeypatch.setattr(helpers, "docx", None)
    text = extract_text_from_file_bytes(data, filename)
    assert "First paragraph" in text
    assert "Second paragraph" in text


def test_plain_text_fallback():
    text = extract_text_from_file_bytes(b"plain text content", "readme.txt")
    assert text == "plain text content"


def test_empty_bytes_returns_empty():
    assert extract_text_from_file_bytes(b"", "empty.docx") == ""


def test_unknown_binary_falls_back_to_text():
    data = b"\x89PNG fake binary content"
    text = extract_text_from_file_bytes(data, "image.png")
    # Should not crash; just decode with replacement chars
    assert isinstance(text, str)


def test_csv_file_reads_as_text():
    csv_data = b"name,age\nAlice,30\nBob,25"
    text = extract_text_from_file_bytes(csv_data, "data.csv")
    assert "Alice" in text
    assert "Bob" in text


@pytest.mark.asyncio
async def test_doc_compare_falls_back_to_default_model_on_model_not_found(tmp_path):
    config = SimpleNamespace(
        agents=SimpleNamespace(
            purposes={"reasoning": "reasoning-missing"},
            defaults=SimpleNamespace(model="default-ok"),
        )
    )
    reasoning_provider = AsyncMock()
    reasoning_provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="Error calling LLM: model not found", finish_reason="error")
    )
    default_provider = AsyncMock()
    default_provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="## 对比报告\n- A 与 B 有差异", finish_reason="stop")
    )

    def provider_factory(model: str):
        if model == "reasoning-missing":
            return reasoning_provider
        if model == "default-ok":
            return default_provider
        raise AssertionError(model)

    tool = DocCompareTool(workspace=tmp_path, config=config, provider_factory=provider_factory)
    with patch.object(tool, "_get_content", new=AsyncMock(side_effect=["文档A", "文档B"])):
        result = await tool.execute("a.docx", "b.docx")

    assert "对比报告" in result
    assert reasoning_provider.chat_with_retry.await_count == 1
    assert default_provider.chat_with_retry.await_count == 1


@pytest.mark.asyncio
async def test_doc_compare_falls_back_when_context_call_llm_fails(tmp_path):
    config = SimpleNamespace(
        agents=SimpleNamespace(
            purposes={"reasoning": "reasoning-ok"},
            defaults=SimpleNamespace(model="reasoning-ok"),
        )
    )
    provider = AsyncMock()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="摘要：通过 provider 回退成功", finish_reason="stop")
    )
    context = SimpleNamespace(
        call_llm=AsyncMock(side_effect=RuntimeError("LLM call failed for task 'reasoning'"))
    )
    tool = DocCompareTool(workspace=tmp_path, config=config, provider_factory=lambda _: provider)
    with patch.object(tool, "_get_content", new=AsyncMock(side_effect=["文档A", "文档B"])):
        result = await tool.execute("a.docx", "b.docx", context=context)

    payload = json.loads(result)
    assert payload["_ithqbot_event"] == "doc_compare"
    assert "摘要：通过 provider 回退成功" in payload["llm_result"]
    assert provider.chat_with_retry.await_count == 1


@pytest.mark.asyncio
async def test_doc_compare_execute_retries_without_context_on_reasoning_runtime_error(tmp_path):
    config = SimpleNamespace(
        agents=SimpleNamespace(
            purposes={"reasoning": "reasoning-ok"},
            defaults=SimpleNamespace(model="reasoning-ok"),
        )
    )
    tool = DocCompareTool(workspace=tmp_path, config=config, provider_factory=lambda _: AsyncMock())
    context = SimpleNamespace(call_llm=AsyncMock())
    first_err = RuntimeError("LLM call failed for task 'reasoning'")
    with patch.object(
        tool,
        "_build_compare_report",
        new=AsyncMock(side_effect=[first_err, "摘要：回退成功"]),
    ) as mocked_report:
        result = await tool.execute("a.docx", "b.docx", context=context)
    payload = json.loads(result)
    assert payload["_ithqbot_event"] == "doc_compare"
    assert "摘要：回退成功" in payload["llm_result"]
    assert mocked_report.await_count == 2


@pytest.mark.asyncio
async def test_doc_compare_execute_uses_rule_based_report_when_reasoning_runtime_error_persists(tmp_path):
    config = SimpleNamespace(
        agents=SimpleNamespace(
            purposes={"reasoning": "reasoning-ok"},
            defaults=SimpleNamespace(model="reasoning-ok"),
        )
    )
    tool = DocCompareTool(workspace=tmp_path, config=config, provider_factory=lambda _: AsyncMock())
    context = SimpleNamespace(call_llm=AsyncMock())
    runtime_err = RuntimeError("LLM call failed for task 'reasoning'")
    with patch.object(tool, "_build_compare_report", new=AsyncMock(side_effect=[runtime_err, runtime_err])) as mocked_report:
        with patch.object(tool, "_get_content", new=AsyncMock(side_effect=["文档A 第一段", "文档B 第一段"])):
            result = await tool.execute("a.docx", "b.docx", context=context)

    payload = json.loads(result)
    assert payload["_ithqbot_event"] == "doc_compare"
    assert "已使用规则比对生成结果" in payload["llm_result"]
    assert "LLM call failed for task 'reasoning'" in payload["llm_result"]
    assert mocked_report.await_count == 2


@pytest.mark.asyncio
async def test_doc_compare_execute_still_returns_rule_report_when_emergency_reader_fails(tmp_path):
    config = SimpleNamespace(
        agents=SimpleNamespace(
            purposes={"reasoning": "reasoning-ok"},
            defaults=SimpleNamespace(model="reasoning-ok"),
        )
    )
    tool = DocCompareTool(workspace=tmp_path, config=config, provider_factory=lambda _: AsyncMock())
    context = SimpleNamespace(call_llm=AsyncMock())
    runtime_err = RuntimeError("LLM call failed for task 'reasoning'")
    with patch.object(tool, "_build_compare_report", new=AsyncMock(side_effect=[runtime_err, runtime_err])):
        with patch.object(tool, "_get_content", new=AsyncMock(side_effect=FileNotFoundError("missing file"))):
            result = await tool.execute("a.docx", "b.docx", context=context)

    payload = json.loads(result)
    assert payload["_ithqbot_event"] == "doc_compare"
    assert "已使用规则比对生成结果" in payload["llm_result"]
    assert "LLM call failed for task 'reasoning'" in payload["llm_result"]


@pytest.mark.asyncio
async def test_doc_compare_returns_rule_based_report_when_models_fail(tmp_path):
    config = SimpleNamespace(
        agents=SimpleNamespace(
            purposes={"reasoning": "reasoning-missing"},
            defaults=SimpleNamespace(model="default-ok"),
        )
    )
    reasoning_provider = AsyncMock()
    reasoning_provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="Error calling LLM: model not found", finish_reason="error")
    )
    default_provider = AsyncMock()
    default_provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="Error calling LLM: upstream unavailable", finish_reason="error")
    )

    def provider_factory(model: str):
        if model == "reasoning-missing":
            return reasoning_provider
        if model == "default-ok":
            return default_provider
        raise AssertionError(model)

    tool = DocCompareTool(workspace=tmp_path, config=config, provider_factory=provider_factory)
    with patch.object(tool, "_get_content", new=AsyncMock(side_effect=["文档A", "文档B"])):
        result = await tool.execute("a.docx", "b.docx")

    payload = json.loads(result)
    assert payload["_ithqbot_event"] == "doc_compare"
    assert "已使用规则比对生成结果" in payload["llm_result"]
    assert "upstream unavailable" in payload["llm_result"]


@pytest.mark.asyncio
async def test_doc_compare_direct_path_uses_route_profile_when_routing_enabled(tmp_path):
    config = Config()
    config.agents.routing = AgentRoutingConfig.model_validate(
        {
            "enabled": True,
            "matrix": _routing_matrix(),
            "rules": [
                {
                    "name": "doc-compare-route",
                    "keywords": ["详细比较"],
                    "purpose": "final_answer",
                    "tier": "large",
                    "activeModel": "routed-reasoning-model",
                    "maxTokens": 2048,
                    "reasoningEffort": "high",
                }
            ],
        }
    )
    routed_provider = AsyncMock()
    routed_provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="摘要：命中路由模型", finish_reason="stop")
    )

    def provider_factory(model: str):
        if model == "routed-reasoning-model":
            return routed_provider
        return AsyncMock()

    tool = DocCompareTool(workspace=tmp_path, config=config, provider_factory=provider_factory)
    with patch.object(tool, "_get_content", new=AsyncMock(side_effect=["文档A", "文档B"])):
        result = await tool.execute("a.docx", "b.docx")

    payload = json.loads(result)
    assert payload["_ithqbot_event"] == "doc_compare"
    assert "摘要：命中路由模型" in payload["llm_result"]
    assert routed_provider.chat_with_retry.await_args.kwargs["model"] == "routed-reasoning-model"
    assert routed_provider.chat_with_retry.await_args.kwargs["max_tokens"] == 2048
    assert routed_provider.chat_with_retry.await_args.kwargs["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_doc_compare_returns_otp_interaction_when_download_not_requested(tmp_path):
    config = SimpleNamespace(
        agents=SimpleNamespace(
            purposes={"reasoning": "reasoning-ok"},
            defaults=SimpleNamespace(model="reasoning-ok"),
        )
    )
    provider = AsyncMock()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="## 对比报告\n- 发现差异", finish_reason="stop")
    )
    tool = DocCompareTool(workspace=tmp_path, config=config, provider_factory=lambda _: provider)
    with patch.object(tool, "_get_content", new=AsyncMock(side_effect=["文档A", "文档B"])):
        result = await tool.execute("a.docx", "b.docx")
    payload = json.loads(result)
    assert payload["_ithqbot_event"] == "doc_compare"
    assert payload["interaction"]["type"] == "otp"
    assert "对比报告" in payload["llm_result"]


@pytest.mark.asyncio
async def test_doc_compare_download_requires_correct_otp_and_exports_file(tmp_path):
    config = SimpleNamespace(
        agents=SimpleNamespace(
            purposes={"reasoning": "reasoning-ok"},
            defaults=SimpleNamespace(model="reasoning-ok"),
        )
    )
    provider = AsyncMock()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="## 对比报告\n- 发现差异", finish_reason="stop")
    )
    tool = DocCompareTool(workspace=tmp_path, config=config, provider_factory=lambda _: provider)
    with patch.object(tool, "_get_content", new=AsyncMock(side_effect=["文档A", "文档B"])):
        first = await tool.execute("a.docx", "b.docx")
        wrong = await tool.execute("a.docx", "b.docx", download=True, otp_code="111111")
        ok = await tool.execute("a.docx", "b.docx", download=True, otp_code="246810")
    first_payload = json.loads(first)
    wrong_payload = json.loads(wrong)
    ok_payload = json.loads(ok)
    assert first_payload["interaction"]["type"] == "otp"
    assert wrong_payload["llm_result"].startswith("验证码错误")
    assert ok_payload["_ithqbot_event"] == "doc_compare"
    assert isinstance(ok_payload.get("files"), list)
    assert "点击下载对比报告" in ok_payload["llm_result"] or "请点击下载" in ok_payload["llm_result"]
    assert provider.chat_with_retry.await_count == 1


@pytest.mark.asyncio
async def test_doc_compare_download_stage_returns_link_without_repeating_analysis(tmp_path):
    config = SimpleNamespace(
        agents=SimpleNamespace(
            purposes={"reasoning": "reasoning-ok"},
            defaults=SimpleNamespace(model="reasoning-ok"),
        )
    )
    provider = AsyncMock()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="摘要：有差异\n关键差异：略", finish_reason="stop")
    )
    tool = DocCompareTool(workspace=tmp_path, config=config, provider_factory=lambda _: provider)
    with patch.object(tool, "_get_content", new=AsyncMock(side_effect=["文档A", "文档B"])):
        with patch.object(
            tool,
            "_export_report",
            new=AsyncMock(
                return_value={
                    "filename": "report.md",
                    "local_path": str(tmp_path / "report.md"),
                    "minio_uri": "minio://ithqbot-storage/doc_compare_reports/report.md",
                    "rel_path": "doc_compare_reports/report.md",
                    "bucket": "ithqbot-storage",
                    "download_url": "https://example.com/report.md",
                }
            ),
        ):
            result = await tool.execute("a.docx", "b.docx", download=True, otp_code="246810")
    payload = json.loads(result)
    assert payload["llm_result"] == "对比结果已生成：[点击下载对比报告](https://example.com/report.md)"
    assert payload["files"][0]["download_url"] == "https://example.com/report.md"
    assert payload["files"][0]["storage_backend"] == "minio"
    assert payload["files"][0]["storage_bucket"] == "ithqbot-storage"


@pytest.mark.asyncio
async def test_doc_compare_download_stage_includes_file_size_when_exported_file_exists(tmp_path):
    config = SimpleNamespace(
        agents=SimpleNamespace(
            purposes={"reasoning": "reasoning-ok"},
            defaults=SimpleNamespace(model="reasoning-ok"),
        )
    )
    provider = AsyncMock()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="摘要：有差异\n关键差异：略", finish_reason="stop")
    )
    tool = DocCompareTool(workspace=tmp_path, config=config, provider_factory=lambda _: provider)
    exported = tmp_path / "report.md"
    exported.write_text("对比结果", encoding="utf-8")
    with patch.object(tool, "_get_content", new=AsyncMock(side_effect=["文档A", "文档B"])):
        with patch.object(
            tool,
            "_export_report",
            new=AsyncMock(
                return_value={
                    "filename": "report.md",
                    "local_path": str(exported),
                    "minio_uri": "minio://ithqbot-storage/doc_compare_reports/report.md",
                    "rel_path": "doc_compare_reports/report.md",
                    "bucket": "ithqbot-storage",
                    "download_url": "https://example.com/report.md",
                }
            ),
        ):
            result = await tool.execute("a.docx", "b.docx", download=True, otp_code="246810")
    payload = json.loads(result)
    assert payload["files"][0]["size"] == exported.stat().st_size
    assert payload["files"][0]["download_url"] == "https://example.com/report.md"
    assert payload["files"][0]["storage"]["bucket"] == "ithqbot-storage"


@pytest.mark.asyncio
async def test_doc_compare_reads_relative_path_from_default_minio_bucket(tmp_path):
    config = SimpleNamespace(
        agents=SimpleNamespace(
            purposes={"reasoning": "reasoning-ok"},
            defaults=SimpleNamespace(model="reasoning-ok"),
        ),
        tools=SimpleNamespace(
            minio=SimpleNamespace(bucket="ithqbot-storage", endpoint="e", access_key="a", secret_key="s")
        ),
    )
    tool = DocCompareTool(workspace=tmp_path, config=config, provider_factory=lambda _: AsyncMock())
    with patch.object(tool, "_fetch_from_minio", new=AsyncMock(return_value="文档内容")) as fetch_mock:
        content = await tool._get_content("feishu_attachments/xx/1.docx")
    assert content == "文档内容"
    fetch_mock.assert_awaited_once_with("minio://ithqbot-storage/feishu_attachments/xx/1.docx")
