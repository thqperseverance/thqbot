from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Literal
from urllib.parse import urlparse

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError


def _normalize_endpoint_url(
    endpoint: str,
    *,
    secure: bool,
    backend: Literal["minio", "s3"] = "minio",
) -> str | None:
    value = str(endpoint or "").strip()
    if not value:
        if backend == "s3":
            return None
        raise ValueError("object storage endpoint is required")
    if "://" in value:
        return value
    scheme = "https" if secure else "http"
    return f"{scheme}://{value}"


def _normalize_expires_seconds(expires: Any) -> int:
    if isinstance(expires, timedelta):
        return max(1, int(expires.total_seconds()))
    try:
        return max(1, int(expires))
    except Exception:
        return 3600


class S3CompatibleClient:
    """Small adapter exposing a MinIO-like interface on top of boto3 S3 client."""

    def __init__(
        self,
        endpoint: str,
        *,
        access_key: str,
        secret_key: str,
        secure: bool = False,
        backend: Literal["minio", "s3"] = "minio",
        region_name: str | None = None,
    ) -> None:
        self.endpoint = str(endpoint or "").strip()
        self.access_key = str(access_key or "").strip()
        self.secret_key = str(secret_key or "").strip()
        self.secure = bool(secure)
        self.backend = backend
        self.endpoint_url = _normalize_endpoint_url(
            self.endpoint,
            secure=self.secure,
            backend=self.backend,
        )
        parsed = urlparse(self.endpoint_url) if self.endpoint_url else None
        hostname = (parsed.hostname or "") if parsed else ""
        # An IP address or localhost is not a valid AWS region; fall back to us-east-1.
        _is_ip_or_localhost = (
            hostname.replace(".", "").isdigit()
            or hostname == "localhost"
        )
        self._region_name = (
            str(region_name or "").strip()
            or (hostname if not _is_ip_or_localhost else "")
            or "us-east-1"
        )
        self._config = BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"})
        self._client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url or None,
            aws_access_key_id=self.access_key or None,
            aws_secret_access_key=self.secret_key or None,
            region_name=self._region_name,
            config=self._config,
        )

    def put_object(self, bucket: str, object_name: str, data: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=bucket,
            Key=object_name,
            Body=data,
            ContentType=content_type or "application/octet-stream",
        )

    def fput_object(
        self,
        bucket: str,
        object_name: str,
        file_path: str,
        content_type: str | None = None,
    ) -> None:
        extra_args: dict[str, Any] = {}
        if content_type:
            extra_args["ContentType"] = content_type
        with open(file_path, "rb") as file_obj:
            self._client.upload_fileobj(file_obj, bucket, object_name, ExtraArgs=extra_args or None)

    def get_object(self, bucket: str, object_name: str):
        return self._client.get_object(Bucket=bucket, Key=object_name)["Body"]

    def fget_object(self, bucket: str, object_name: str, file_path: str) -> None:
        target = Path(file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        response = self.get_object(bucket, object_name)
        try:
            with target.open("wb") as file_obj:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    file_obj.write(chunk)
        finally:
            try:
                response.close()
            except Exception:
                pass

    def list_objects(self, bucket: str, prefix: str = "", recursive: bool = True) -> Iterator[SimpleNamespace]:
        _ = recursive
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix or ""):
            for item in page.get("Contents", []) or []:
                yield SimpleNamespace(
                    object_name=str(item.get("Key") or ""),
                    size=int(item.get("Size") or 0),
                    last_modified=item.get("LastModified"),
                    etag=item.get("ETag"),
                )

    def presigned_get_object(self, bucket_name: str, object_name: str, expires: Any) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket_name, "Key": object_name},
            ExpiresIn=_normalize_expires_seconds(expires),
        )

    def stat_object(self, bucket: str, object_name: str) -> SimpleNamespace:
        response = self._client.head_object(Bucket=bucket, Key=object_name)
        return SimpleNamespace(
            size=int(response.get("ContentLength") or 0),
            content_type=str(response.get("ContentType") or ""),
            last_modified=response.get("LastModified"),
        )

    def bucket_exists(self, bucket: str) -> bool:
        try:
            self._client.head_bucket(Bucket=bucket)
            return True
        except ClientError:
            return False

    def make_bucket(self, bucket: str) -> None:
        self._client.create_bucket(Bucket=bucket)
