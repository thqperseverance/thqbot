from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from loguru import logger

from ithqbot.bus.events import InboundMessage, RuntimeCommand
from ithqbot.bus.queue import MessageBus


def _command_id_for_message(msg: InboundMessage) -> str:
    metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
    return str(
        metadata.get("request_msg_id")
        or metadata.get("message_id")
        or metadata.get("trace_id")
        or f"{msg.session_key}:{int(time.time() * 1000)}"
    ).strip()


@dataclass
class RuntimeIngressAdapter:
    bus: MessageBus

    async def publish_message(self, msg: InboundMessage, *, command_type: str = "message") -> RuntimeCommand:
        command = RuntimeCommand(
            command_type=command_type,
            payload=msg,
            command_id=_command_id_for_message(msg),
            session_key=msg.session_key,
            metadata=dict(msg.metadata or {}),
        )
        await self.bus.publish_runtime_ingress(command)
        return command


class RuntimeScheduler:
    """Decouple transport ingress from runtime execution ownership."""

    def __init__(self, bus: MessageBus, *, dedupe_window: int = 2000):
        self.bus = bus
        self._running = False
        self._dedupe_window = max(16, int(dedupe_window))
        self._recent_command_ids: OrderedDict[str, None] = OrderedDict()

    def _remember(self, command_id: str) -> None:
        self._recent_command_ids[command_id] = None
        self._recent_command_ids.move_to_end(command_id)
        while len(self._recent_command_ids) > self._dedupe_window:
            self._recent_command_ids.popitem(last=False)

    def _seen(self, command_id: str) -> bool:
        return command_id in self._recent_command_ids

    async def run(self) -> None:
        self._running = True
        logger.info("Runtime scheduler started")
        while self._running:
            try:
                command = await asyncio.wait_for(self.bus.consume_runtime_ingress(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Runtime scheduler ingress error: {}", exc)
                continue

            if self._seen(command.command_id):
                logger.info("Runtime scheduler deduped command {}", command.command_id)
                continue
            self._remember(command.command_id)
            await self.bus.publish_runtime_execution(command)

    def stop(self) -> None:
        self._running = False


class RuntimeExecutionWorker:
    """Execution worker consumes runtime commands and dispatches to a callback."""

    def __init__(self, bus: MessageBus, handler: Any):
        self.bus = bus
        self.handler = handler
        self._running = False

    async def run(self) -> None:
        self._running = True
        logger.info("Runtime execution worker started")
        while self._running:
            try:
                command = await asyncio.wait_for(self.bus.consume_runtime_execution(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Runtime execution worker receive error: {}", exc)
                continue

            await self.handler(command)

    def stop(self) -> None:
        self._running = False
