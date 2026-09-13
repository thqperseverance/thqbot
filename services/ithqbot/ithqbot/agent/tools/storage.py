from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from ithqbot.agent.tools.base import Tool
from ithqbot.storage import build_storage_session


class StorageFetchTool(Tool):
    """Tool to fetch files from shared object storage."""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        workspace: Path,
        *,
        backend: str = "minio",
        secure: bool = False,
        region: str = "",
    ):
        super().__init__()
        self._workspace = workspace
        self._settings = {
            "backend": backend,
            "endpoint": endpoint,
            "access_key": access_key,
            "secret_key": secret_key,
            "bucket": bucket,
            "secure": secure,
            "region": region,
        }
        self._bucket = bucket
        self._client = self._build_client()
        self._max_attempts = 3
        self._base_delay_seconds = 0.3

    def _build_client(self):
        session = build_storage_session(None, bucket=self._bucket, fallback=self._settings)
        self._bucket = session.bucket
        return session.client

    def _reconnect(self) -> None:
        self._client = self._build_client()

    @property
    def client(self):
        return self._client

    @client.setter
    def client(self, value) -> None:
        self._client = value

    async def _run_io(self, operation: str, fn, cancellation_token: Any | None = None) -> None:
        import asyncio

        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                self.throw_if_cancelled(cancellation_token)
                await asyncio.to_thread(fn)
                self.throw_if_cancelled(cancellation_token)
                return
            except Exception as exc:
                last_error = exc
                if attempt >= self._max_attempts:
                    break
                delay = min(self._base_delay_seconds * (2 ** (attempt - 1)), 2.0)
                logger.warning(
                    "Storage {} failed on attempt {}/{}: {}",
                    operation,
                    attempt,
                    self._max_attempts,
                    exc,
                )
                await asyncio.sleep(delay)
                self._reconnect()
        if last_error:
            raise last_error

    @property
    def name(self) -> str:
        return "storage_fetch"

    @property
    def description(self) -> str:
        return "Fetch a file from shared object storage using its relative path (rel_path)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "rel_path": {
                    "type": "string",
                    "description": "The relative path of the file in object storage.",
                }
            },
            "required": ["rel_path"],
        }

    async def execute(self, rel_path: str, cancellation_token: Any | None = None) -> str:
        try:
            self.throw_if_cancelled(cancellation_token)
            local_path = self._workspace / Path(rel_path).name
            logger.info("Fetching {} from object storage to {}", rel_path, local_path)
            await self._run_io(
                "fetch",
                lambda: self._client.fget_object(self._bucket, rel_path, str(local_path)),
                cancellation_token=cancellation_token,
            )
            return f"File fetched to {local_path}. You can now use read_file or other tools on it."
        except Exception as exc:
            logger.error("Storage fetch failed: {}", exc)
            return f"Error fetching file: {exc}"


class StoragePushTool(Tool):
    """Tool to push files to shared object storage."""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        workspace: Path,
        *,
        backend: str = "minio",
        secure: bool = False,
        region: str = "",
    ):
        super().__init__()
        self._workspace = workspace
        self._settings = {
            "backend": backend,
            "endpoint": endpoint,
            "access_key": access_key,
            "secret_key": secret_key,
            "bucket": bucket,
            "secure": secure,
            "region": region,
        }
        self._bucket = bucket
        self._session = self._build_session()
        self._chat_id = "default_chat"
        self._max_attempts = 3
        self._base_delay_seconds = 0.3

    def _build_session(self):
        session = build_storage_session(None, bucket=self._bucket, fallback=self._settings)
        self._bucket = session.bucket
        return session

    def _reconnect(self) -> None:
        self._session = self._build_session()

    @property
    def client(self):
        return self._session.client

    @client.setter
    def client(self, value) -> None:
        self._session.client = value

    async def _run_io(self, operation: str, fn, cancellation_token: Any | None = None) -> None:
        import asyncio

        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                self.throw_if_cancelled(cancellation_token)
                await asyncio.to_thread(fn)
                self.throw_if_cancelled(cancellation_token)
                return
            except Exception as exc:
                last_error = exc
                if attempt >= self._max_attempts:
                    break
                delay = min(self._base_delay_seconds * (2 ** (attempt - 1)), 2.0)
                logger.warning(
                    "Storage {} failed on attempt {}/{}: {}",
                    operation,
                    attempt,
                    self._max_attempts,
                    exc,
                )
                await asyncio.sleep(delay)
                self._reconnect()
        if last_error:
            raise last_error

    def set_context(self, channel: str, chat_id: str, metadata: dict[str, Any] | None = None) -> None:
        _ = channel, metadata
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "storage_push"

    @property
    def description(self) -> str:
        return "Push a local file from your workspace to shared object storage and return a file_meta JSON block."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "local_filename": {
                    "type": "string",
                    "description": "The name of the local file in your workspace to push.",
                }
            },
            "required": ["local_filename"],
        }

    async def execute(self, local_filename: str, cancellation_token: Any | None = None) -> str:
        from ithqbot import context

        self.throw_if_cancelled(cancellation_token)
        runtime_ctx = context.get_runtime_context()
        account_id = context.account_id.get()
        chat_id = runtime_ctx.get("chat_id") or self._chat_id

        if not account_id:
            return "错误：上下文缺少 account_id，无法上传文件。"

        try:
            local_path = self._workspace / local_filename
            if not local_path.exists():
                return f"错误：工作区中不存在文件 {local_filename}。"

            file_id = str(uuid.uuid4())
            ext = local_path.suffix
            rel_path = f"{account_id}/{chat_id}/{file_id}{ext}"

            logger.info("Pushing {} to object storage at {}", local_path, rel_path)

            mime_type, _ = mimetypes.guess_type(str(local_path))
            mime_type = mime_type or "application/octet-stream"
            size = local_path.stat().st_size

            await self._run_io(
                "push",
                lambda: self._session.client.fput_object(
                    self._bucket,
                    rel_path,
                    str(local_path),
                    content_type=mime_type,
                ),
                cancellation_token=cancellation_token,
            )
            self.throw_if_cancelled(cancellation_token)

            file_meta = {
                "kind": "file",
                "name": local_filename,
                "size": size,
                "mime": mime_type,
                "storage_backend": self._session.backend,
                "storage_bucket": self._bucket,
                "storage_uri": self._session.build_uri(rel_path),
                "storage": self._session.build_storage_metadata(rel_path),
                "rel_path": rel_path,
            }
            file_meta.update(self._session.build_compatibility_fields(rel_path))

            media_list = context.outbound_media.get()
            media_list.append(file_meta)

            return (
                f"File successfully pushed. The file has been automatically attached to your response.\n"
                f"You MUST include a helpful message body explaining that the file {local_filename} is ready."
            )
        except Exception as exc:
            logger.error("Storage push failed: {}", exc)
            return f"Error pushing file: {exc}"
