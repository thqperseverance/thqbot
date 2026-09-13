"""健康检查：/health 只证明进程活着；/ready 逐项检查依赖。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .. import realtime
from ..config import get_settings
from ..db import get_engine
from ..source_adapter import outbound_consumer_status

router = APIRouter(tags=["health"])


def _check_database() -> dict:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True, "backend": "postgresql"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def _check_redis() -> dict:
    ok = await realtime.ping()
    return {"ok": ok, "backend": "redis"}


def _check_kafka() -> dict:
    status = outbound_consumer_status()
    running = bool(status.get("consumer_connected")) or bool(status.get("running"))
    return {"ok": running, "detail": status}


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> JSONResponse:
    settings = get_settings()
    checks: dict[str, dict] = {
        "database": _check_database(),
        "redis": await _check_redis(),
    }
    if settings.kafka_enabled:
        checks["kafka_outbound_consumer"] = _check_kafka()

    ok = all(item.get("ok") for item in checks.values())
    payload = {"status": "ready" if ok else "degraded", "checks": checks}
    return JSONResponse(payload, status_code=200 if ok else 503)
