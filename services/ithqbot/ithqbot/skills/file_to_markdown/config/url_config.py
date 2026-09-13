"""Runtime configuration helpers for the file-to-markdown skill."""

from __future__ import annotations

import os


def get_minio_endpoint() -> str:
    return os.getenv("ITHQBOT_FILE_TO_MARKDOWN_MINIO_ENDPOINT", "").strip()


def get_minio_access_key() -> str:
    return os.getenv("ITHQBOT_FILE_TO_MARKDOWN_MINIO_ACCESS_KEY", "").strip()


def get_minio_secret_key() -> str:
    return os.getenv("ITHQBOT_FILE_TO_MARKDOWN_MINIO_SECRET_KEY", "").strip()


def get_minio_secure() -> bool:
    return os.getenv("ITHQBOT_FILE_TO_MARKDOWN_MINIO_SECURE", "").strip().lower() in {"1", "true", "yes"}


def get_upload_url() -> str:
    return os.getenv("ITHQBOT_FILE_TO_MARKDOWN_UPLOAD_URL", "").strip()


def get_status_url() -> str:
    return os.getenv("ITHQBOT_FILE_TO_MARKDOWN_STATUS_URL", "").strip()


def get_download_url() -> str:
    return os.getenv("ITHQBOT_FILE_TO_MARKDOWN_DOWNLOAD_URL", "").strip()
