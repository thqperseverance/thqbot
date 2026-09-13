"""Tool wrapper for searching a configured knowledge base and summarizing the result."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import aiohttp
from loguru import logger

from ithqbot.agent.tools.base import Tool
from ithqbot.observability import record_trace_event
from ithqbot.skills.knowledge_retrieval.config import knowledge_config
from ithqbot.utils.http_client import classify_http_client_error

if TYPE_CHECKING:
    from ithqbot.agent.skills.base import SkillContext


class KnowledgeRetrievalTool(Tool):
    """从配置的知识库中召回内容，并给出面向用户的答案。"""

    def __init__(self, workspace: Any = None, config: Any = None, **kwargs: Any):
        super().__init__()
        _ = kwargs
        self._workspace = workspace
        self._config = config

    @property
    def name(self) -> str:
        return "retrieve_knowledge"

    @property
    def description(self) -> str:
        return "Search the configured knowledge base, extract relevant passages, and answer the user's question in Chinese."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用户的问题或检索语句。"
                },
                "domain": {
                    "type": "string",
                    "description": "可选的知识域或知识库名称；未提供时使用默认知识库。"
                }
            },
            "required": ["query"]
        }

    async def execute(
        self,
        query: str = "",
        domain: str | None = None,
        context: "SkillContext | None" = None,
        **kwargs: Any,
    ) -> str:
        """从知识库检索信息并生成结构化结果。"""
        _ = kwargs
        query_text = (query or "").strip()
        domain_text = (domain or "").strip()
        if not query_text:
            return "错误：缺少必要参数 query。"

        knowledge_api_url = knowledge_config.get_knowledge_api_url()
        if not knowledge_api_url:
            return "错误：未配置知识检索接口地址，请设置 ITHQBOT_KNOWLEDGE_API_URL。"

        knowledge_base = domain_text or knowledge_config.get_default_knowledge_base()
        try:
            await self._emit_progress(
                context,
                5,
                "initializing",
                "正在准备知识检索请求",
                status_details={"execution": {"query_length": len(query_text), "domain": knowledge_base}},
            )
            request_data = self._build_request_payload(query_text, knowledge_base)

            await self._emit_progress(
                context,
                25,
                "searching",
                "正在检索知识库",
                status_details={"execution": {"knowledge_base": knowledge_base}},
            )
            raw_results = await self._call_knowledge_api(knowledge_api_url, request_data)

            await self._emit_progress(
                context,
                55,
                "processing",
                "正在整理召回结果",
                status_details={"execution": {"knowledge_base": knowledge_base}},
            )
            passages = self._process_api_results(raw_results)

            await self._emit_progress(
                context,
                80,
                "analyzing",
                "正在生成检索摘要",
                status_details={"execution": {"hit_count": len(passages)}},
            )
            answer = await self._summarize_passages(passages, query_text, context)

            payload = {
                "status": "success",
                "message": "知识检索完成",
                "data": {
                    "query": query_text,
                    "domain": knowledge_base,
                    "hit_count": len(passages),
                    "answer": answer,
                    "passages": passages[:5],
                },
            }
            await self._emit_progress(
                context,
                100,
                "finalizing",
                "知识检索完成",
                status_details={"execution": {"hit_count": len(passages), "knowledge_base": knowledge_base}},
            )
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            logger.exception("KnowledgeRetrievalTool failed: {}", exc)
            return f"错误：知识检索失败。详细信息：{self._normalize_error(exc)}"

    async def _emit_progress(
        self,
        context: "SkillContext | None",
        percent: int,
        stage: str,
        message: str,
        status_details: dict[str, Any] | None = None,
    ) -> None:
        details = {
            "percent": percent,
            "stage": stage,
            "status_text": message,
        }
        if status_details:
            details["status_details"] = status_details

        if context is not None:
            await context.emit_progress(
                percent,
                stage,
                message,
                progress_stage="skill_call",
                call_type="skill",
                tool_name=self.name,
                skill_name="knowledge_retrieval",
                status_details=status_details or {},
            )
            return

        record_trace_event(
            event_name="skill.progress",
            phase="running",
            status="running",
            component="knowledge_retrieval",
            source="skill",
            content_preview=message,
            details=details,
        )

    def _build_request_payload(self, query: str, knowledge_base: str) -> dict[str, Any]:
        return {
            "knowledgebase_name": [knowledge_base],
            "search_params": {
                "query": query,
                "retrieval_type": "hybrid_search",
                "score_threshold": 0,
                "top_k": 10,
            },
        }

    async def _call_knowledge_api(self, url: str, request_data: dict[str, Any]) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                async with session.post(url, json=request_data) as response:
                    response.raise_for_status()
                    return await response.json()
        except Exception as exc:
            http_error = classify_http_client_error("知识库检索", exc)
            raise RuntimeError(http_error.message) from exc

    def _process_api_results(self, results: dict[str, Any]) -> list[dict[str, str]]:
        data = results.get("data")
        if not isinstance(data, list):
            raise ValueError("知识库接口响应缺少 data 数组。")

        passages: list[dict[str, str]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            text = str(item.get("page_content") or "").strip()
            if not text:
                continue
            passages.append(
                {
                    "id": str(item.get("id") or ""),
                    "title": str(item.get("title") or item.get("doc_name") or ""),
                    "page_content": text,
                }
            )
        return passages

    async def _summarize_passages(
        self,
        passages: list[dict[str, str]],
        query: str,
        context: "SkillContext | None",
    ) -> str:
        if not passages:
            return "未检索到直接相关内容，请补充关键词或确认知识库是否已收录该主题。"

        excerpt = passages[:5]
        if context is None:
            bullets = [f"{idx}. {item['page_content'][:120]}" for idx, item in enumerate(excerpt, start=1)]
            return "根据知识库召回内容，整理到以下相关信息：\n" + "\n".join(bullets)

        prompt = (
            "请基于以下知识库召回内容，用中文回答用户问题。\n"
            "要求：\n"
            "1. 仅依据召回内容回答，不要编造。\n"
            "2. 先给结论，再列 2-5 条关键依据。\n"
            "3. 如果信息不足，请明确指出。\n\n"
            f"用户问题：{query}\n\n"
            f"召回内容：\n{json.dumps(excerpt, ensure_ascii=False)}"
        )
        route_profile = await context.resolve_llm_route_profile(
            task="final_answer",
            system_prompt="你是知识库问答助手，请基于检索证据给出准确、克制的中文回答。",
            prompt=prompt,
        )
        result = await context.call_llm(
            task="final_answer",
            system_prompt="你是知识库问答助手，请基于检索证据给出准确、克制的中文回答。",
            prompt=prompt,
            model=route_profile.active_model,
            max_tokens=route_profile.max_tokens,
            reasoning_effort=route_profile.reasoning_effort,
        )
        return (result or "").strip() or "已检索到相关内容，但暂时无法生成总结。"

    @staticmethod
    def _normalize_error(exc: Exception) -> str:
        text = str(exc).strip()
        return text[:300] if text else exc.__class__.__name__
