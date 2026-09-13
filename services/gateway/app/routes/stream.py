"""SSE 实时推送（D9=A）。

``GET /api/stream`` 建立一条长连接：
- 用户发出的消息、agent 的进度（含 skill/tool 名）、最终回复，都通过 Redis Pub/Sub 扇出到这里；
- 心跳注释帧避免代理层超时断连；
- 前端断线后重连即可，历史消息走 ``GET /api/conversations/{id}/messages`` 补齐。

说明（D10=A）：本轮不做 token 级流式，agent 的文字增量作为 ``status`` 事件推送，
用户看到的是"过程 + 完整回复"，而不是逐字打字机。
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from .. import realtime
from ..config import get_settings
from ..deps import current_user
from ..models import User

router = APIRouter(prefix="/api", tags=["stream"])

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _frame(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


@router.get("/stream")
async def stream(request: Request, user: User = Depends(current_user)) -> StreamingResponse:
    settings = get_settings()
    heartbeat = max(1.0, float(settings.sse_heartbeat_seconds))

    async def event_source():
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=max(8, settings.sse_queue_size))

        async def pump() -> None:
            try:
                async for event in realtime.subscribe_user_events(user.id):
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        # 客户端太慢就丢弃最旧事件，避免无限堆积
                        try:
                            queue.get_nowait()
                            queue.put_nowait(event)
                        except Exception:  # noqa: BLE001
                            pass
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 推送通道故障不应中断连接
                return

        task = asyncio.create_task(pump())
        try:
            yield _frame({"type": "ready", "user_id": user.id})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _frame(event)
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    return StreamingResponse(event_source(), media_type="text/event-stream", headers=SSE_HEADERS)
