"""``text_stats`` 技能的单测（就近放置，遵循 SKILL_STANDARDS §12）。"""

from __future__ import annotations

import json

import pytest

from ithqbot.skills.text_stats.tool.tool import (
    TextStatsTool,
    compute_stats,
    extract_keywords,
)

TEXT = "thqbot 是一个 agent 平台。\n\nthqbot 支持多轮对话与技能调用。\n"


def test_compute_stats_basic_counts():
    stats = compute_stats(TEXT)
    assert stats["chars_total"] == len(TEXT)
    assert stats["lines"] == 4
    assert stats["paragraphs"] == 2
    assert stats["words_latin"] == 3  # thqbot / agent / thqbot
    assert stats["words_cjk"] > 0
    assert stats["words_total"] == stats["words_cjk"] + stats["words_latin"]
    assert stats["chars_no_space"] < stats["chars_total"]


def test_compute_stats_empty_text():
    stats = compute_stats("")
    assert stats["chars_total"] == 0
    assert stats["lines"] == 0
    assert stats["paragraphs"] == 0
    assert stats["words_total"] == 0
    assert stats["reading_seconds"] == 0
    assert stats["keywords"] == []


def test_compute_stats_handles_non_string_input():
    stats = compute_stats(None)  # type: ignore[arg-type]
    assert stats["chars_total"] == 0
    stats_number = compute_stats(12345)  # type: ignore[arg-type]
    assert stats_number["chars_total"] == 5


def test_keywords_rank_by_frequency_and_filter_stopwords():
    text = "平台 平台 平台 设计 设计 方案 the the and"
    keywords = extract_keywords(text, top_n=3)
    words = [item["word"] for item in keywords]
    assert "平台" in words
    assert "the" not in words
    assert keywords[0]["word"] == "平台"
    assert keywords[0]["count"] == 3


def test_keywords_dedupe_latin_case_insensitively():
    keywords = extract_keywords("Agent agent AGENT beta", top_n=5)
    assert keywords[0]["word"] == "agent"
    assert keywords[0]["count"] == 3


def test_top_n_is_clamped():
    text = " ".join(f"word{index}" for index in range(100))
    assert len(compute_stats(text, top_n=0)["keywords"]) == 1
    assert len(compute_stats(text, top_n=999)["keywords"]) <= 50


def test_single_cjk_run_falls_back_to_single_chars():
    keywords = extract_keywords("好", top_n=3)
    assert keywords == [{"word": "好", "count": 1}]


def test_tool_contract_shape():
    tool = TextStatsTool()
    assert tool.name == "text_stats"
    assert tool.description
    schema = tool.parameters
    assert schema["required"] == ["text"]
    assert set(schema["properties"]) == {"text", "top_n"}


async def test_execute_returns_structured_payload():
    tool = TextStatsTool()
    result = await tool.execute(text=TEXT, top_n=3)
    assert result.success is True
    payload = json.loads(result.content)
    assert payload["status"] == "success"
    assert payload["data"]["chars_total"] == len(TEXT)
    assert result.metadata["chars_total"] == len(TEXT)


async def test_execute_rejects_blank_text():
    tool = TextStatsTool()
    for blank in ("", "   ", "\n\t"):
        result = await tool.execute(text=blank)
        assert result.success is False
        assert result.error == "empty text"


async def test_execute_reports_progress_when_context_present():
    calls: list[tuple] = []

    class FakeContext:
        async def emit_progress(self, percent, stage, message, **kwargs):
            calls.append((percent, stage, message, kwargs))

    tool = TextStatsTool()
    result = await tool.execute(text=TEXT, context=FakeContext())
    assert result.success is True
    assert len(calls) == 1
    percent, stage, _message, kwargs = calls[0]
    assert percent == 50
    assert stage == "analyzing"
    assert kwargs["skill_name"] == "text_stats"


async def test_execute_survives_progress_failure():
    class BrokenContext:
        async def emit_progress(self, *_args, **_kwargs):
            raise RuntimeError("progress channel down")

    tool = TextStatsTool()
    result = await tool.execute(text=TEXT, context=BrokenContext())
    assert result.success is True


@pytest.mark.parametrize("top_n", [1, 5, 50])
async def test_execute_respects_top_n(top_n: int):
    text = " ".join(f"token{index}" for index in range(60))
    tool = TextStatsTool()
    result = await tool.execute(text=text, top_n=top_n)
    payload = json.loads(result.content)
    assert len(payload["data"]["keywords"]) == top_n
