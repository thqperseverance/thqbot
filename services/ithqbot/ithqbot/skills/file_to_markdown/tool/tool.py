"""Tool wrapper for converting MinIO files to markdown via an external service."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import tempfile
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

from ithqbot.agent.tools.base import Tool
from ithqbot.observability import record_trace_event
from ithqbot.skills.file_to_markdown.config import url_config
from ithqbot.storage import ObjectStorageSession, build_storage_session
from ithqbot.utils.http_client import classify_http_client_error

if TYPE_CHECKING:
    from ithqbot.agent.skills.base import SkillContext


class FileToMarkdownTool(Tool):
    """将对象存储文件送交转换服务，并回传 markdown 文件。"""

    def __init__(self, workspace: Any = None, config: Any = None, **kwargs: Any):
        super().__init__()
        _ = kwargs
        self._workspace = Path(workspace).resolve() if workspace else Path.cwd()
        self._config = config
        self._upload_url = url_config.get_upload_url()
        self._status_url = url_config.get_status_url()
        self._download_url = url_config.get_download_url()

    @property
    def name(self) -> str:
        return "file_to_markdown"

    @property
    def description(self) -> str:
        return "Convert files from object storage to markdown through a configured external service and return downloadable markdown artifacts."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "bucket": {
                    "type": "string",
                    "description": "源文件所在的对象存储 bucket。"
                },
                "object_names": {
                    "type": "array",
                    "items": {
                        "type": "string"
                    },
                    "description": "待转换对象的 object key 列表。"
                },
                "save_dir": {
                    "type": "string",
                    "description": "可选的临时工作目录；未提供时自动创建临时目录。"
                }
            },
            "required": ["bucket", "object_names"]
        }

    async def execute(
        self,
        bucket: str = "",
        object_names: list[str] | None = None,
        save_dir: str | None = None,
        context: "SkillContext | None" = None,
        **kwargs: Any,
    ) -> str:
        """执行文件转换与回传流程。"""
        _ = kwargs
        bucket_name = (bucket or "").strip()
        object_keys = [str(item).strip() for item in (object_names or []) if str(item).strip()]
        if not bucket_name:
            return "错误：缺少必要参数 bucket。"
        if not object_keys:
            return "错误：缺少必要参数 object_names。"
        if not self._upload_url or not self._status_url or not self._download_url:
            return (
                "错误：文件转 Markdown 服务地址未完整配置，请设置 "
                "ITHQBOT_FILE_TO_MARKDOWN_UPLOAD_URL、ITHQBOT_FILE_TO_MARKDOWN_STATUS_URL、"
                "ITHQBOT_FILE_TO_MARKDOWN_DOWNLOAD_URL。"
            )

        storage_session = self._build_storage_session(bucket_name)
        request_msg_id = getattr(context, "request_msg_id", "") if context else ""

        try:
            await self._emit_progress(
                context,
                5,
                "initializing",
                "正在准备文件转换任务",
                status_details={"execution": {"file_count": len(object_keys), "bucket": bucket_name}},
            )

            temp_dir_manager = self._create_temp_dir_manager(save_dir)
            with temp_dir_manager as temp_dir:
                temp_root = Path(temp_dir)

                await self._emit_progress(
                    context,
                    20,
                    "processing",
                    "正在从对象存储获取源文件并提交转换任务",
                    status_details={"execution": {"file_count": len(object_keys)}},
                )
                upload_result = await self._upload_from_storage(
                    storage_session,
                    bucket_name,
                    object_keys,
                    temp_root,
                )
                batch_no = str(upload_result.get("batchNo") or "").strip()
                if not batch_no:
                    raise RuntimeError("转换服务未返回批次号。")

                await self._emit_progress(
                    context,
                    45,
                    "searching",
                    "正在轮询转换状态",
                    status_details={"execution": {"batch_no": batch_no}},
                )
                status_result = await self._wait_for_task(batch_no)

                await self._emit_progress(
                    context,
                    70,
                    "processing",
                    "转换完成，正在下载结果包",
                    status_details={"execution": {"batch_no": batch_no}},
                )
                zip_path = await self._download_result(batch_no, temp_root)

                await self._emit_progress(
                    context,
                    88,
                    "finalizing",
                    "正在上传 markdown 结果到对象存储",
                    status_details={"execution": {"batch_no": batch_no}},
                )
                files = await self._extract_and_upload_md(
                    zip_file_path=zip_path,
                    storage_session=storage_session,
                    context=context,
                )
                if not files:
                    raise RuntimeError("结果包中未找到 markdown 文件。")

            payload = {
                "status": "success",
                "message": "Markdown 转换完成",
                "data": {
                    "batch_no": batch_no,
                    "source_bucket": bucket_name,
                    "source_objects": object_keys,
                    "request_msg_id": request_msg_id,
                    "task_status": status_result.get("data", {}),
                    "uploaded_count": len(files),
                },
                "files": files,
            }
            await self._emit_progress(
                context,
                100,
                "finalizing",
                "Markdown 转换完成",
                status_details={"execution": {"batch_no": batch_no, "uploaded_count": len(files)}},
            )
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            logger.exception("FileToMarkdownTool failed: {}", exc)
            return f"错误：文件转 Markdown 失败。详细信息：{self._normalize_error(exc)}"

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
                skill_name="file_to_markdown",
                status_details=status_details or {},
            )
            return

        record_trace_event(
            event_name="skill.progress",
            phase="running",
            status="running",
            component="file_to_markdown",
            source="skill",
            content_preview=message,
            details=details,
        )

    def _build_storage_session(self, bucket_name: str) -> ObjectStorageSession:
        cfg = getattr(self._config, "get_active_storage_config", lambda: None)()
        fallback: dict[str, Any] = {}
        config_bucket = bucket_name

        if cfg is not None:
            fallback = {
                "backend": getattr(cfg, "backend", "minio"),
                "endpoint": str(getattr(cfg, "endpoint", "") or "").strip(),
                "access_key": str(getattr(cfg, "access_key", "") or "").strip(),
                "secret_key": str(getattr(cfg, "secret_key", "") or "").strip(),
                "secure": bool(getattr(cfg, "secure", False)),
                "region": str(getattr(cfg, "region", "") or "").strip(),
            }
            config_bucket = str(getattr(cfg, "bucket", "") or bucket_name).strip() or bucket_name

        fallback["endpoint"] = fallback.get("endpoint") or url_config.get_minio_endpoint()
        fallback["access_key"] = fallback.get("access_key") or url_config.get_minio_access_key()
        fallback["secret_key"] = fallback.get("secret_key") or url_config.get_minio_secret_key()
        fallback["secure"] = bool(fallback.get("secure")) or url_config.get_minio_secure()

        if fallback.get("backend", "minio") != "s3" and not fallback.get("endpoint"):
            raise RuntimeError("未配置对象存储 endpoint。")

        return build_storage_session(self._config, bucket=config_bucket, fallback=fallback)

    def _create_temp_dir_manager(self, save_dir: str | None) -> tempfile.TemporaryDirectory[str]:
        if save_dir:
            root = Path(save_dir).expanduser()
            if not root.is_absolute():
                root = (self._workspace / root).resolve()
            root.mkdir(parents=True, exist_ok=True)
            return tempfile.TemporaryDirectory(prefix="file-to-md-", dir=root)
        return tempfile.TemporaryDirectory(prefix="file-to-md-")

    async def _upload_from_storage(
        self,
        storage_session: ObjectStorageSession,
        bucket: str,
        object_names: list[str],
        temp_root: Path,
    ) -> dict[str, Any]:
        temp_files: list[Path] = []
        file_handles: list[Any] = []
        batch_no = uuid.uuid4().hex
        try:
            for object_name in object_names:
                local_path = temp_root / PurePosixPath(object_name).name
                await asyncio.to_thread(
                    storage_session.client.fget_object,
                    bucket,
                    object_name,
                    str(local_path),
                )
                temp_files.append(local_path)

            files: list[tuple[str, tuple[str, Any, str]]] = []
            for local_path in temp_files:
                file_handle = local_path.open("rb")
                file_handles.append(file_handle)
                mime_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
                files.append(("files", (local_path.name, file_handle, mime_type)))

            data = {"batchNo": batch_no, "sysSource": "ithqbot"}
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(180.0, connect=30.0),
                trust_env=False,
            ) as client:
                response = await client.post(self._upload_url, data=data, files=files)
                response.raise_for_status()
                result = self._parse_json_response(response, "文件上传")
            result["batchNo"] = batch_no
            return result
        except Exception as exc:
            http_error = classify_http_client_error("文件上传", exc)
            raise RuntimeError(http_error.message) from exc
        finally:
            for handle in file_handles:
                try:
                    handle.close()
                except Exception:
                    pass

    async def _wait_for_task(self, batch_no: str) -> dict[str, Any]:
        params = {"batchNo": batch_no}
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=15.0),
            trust_env=False,
        ) as client:
            for _ in range(18):
                try:
                    response = await client.get(self._status_url, params=params)
                    response.raise_for_status()
                    result = self._parse_json_response(response, "任务状态查询")
                except Exception as exc:
                    http_error = classify_http_client_error("任务状态查询", exc)
                    raise RuntimeError(http_error.message) from exc

                state = str((result.get("data") or {}).get("batchState") or "").upper()
                success = result.get("success")
                if state == "SUCCESS":
                    return result
                if success is False or state in {"FAILED", "ERROR"}:
                    raise RuntimeError(self._extract_remote_error(result, "转换任务执行失败。"))
                await asyncio.sleep(5)

        raise RuntimeError("转换任务在超时时间内未完成。")

    async def _download_result(self, batch_no: str, temp_root: Path) -> Path:
        params = {"batchNo": batch_no}
        target_path = temp_root / f"{batch_no}.zip"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(180.0, connect=30.0),
                trust_env=False,
            ) as client:
                response = await client.get(self._download_url, params=params)
                response.raise_for_status()
        except Exception as exc:
            http_error = classify_http_client_error("结果下载", exc)
            raise RuntimeError(http_error.message) from exc

        target_path.write_bytes(response.content)
        return target_path

    async def _extract_and_upload_md(
        self,
        zip_file_path: Path,
        storage_session: ObjectStorageSession,
        context: "SkillContext | None",
    ) -> list[dict[str, Any]]:
        with tempfile.TemporaryDirectory(prefix="file-to-md-extract-") as extract_dir:
            extract_root = Path(extract_dir)
            self._safe_extract_zip(zip_file_path, extract_root)

            files: list[dict[str, Any]] = []
            prefix = self._build_result_prefix(context)
            for md_path in sorted(extract_root.rglob("*.md")):
                rel_name = PurePosixPath(md_path.relative_to(extract_root).as_posix()).name
                object_path = f"{prefix}/{rel_name}"
                mime_type = "text/markdown"
                await asyncio.to_thread(
                    storage_session.client.fput_object,
                    storage_session.bucket,
                    object_path,
                    str(md_path),
                    content_type=mime_type,
                )
                download_url = await self._build_download_url(storage_session, object_path)
                file_item = {
                    "name": md_path.name,
                    "mime": mime_type,
                    "size": md_path.stat().st_size,
                    "rel_path": object_path,
                    "storage_backend": storage_session.backend,
                    "storage_bucket": storage_session.bucket,
                    "storage_uri": storage_session.build_uri(object_path),
                    "download_url": download_url,
                    "storage": storage_session.build_storage_metadata(object_path),
                }
                file_item.update(storage_session.build_compatibility_fields(object_path))
                files.append(file_item)
            return files

    def _safe_extract_zip(self, zip_file_path: Path, destination: Path) -> None:
        with zipfile.ZipFile(zip_file_path, "r") as zip_ref:
            for member in zip_ref.infolist():
                member_name = member.filename.replace("\\", "/").lstrip("/")
                if not member_name:
                    continue
                target = (destination / member_name).resolve()
                if destination.resolve() not in target.parents and target != destination.resolve():
                    raise RuntimeError(f"压缩包包含非法路径：{member.filename}")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zip_ref.open(member, "r") as src, target.open("wb") as dst:
                    dst.write(src.read())

    async def _build_download_url(self, storage_session: ObjectStorageSession, path: str) -> str:
        try:
            return await asyncio.to_thread(
                storage_session.client.presigned_get_object,
                storage_session.bucket,
                path,
                expires=timedelta(hours=24),
            )
        except Exception:
            return ""

    def _build_result_prefix(self, context: "SkillContext | None") -> str:
        if context is None:
            return f"file_to_markdown/{uuid.uuid4().hex}"
        account_id = (context.account_id or "anonymous").strip() or "anonymous"
        chat_id = (context.chat_id or "chat").strip() or "chat"
        return f"{account_id}/{chat_id}/file_to_markdown/{uuid.uuid4().hex}"

    @staticmethod
    def _parse_json_response(response: httpx.Response, service_name: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{service_name}返回的不是合法 JSON。") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{service_name}返回结构不是对象。")
        return payload

    @staticmethod
    def _extract_remote_error(payload: dict[str, Any], fallback: str) -> str:
        for key in ("message", "error", "msg"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return fallback

    @staticmethod
    def _normalize_error(exc: Exception) -> str:
        text = str(exc).strip()
        return text[:300] if text else exc.__class__.__name__
