"""创建 MVP 使用的 MinIO/S3 bucket（幂等）。

从环境变量读取：MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY /
MINIO_BUCKET / MINIO_SECURE。

用法（在仓库根执行，先把 .env 里的值导入环境）::

    python scripts/init_minio.py
"""

from __future__ import annotations

import os
import sys

from minio import Minio


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def main() -> int:
    endpoint = os.environ.get("MINIO_ENDPOINT", "").strip()
    access_key = os.environ.get("MINIO_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("MINIO_SECRET_KEY", "").strip()
    bucket = os.environ.get("MINIO_BUCKET", "ithqbot-storage").strip() or "ithqbot-storage"
    secure = _bool_env("MINIO_SECURE", False)

    missing = [
        name
        for name, value in (
            ("MINIO_ENDPOINT", endpoint),
            ("MINIO_ACCESS_KEY", access_key),
            ("MINIO_SECRET_KEY", secret_key),
        )
        if not value
    ]
    if missing:
        print("missing env vars: " + ", ".join(missing), file=sys.stderr)
        return 2

    client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
    if client.bucket_exists(bucket):
        print(f"bucket already exists: {bucket} @ {endpoint}")
        return 0
    client.make_bucket(bucket)
    print(f"created bucket: {bucket} @ {endpoint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
