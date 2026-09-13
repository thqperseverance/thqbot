from __future__ import annotations

from fastapi import FastAPI, Response, status
import uvicorn
from loguru import logger

from ithqbot.channels.manager import ChannelManager
from ithqbot.observability import get_observability_status

app = FastAPI(title="ithqbot Health API")

# Shared state
_channel_manager: ChannelManager | None = None

def set_health_context(channel_manager: ChannelManager | None) -> None:
    global _channel_manager
    _channel_manager = channel_manager

@app.get("/health")
async def health():
    """Liveness check."""
    return {"status": "ok"}

@app.get("/ready")
async def ready(response: Response):
    """Readiness check."""
    statuses = {}
    
    # 1. Observability (Redis) Status
    obs_status = get_observability_status()
    statuses["observability"] = obs_status
    
    # 2. Channels (Kafka) Status
    if _channel_manager:
        channels_status = _channel_manager.get_status()
        statuses["channels"] = channels_status
        kafka_healthy = any(ch.get("running", False) for name, ch in channels_status.items() if name == "icatmsg")
    else:
        kafka_healthy = True # If not initialized yet, assume healthy (or unknown)
    
    overall_healthy = obs_status.get("healthy", True) and kafka_healthy
    
    if not overall_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unhealthy", "components": statuses}
        
    return {"status": "healthy", "components": statuses}

async def start_health_server(port: int, channel_manager: ChannelManager | None) -> None:
    """Start the health check server."""
    set_health_context(channel_manager)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    logger.info("Starting health check server on port {}", port)
    await server.serve()
