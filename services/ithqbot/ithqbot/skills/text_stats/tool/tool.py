"""``text_stats`` 技能实现：确定性文本统计。

设计约束（见 skills/text_stats/SKILL.md）：
- 不调用模型、不访问网络、不写文件，纯本地计算；
- 返回结构化 ``ToolResult``，便于测试与前端展示；
- 若注入了 ``SkillContext``，上报一次进度事件，让 UI 能看到技能被调用。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

from ithqbot.agent.tools.base import Tool, ToolResult

if TYPE_CHECKING:  # pragma: no cover
    from ithqbot.agent.skills.base import SkillContext

CJK_CHAR = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
# 允许首字母后的数字与连字符：gpt4 / k8s / state-of-the-art 都算一个词
LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'\-]*")
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9'\-]*|[\u3400-\u4dbf\u4e00-\u9fff]")

# 高频无意义词，仅用于关键词排序过滤（保持精简）
STOPWORDS = frozenset(
    {
        # 英文
        "the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "had",
        "her", "was", "one", "our", "out", "day", "get", "has", "him", "his", "how",
        "its", "may", "new", "now", "old", "see", "two", "who", "boy", "did", "use",
        "that", "this", "with", "have", "from", "they", "will", "would", "there",
        "their", "what", "about", "which", "when", "your", "them", "then", "than",
        "into", "more", "some", "such", "only", "also", "been", "were", "here",
        # 中文常见虚词/连接词（bigram 形式）
        "我们", "你们", "他们", "这个", "那个", "一个", "可以", "因为", "所以",
        "但是", "如果", "就是", "不是", "没有", "以及", "并且", "或者", "然后",
        "已经", "还是", "这样", "那样", "什么", "怎么", "为了", "通过", "需要",
        "进行", "对于", "关于", "由于", "同时", "这些", "那些", "自己", "一些",
    }
)

DEFAULT_TOP_N = 5
MAX_TOP_N = 50


def _cjk_bigrams(text: str) -> list[str]:
    """把连续中文切成 2-gram；单字连续串长度为 1 时退化为该单字。"""
    tokens: list[str] = []
    for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", text):
        if len(run) == 1:
            tokens.append(run)
            continue
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def extract_keywords(text: str, top_n: int = DEFAULT_TOP_N) -> list[dict[str, Any]]:
    """基于词频（英文词 + 中文 2-gram）提取关键词。"""
    candidates: list[str] = []
    for match in LATIN_WORD.finditer(text):
        word = match.group(0).lower()
        if len(word) >= 2 and word not in STOPWORDS:
            candidates.append(word)
    for bigram in _cjk_bigrams(text):
        if bigram not in STOPWORDS:
            candidates.append(bigram)

    counts = Counter(candidates)
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [{"word": word, "count": count} for word, count in ordered[:top_n]]


def compute_stats(text: str, top_n: int = DEFAULT_TOP_N) -> dict[str, Any]:
    """计算全部统计指标（纯函数，便于单测）。"""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    # 契约：top_n 一律钳制到 [1, MAX_TOP_N]；类型不合法时才回落到默认值。
    # 注意不能用 `top_n or DEFAULT`，否则 0 会被当成"未提供"而变成 5。
    try:
        requested = int(top_n)
    except (TypeError, ValueError):
        requested = DEFAULT_TOP_N
    top_n = max(1, min(requested, MAX_TOP_N))

    chars_total = len(text)
    chars_no_space = sum(1 for char in text if not char.isspace())
    lines = text.count("\n") + 1 if text else 0
    paragraphs = len([block for block in re.split(r"\n\s*\n", text) if block.strip()])

    words_cjk = len(CJK_CHAR.findall(text))
    words_latin = len(LATIN_WORD.findall(text))
    words_total = words_cjk + words_latin
    # 约 240 词/分钟
    reading_seconds = max(1, round(words_total / 4)) if words_total else 0

    return {
        "chars_total": chars_total,
        "chars_no_space": chars_no_space,
        "lines": lines,
        "paragraphs": paragraphs,
        "words_cjk": words_cjk,
        "words_latin": words_latin,
        "words_total": words_total,
        "reading_seconds": reading_seconds,
        "keywords": extract_keywords(text, top_n=top_n),
    }


class TextStatsTool(Tool):
    """统计文本规模与高频关键词。"""

    @property
    def name(self) -> str:
        return "text_stats"

    @property
    def description(self) -> str:
        return (
            "统计一段文本的字符数、行数、段落数、中英文词数、预计朗读时长与高频关键词。"
            "当用户要求字数统计、文本概览或关键词提取时使用。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "待统计的文本内容。"},
                "top_n": {
                    "type": "integer",
                    "description": "返回的高频关键词个数，默认 5。",
                    "default": DEFAULT_TOP_N,
                    "minimum": 1,
                    "maximum": MAX_TOP_N,
                },
            },
            "required": ["text"],
        }

    async def execute(  # type: ignore[override]
        self,
        text: str = "",
        top_n: int = DEFAULT_TOP_N,
        context: "SkillContext | None" = None,
        **kwargs: Any,
    ) -> ToolResult:
        if not isinstance(text, str) or not text.strip():
            return ToolResult(
                content="text 不能为空，请提供需要统计的文本。",
                success=False,
                error="empty text",
            )

        await self._emit_progress(context)

        stats = compute_stats(text, top_n=top_n)
        payload = {"status": "success", "message": "统计完成", "data": stats}
        return ToolResult(
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            success=True,
            metadata={"chars_total": stats["chars_total"], "keywords": len(stats["keywords"])},
        )

    @staticmethod
    async def _emit_progress(context: "SkillContext | None") -> None:
        if context is None:
            return
        emit = getattr(context, "emit_progress", None)
        if emit is None:
            return
        try:
            await emit(
                50,
                "analyzing",
                "正在统计文本",
                progress_stage="skill_call",
                call_type="skill",
                skill_name="text_stats",
                tool_name="text_stats",
                status_details={"execution": {"step": "compute_stats"}},
            )
        except Exception:  # noqa: BLE001 - 进度上报失败不能影响技能结果
            pass
