import asyncio
import httpx
from typing import Any
from ithqbot.agent.tools.base import Tool
from ithqbot.utils.http_client import classify_http_client_error

class WeatherTool(Tool):
    """Get current weather and forecasts from wttr.in."""

    name = "weather"
    description = "Get current weather and 3-day forecast for a location (no API key required)."
    parameters = {
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "City name, airport code, or IP address"},
            "format": {
                "type": "string", 
                "enum": ["current", "full", "compact"], 
                "default": "current",
                "description": "Output format: current (one line), full (3-day ASCII), compact (detailed one-line)"
            }
        },
        "required": ["location"]
    }

    async def execute(self, location: str, format: str = "current", cancellation_token: Any = None, **kwargs: Any) -> str:
        self.throw_if_cancelled(cancellation_token)
        # Map formats to wttr.in query params
        if format == "current":
            url = f"https://wttr.in/{location}?format=3"
        elif format == "compact":
            url = f"https://wttr.in/{location}?format=%l:+%c+%t+%h+%w"
        else: # full
            url = f"https://wttr.in/{location}?T"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url)
                r.raise_for_status()
                self.throw_if_cancelled(cancellation_token)
                return r.text.strip()
        except Exception as e:
            error = classify_http_client_error("天气查询", e)
            return error.to_text()
