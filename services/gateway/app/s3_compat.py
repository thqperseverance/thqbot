from __future__ import annotations

from pathlib import Path

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError


def normalize_endpoint_url(endpoint: str, *, secure: bool) -> str:
    value = str(endpoint or "").strip()
    if not value:
        raise ValueError("object storage endpoint is required")
    if "://" in value:
        return value
    scheme = "https" if secure else "http"
    return f"{scheme}://{value}"


DEFAULT_REGION = "us-east-1"


class S3CompatClient:
    def __init__(
        self,
        endpoint: str,
        *,
        access_key: str,
        secret_key: str,
        secure: bool = False,
        region: str = "",
    ) -> None:
        endpoint_url = normalize_endpoint_url(endpoint, secure=secure)
        # 注意：不能用 endpoint 的 hostname 当 region —— 自建 MinIO 常见形如
        # 127.0.0.1:9000 / minio:9000，boto3 会直接抛 InvalidRegionError。
        # MinIO 本身忽略 region，因此缺省用合法的 us-east-1。
        region_name = str(region or "").strip() or DEFAULT_REGION
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=str(access_key or "").strip() or None,
            aws_secret_access_key=str(secret_key or "").strip() or None,
            region_name=region_name,
            config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def bucket_exists(self, bucket: str) -> bool:
        try:
            self._client.head_bucket(Bucket=bucket)
            return True
        except ClientError:
            return False

    def make_bucket(self, bucket: str) -> None:
        self._client.create_bucket(Bucket=bucket)

    def put_bytes(self, bucket: str, object_name: str, data: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=bucket,
            Key=object_name,
            Body=data,
            ContentType=content_type or "application/octet-stream",
        )

    def get_object(self, bucket: str, object_name: str):
        response = self._client.get_object(Bucket=bucket, Key=object_name)
        return response["Body"], response

    def download_to_file(self, bucket: str, object_name: str, file_path: str) -> None:
        response, _ = self.get_object(bucket, object_name)
        target = Path(file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
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

    def presigned_get_object(self, bucket: str, object_name: str, expires_seconds: int) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": object_name},
            ExpiresIn=max(1, int(expires_seconds)),
        )

    def delete_object(self, bucket: str, object_name: str) -> None:
        self._client.delete_object(Bucket=bucket, Key=object_name)

    def stat_object(self, bucket: str, object_name: str) -> dict:
        return self._client.head_object(Bucket=bucket, Key=object_name)
