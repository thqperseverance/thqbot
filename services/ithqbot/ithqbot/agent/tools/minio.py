"""Backward-compatible MinIO tool aliases.

The real implementation lives in `ithqbot.agent.tools.storage`.
"""

from typing import Any

from ithqbot.agent.tools.storage import StorageFetchTool, StoragePushTool


class MinioFetchTool(StorageFetchTool):
    @property
    def name(self) -> str:
        return "minio_fetch"

    @property
    def description(self) -> str:
        return "Fetch a file from the shared object storage using its relative path (rel_path)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "rel_path": {
                    "type": "string",
                    "description": "The relative path of the file in object storage (from file_meta).",
                }
            },
            "required": ["rel_path"],
        }


class MinioPushTool(StoragePushTool):
    @property
    def name(self) -> str:
        return "minio_push"

    @property
    def description(self) -> str:
        return "Push a local file from your workspace to shared object storage and return a file_meta JSON block."
