"""Tool for comparing two documents and summarizing differences."""

import asyncio
import difflib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from ithqbot.agent.skills.llm_router import LLMTaskRouter
from ithqbot.agent.tools.base import Tool
from ithqbot.config.schema import RouteProfile, RoutePurpose
from ithqbot.observability import record_trace_event
from ithqbot.providers.base import LLMResponse
from ithqbot.skills.doc_compare.direct_handler import handle_direct_doc_compare
from ithqbot.storage import build_storage_session, is_storage_uri, parse_storage_uri
from ithqbot.utils.helpers import estimate_message_tokens, extract_text_from_file_bytes

if TYPE_CHECKING:
    from ithqbot.agent.skills.base import SkillContext

class DocCompareTool(Tool):
    """Tool to compare two documents and provide a summary of differences."""

    def __init__(self, workspace: Path, config: Any, provider_factory: Any):
        super().__init__()
        self._workspace = workspace
        self._config = config
        self._provider_factory = provider_factory
        self._fixed_otp_code = "246810"
        self._report_cache: dict[str, str] = {}
        self._error_prefix = "错误："
        self._llm_router: LLMTaskRouter | None = None

    @property
    def name(self) -> str:
        return "doc_compare"

    @property
    def description(self) -> str:
        return (
            "Compare two text documents (local or object-storage URI) and provide a detailed markdown report of differences "
            "using a reasoning LLM. Use this to find changes, additions, or inconsistencies between two files."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path1": {
                    "type": "string",
                    "description": "Path to the first document (local path, minio://bucket/path, or s3://bucket/path)."
                },
                "path2": {
                    "type": "string",
                    "description": "Path to the second document (local path, minio://bucket/path, or s3://bucket/path)."
                },
                "download": {
                    "type": "boolean",
                    "description": "Whether to generate a downloadable report file."
                },
                "otp_code": {
                    "type": "string",
                    "description": "OTP code required when download=true."
                }
            },
            "required": ["path1", "path2"]
        }

    @property
    def direct_handler(self):
        return handle_direct_doc_compare

    async def execute(
        self,
        path1: str = "",
        path2: str = "",
        download: bool = False,
        otp_code: str | None = None,
        context: "SkillContext | None" = None,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> str:
        """Execute the comparison logic."""
        self.throw_if_cancelled(cancellation_token)
        path1 = str(path1 or kwargs.get("path1") or "").strip()
        path2 = str(path2 or kwargs.get("path2") or "").strip()
        if not path1 or not path2:
            return f"{self._error_prefix}缺少必要参数 path1/path2。"
        download = bool(download or kwargs.get("download", False))
        otp_raw = otp_code if otp_code is not None else kwargs.get("otp_code")
        otp_code = str(otp_raw).strip() if otp_raw is not None else None
        record_trace_event(
            event_name="skill.progress",
            phase="running",
            status="running",
            component="doc_compare",
            source="skill",
            content_preview="正在开始文档对比分析...",
            details={"percent": 5, "stage": "initializing", "status_text": "正在开始文档对比分析..."}
        )
        try:
            report = await self._build_compare_report(path1, path2, context=context)
            self.throw_if_cancelled(cancellation_token)
        except Exception as first_error:
            # Special high-level catch for reasoning task failures that escaped _chat_with_model
            if self._looks_like_reasoning_task_failure(first_error):
                logger.warning(
                    "DocCompareTool high-level catch for reasoning failure: {}. Retrying without context.",
                    first_error,
                )
                try:
                    report = await self._build_compare_report(path1, path2, context=None)
                    self.throw_if_cancelled(cancellation_token)
                except Exception as fallback_error:
                    logger.exception(f"DocCompareTool failed after high-level fallback: {fallback_error}")
                    report = await self._build_rule_based_report_from_exception(path1, path2, fallback_error)
            else:
                logger.exception(f"DocCompareTool failed: {first_error}")
                report = await self._build_rule_based_report_from_exception(path1, path2, first_error)
        try:
            if self._is_error_text(report):
                return report
            if not download:
                interaction = self._build_otp_interaction(path1, path2)
                payload = {
                    "_ithqbot_event": "doc_compare",
                    "llm_result": (
                        f"{report}\n\n"
                        "如需下载本次对比结果，请提示用户输入验证码，再调用 doc_compare(download=true, otp_code=...)。"
                    ),
                    "interaction_message": "请输入验证码后下载本次文档对比结果。",
                    "interaction": interaction,
                }
                return json.dumps(payload, ensure_ascii=False)
            if (otp_code or "").strip() != self._fixed_otp_code:
                interaction = self._build_otp_interaction(path1, path2)
                payload = {
                    "_ithqbot_event": "doc_compare",
                    "llm_result": "验证码错误，暂时无法下载对比结果。",
                    "interaction_message": "验证码错误，请重新输入。",
                    "interaction": interaction,
                }
                return json.dumps(payload, ensure_ascii=False)
            output = await self._export_report(report, path1, path2)
            file_items: list[dict[str, Any]] = []
            if output.get("rel_path"):
                file_size = 0
                local_path_text = output.get("local_path")
                if isinstance(local_path_text, str) and local_path_text.strip():
                    try:
                        file_size = Path(local_path_text).stat().st_size
                    except OSError:
                        file_size = 0
                file_item = {
                    "name": output.get("filename"),
                    "mime": "text/markdown",
                    "rel_path": output.get("rel_path"),
                    "size": file_size,
                    "storage_backend": output.get("storage_backend") or ("minio" if output.get("minio_uri") else "s3" if output.get("s3_uri") else None),
                    "storage_bucket": output.get("bucket"),
                    "storage_uri": output.get("storage_uri"),
                    "download_url": output.get("download_url"),
                    "storage": {
                        "backend": output.get("storage_backend") or ("minio" if output.get("minio_uri") else "s3" if output.get("s3_uri") else None),
                        "bucket": output.get("bucket"),
                        "path": output.get("rel_path"),
                    },
                }
                if output.get("minio_uri"):
                    file_item["minio_uri"] = output.get("minio_uri")
                if output.get("s3_uri"):
                    file_item["s3_uri"] = output.get("s3_uri")
                file_items.append(file_item)
            download_url = (output.get("download_url") or "").strip()
            llm_result = "对比结果已生成，请点击下载。"
            if download_url:
                llm_result = f"对比结果已生成：[点击下载对比报告]({download_url})"
            payload = {
                "_ithqbot_event": "doc_compare",
                "llm_result": llm_result,
                "file_message": "文档对比结果已生成，请下载文件查看。",
                "files": file_items,
            }
            return json.dumps(payload, ensure_ascii=False)
        except Exception as e:
            logger.exception(f"DocCompareTool failed: {e}")
            return f"{self._error_prefix}文档比较失败，请稍后重试。详细信息：{str(e)}"

    async def _build_compare_report(
        self,
        path1: str,
        path2: str,
        context: "SkillContext | None" = None,
    ) -> str:
        cache_key = f"{path1}|||{path2}"
        cached = self._report_cache.get(cache_key)
        if cached:
            return cached
        record_trace_event(
            event_name="skill.progress",
            phase="running",
            status="running",
            component="doc_compare",
            source="skill",
            content_preview="正在读取并解析两个文档内容...",
            details={"percent": 15, "stage": "reading", "status_text": "正在读取并解析两个文档内容..."}
        )
        content1 = await self._get_content(path1)
        content2 = await self._get_content(path2)

        record_trace_event(
            event_name="skill.progress",
            phase="running",
            status="running",
            component="doc_compare",
            source="skill",
            content_preview="正在提交大模型进行对比分析...",
            details={"percent": 45, "stage": "analyzing", "status_text": "正在提交大模型进行对比分析..."}
        )
        default_model = self._config.agents.defaults.model
        content1 = self._truncate_to_safe_limit(content1, 25000)
        content2 = self._truncate_to_safe_limit(content2, 25000)
        prompt = f"""请用中文对以下两个文档进行详细比较，输出纯文本报告，不要使用 Markdown 语法符号（例如 #、##、-、*）。
报告结构固定为三段，并使用中文标题：
1）摘要：概括主要异同。
2）关键差异：逐条列出具体差异点。
3）综合判断：说明两份文档的适用场景或完整性差异。

文档1（路径：{path1}）：
{content1}

文档2（路径：{path2}）：
{content2}
"""
        messages = [
            {
                "role": "system",
                "content": "你是专业的文档对比分析助手。请始终使用中文输出，内容准确、简洁、可读。"
            },
            {"role": "user", "content": prompt}
        ]
        route_profile = await self._resolve_route_profile(
            task="final_answer",
            prompt=prompt,
            messages=messages,
        )
        response = await self._chat_with_model(route_profile, messages, context=context)
        if (
            self._is_error_response(response)
            and route_profile.active_model != default_model
            and self._looks_like_model_config_error(response.content)
        ):
            logger.warning(
                "DocCompareTool retrying with default model after reasoning model failure: {}",
                route_profile.active_model,
            )
            response = await self._chat_with_model(
                RouteProfile(
                    purpose=RoutePurpose.FINAL_ANSWER,
                    active_model=default_model,
                    fallback_models=[],
                    source="doc_compare_default_fallback",
                ),
                messages,
                context=context,
            )
        if self._is_error_response(response):
            reason = self._normalize_error_message(response.content)
            logger.warning("DocCompareTool using rule-based fallback report due to LLM failure: {}", reason)
            report = self._build_rule_based_report(path1, path2, content1, content2, reason=reason)
            self._report_cache[cache_key] = report
            return report
        report = (response.content or "").strip() or f"{self._error_prefix}文档比较失败：模型返回为空。"
        if not self._is_error_text(report):
            self._report_cache[cache_key] = report
        return report

    async def _build_rule_based_report_from_exception(
        self,
        path1: str,
        path2: str,
        error: Exception,
    ) -> str:
        reason = self._normalize_error_message(str(error))
        try:
            content1, content2 = await asyncio.gather(
                self._get_content(path1),
                self._get_content(path2),
            )
            report = self._build_rule_based_report(
                path1,
                path2,
                self._truncate_to_safe_limit(content1, 25000),
                self._truncate_to_safe_limit(content2, 25000),
                reason=reason,
            )
            self._report_cache[f"{path1}|||{path2}"] = report
            return report
        except Exception as fallback_exc:
            logger.exception(
                "DocCompareTool failed building emergency rule-based report: {}",
                fallback_exc,
            )
            # Last-resort fallback: still return a rule-based skeleton report instead of a hard error.
            degraded_reason = (
                f"{reason}；附加信息：规则兜底阶段读取文档失败（{self._normalize_error_message(str(fallback_exc))}）"
            )
            return self._build_rule_based_report(
                path1,
                path2,
                "",
                "",
                reason=degraded_reason,
            )

    def _build_otp_interaction(self, path1: str, path2: str) -> dict[str, Any]:
        return {
            "version": "v1",
            "interaction_id": f"doc-compare-{abs(hash(f'{path1}|{path2}'))}",
            "type": "otp",
            "title": "下载对比结果需要验证码",
            "prompt": "请输入验证码后下载本次文档对比结果",
            "submit_label": "验证并下载",
            "cancel_label": "取消",
            "fields": [
                {
                    "key": "otp_code",
                    "label": "验证码",
                    "input_type": "otp",
                    "required": True
                }
            ],
            "expires_in_seconds": 600,
            "sensitive": True,
            "context": {
                "tool": "doc_compare",
                "path1": path1,
                "path2": path2,
                "download": True
            }
        }

    async def _export_report(self, report: str, path1: str, path2: str) -> dict[str, str]:
        report_dir = self._workspace / ".ithqbot" / "doc_compare_reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        p1 = Path(path1).name.replace("/", "_")
        p2 = Path(path2).name.replace("/", "_")
        filename = f"doc_compare_{ts}_{p1}_vs_{p2}.md"
        local_path = report_dir / filename
        record_trace_event(
            event_name="skill.progress",
            phase="running",
            status="running",
            component="doc_compare",
            source="skill",
            content_preview="正在导出对比报告文件...",
            details={"percent": 85, "stage": "exporting", "status_text": "正在导出对比报告文件..."}
        )
        local_path.write_text(report, encoding="utf-8")
        storage_info = await self._upload_report_to_storage(local_path, filename)
        return {
            "filename": filename,
            "local_path": str(local_path),
            **storage_info,
        }

    async def _upload_report_to_storage(self, local_path: Path, filename: str) -> dict[str, str]:
        session = build_storage_session(self._config)
        if not session.bucket:
            return {}
        rel_path = f"doc_compare_reports/{filename}"
        payload = {
            "rel_path": rel_path,
            "bucket": session.bucket,
            "download_url": "",
            "storage_backend": session.backend,
            "storage_uri": session.build_uri(rel_path),
        }
        payload.update(session.build_compatibility_fields(rel_path))
        try:
            await asyncio.to_thread(session.client.fput_object, session.bucket, rel_path, str(local_path))
        except Exception as exc:
            logger.warning("DocCompareTool storage upload failed for {}: {}", rel_path, exc)
            return payload
        download_url = ""
        try:
            download_url = await asyncio.to_thread(
                session.client.presigned_get_object,
                session.bucket,
                rel_path,
                expires=timedelta(hours=24),
            )
        except Exception:
            download_url = ""
        payload["download_url"] = download_url
        return payload

    async def _upload_report_to_minio(self, local_path: Path, filename: str) -> tuple[str, str, str, str]:
        payload = await self._upload_report_to_storage(local_path, filename)
        return (
            str(payload.get("minio_uri") or payload.get("storage_uri") or ""),
            str(payload.get("rel_path") or ""),
            str(payload.get("bucket") or ""),
            str(payload.get("download_url") or ""),
        )

    def _get_llm_router(self) -> LLMTaskRouter:
        if self._llm_router is None:
            self._llm_router = LLMTaskRouter(
                config=self._config,
                provider_factory=self._provider_factory,
                skill_name=self.name,
            )
        return self._llm_router

    async def _resolve_route_profile(
        self,
        *,
        task: str,
        prompt: str,
        messages: list[dict[str, str]],
    ) -> RouteProfile:
        agents = getattr(self._config, "agents", None)
        routing = getattr(agents, "routing", None)
        if routing is None:
            mapped_model = None
            purposes = getattr(agents, "purposes", None)
            if isinstance(purposes, dict):
                mapped_model = purposes.get(task)
                if not mapped_model and task == "final_answer":
                    mapped_model = purposes.get("reasoning")
            default_model = str(getattr(getattr(agents, "defaults", None), "model", "") or "")
            return RouteProfile(
                purpose=RoutePurpose.FINAL_ANSWER,
                active_model=str(mapped_model or default_model),
                fallback_models=[],
                source="doc_compare_legacy",
            )
        return await self._get_llm_router().resolve_route_profile(
            task=task,
            prompt=prompt,
            messages=messages,
        )

    async def _chat_with_model(
        self,
        route_profile: RouteProfile,
        messages: list[dict[str, str]],
        context: "SkillContext | None" = None,
    ) -> LLMResponse:
        """
        Chat with a model, either via task-routed context or direct provider fallback.
        """
        last_exception = None
        model = route_profile.active_model
        fallback_models = list(route_profile.fallback_models)

        # 1. Try task-routed call if context is available
        if context is not None:
            logger.info("DocCompareTool attempting task-routed call (reasoning)")
            try:
                result = await context.call_llm(
                    task="final_answer",
                    messages=messages,
                    model=model,
                    max_tokens=route_profile.max_tokens,
                    reasoning_effort=route_profile.reasoning_effort,
                )
                if result:
                    logger.info("DocCompareTool task-routed call SUCCEEDED")
                    return LLMResponse(content=str(result), finish_reason="stop")
            except Exception as e:
                last_exception = e
                logger.info("DocCompareTool task-routed call FAILED: {} (falling back to direct provider)", e)

        # 2. Try direct provider call (requested model)
        try:
            if not model:
                raise ValueError("No model specified for direct provider call")
            logger.info("DocCompareTool attempting direct provider call for model: {}", model)
            llm = self._provider_factory(model)
            if llm:
                resp = await llm.chat_with_retry(
                    messages=messages,
                    model=model,
                    max_tokens=route_profile.max_tokens,
                    reasoning_effort=route_profile.reasoning_effort,
                )
                # If it's a success or a different kind of error, return it
                if resp.finish_reason != "error" or not last_exception:
                    logger.info("DocCompareTool direct provider call COMPLETED (status: {})", resp.finish_reason)
                    return resp
            else:
                logger.warning("DocCompareTool provider_factory returned None for {}", model)
        except Exception as e:
            last_exception = last_exception or e
            logger.info("DocCompareTool direct provider call FAILED for {}: {}", model, e)

        for fallback_model in fallback_models:
            try:
                logger.info("DocCompareTool attempting route fallback model: {}", fallback_model)
                llm = self._provider_factory(fallback_model)
                if llm:
                    resp = await llm.chat_with_retry(
                        messages=messages,
                        model=fallback_model,
                        max_tokens=route_profile.max_tokens,
                        reasoning_effort=route_profile.reasoning_effort,
                    )
                    if resp.finish_reason != "error" or not last_exception:
                        logger.info(
                            "DocCompareTool route fallback COMPLETED for {} (status: {})",
                            fallback_model,
                            resp.finish_reason,
                        )
                        return resp
            except Exception as e:
                last_exception = last_exception or e
                logger.info("DocCompareTool route fallback FAILED for {}: {}", fallback_model, e)

        # 3. Last ditch fallback to default model
        default_model = getattr(self._config.agents.defaults, "model", None)
        if default_model and default_model != model:
            try:
                logger.info("DocCompareTool attempting fallback to default model: {}", default_model)
                llm = self._provider_factory(default_model)
                if llm:
                    return await llm.chat_with_retry(
                        messages=messages,
                        model=default_model,
                        max_tokens=route_profile.max_tokens,
                        reasoning_effort=route_profile.reasoning_effort,
                    )
            except Exception as e:
                last_exception = e
                logger.info("DocCompareTool fallback model call FAILED for {}: {}", default_model, e)

        # 4. Return error response if all failed
        logger.info("DocCompareTool all LLM steps FAILED. Falling back to rule-based report.")
        return LLMResponse(
            content=f"LLM comparison failed after all retries. Last error: {last_exception}",
            finish_reason="error",
        )

    @staticmethod
    def _is_error_response(response: LLMResponse) -> bool:
        return response.finish_reason == "error"

    @staticmethod
    def _looks_like_model_config_error(content: str | None) -> bool:
        text = (content or "").lower()
        markers = (
            "model not found",
            "no such model",
            "unknown model",
            "invalid model",
            "does not exist",
            "模型不存在",
            "model does not exist",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _normalize_error_message(content: str | None) -> str:
        text = (content or "").strip()
        if not text:
            return "比较模型暂时不可用"
        return " ".join(text.split())[:500]

    @staticmethod
    def _looks_like_reasoning_task_failure(error: Exception) -> bool:
        text = str(error or "").strip().lower()
        return "llm call failed for task 'reasoning'" in text or "task \"reasoning\"" in text

    def _build_rule_based_report(
        self,
        path1: str,
        path2: str,
        content1: str,
        content2: str,
        reason: str,
    ) -> str:
        lines1 = [line.strip() for line in content1.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        lines2 = [line.strip() for line in content2.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        matcher = difflib.SequenceMatcher(a=lines1, b=lines2)
        replace_count = 0
        add_count = 0
        delete_count = 0
        samples: list[str] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            if tag == "replace":
                replace_count += max(i2 - i1, j2 - j1)
            elif tag == "insert":
                add_count += j2 - j1
            elif tag == "delete":
                delete_count += i2 - i1
            if len(samples) >= 5:
                continue
            left = " ".join(item for item in lines1[i1:i2] if item)[:80]
            right = " ".join(item for item in lines2[j1:j2] if item)[:80]
            if tag == "insert":
                samples.append(f"新增：{right or '（空）'}")
            elif tag == "delete":
                samples.append(f"删除：{left or '（空）'}")
            else:
                samples.append(f"变更：{left or '（空）'} -> {right or '（空）'}")

        ratio = matcher.ratio()
        similarity = f"{ratio * 100:.1f}%"
        if not samples:
            samples.append("两份文档文本高度一致，未识别到明显差异片段。")
        summary = (
            "摘要：由于比较模型暂时不可用，已使用规则比对生成结果。"
            f"对比文件为 {Path(path1).name} 与 {Path(path2).name}，"
            f"相似度约 {similarity}，共识别新增 {add_count} 处、删除 {delete_count} 处、变更 {replace_count} 处。"
        )
        key_diff = "关键差异：" + "；".join(samples)
        judgement = (
            "综合判断：当前结果基于文本规则比对，适合快速定位差异。"
            f"若需语义级分析，请稍后重试模型对比。失败原因：{reason}"
        )
        return "\n".join((summary, key_diff, judgement))

    def _is_error_text(self, text: str | None) -> bool:
        raw = (text or "").strip()
        return raw.startswith("Error:") or raw.startswith(self._error_prefix)

    async def _get_content(self, path: str) -> str:
        """Fetch content from local path or object storage."""
        if is_storage_uri(str(path)):
            return await self._fetch_from_storage(path)

        # Local path
        p = Path(path)
        if not p.is_absolute():
            p = self._workspace / p

        if not p.exists():
            storage_candidate = self._as_default_storage_path(path)
            if storage_candidate:
                try:
                    if storage_candidate.startswith("minio://"):
                        return await self._fetch_from_minio(storage_candidate)
                    return await self._fetch_from_storage(storage_candidate)
                except Exception as exc:
                    logger.warning("DocCompareTool fallback storage read failed for {}: {}", storage_candidate, exc)
            raise FileNotFoundError(f"文件不存在：{path}")

        data = p.read_bytes()
        return extract_text_from_file_bytes(data, path)

    def _as_default_storage_path(self, path: str) -> str | None:
        text = (path or "").strip()
        if not text or text.startswith(("minio://", "s3://", "http://", "https://", "/")):
            return None
        if "/" not in text:
            return None
        cfg = getattr(self._config, "get_active_storage_config", lambda: None)()
        if cfg is None:
            tools = getattr(self._config, "tools", None)
            cfg = getattr(tools, "storage", None) or getattr(tools, "minio", None)
        bucket = getattr(cfg, "bucket", "") if cfg else ""
        backend = getattr(cfg, "backend", "minio") if cfg else "minio"
        if not bucket:
            return None
        return f"{backend}://{bucket}/{text}"

    def _truncate_to_safe_limit(self, text: str, max_tokens: int) -> str:
        """Truncate text if it exceeds token limit."""
        if not text:
            return ""
        # Simple estimation: 1 token ~ 4 chars for non-tiktoken fallback
        if len(text) < max_tokens:
            return text

        # Use existing helper to get accurate count
        tokens = estimate_message_tokens({"content": text})
        if tokens <= max_tokens:
            return text

        # Truncate and add notice
        ratio = max_tokens / tokens
        keep_len = int(len(text) * ratio)
        return text[:keep_len] + "\n\n... [Content truncated due to size] ..."

    async def _fetch_from_storage(self, url: str) -> str:
        """Fetch from object storage."""
        _, bucket, rel_path = parse_storage_uri(url)
        session = build_storage_session(self._config, bucket=bucket)
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name

        try:
            await asyncio.to_thread(session.client.fget_object, session.bucket, rel_path, tmp_path)
            data = Path(tmp_path).read_bytes()
            return extract_text_from_file_bytes(data, rel_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    async def _fetch_from_minio(self, url: str) -> str:
        return await self._fetch_from_storage(url)
