"""HTTP 路由聚合。"""

from . import auth, chat, files, health, stream

__all__ = ["auth", "chat", "files", "health", "stream"]
