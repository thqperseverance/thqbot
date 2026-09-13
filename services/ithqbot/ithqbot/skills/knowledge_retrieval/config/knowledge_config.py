"""Runtime configuration helpers for the knowledge retrieval skill."""

from __future__ import annotations

import os


def get_knowledge_api_url() -> str:
    """Return the retrieval endpoint configured by the deployment environment."""
    return os.getenv("ITHQBOT_KNOWLEDGE_API_URL", "").strip()


def get_default_knowledge_base() -> str:
    """Return the default knowledge base name configured by the deployment environment."""
    return os.getenv("ITHQBOT_KNOWLEDGE_BASE", "marketkgpool").strip() or "marketkgpool"
