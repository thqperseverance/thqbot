"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from ithqbot.bus.queue import MessageBus
from ithqbot.channels.base import BaseChannel
from ithqbot.config.schema import Config


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.

    Responsibilities:
    - Initialize enabled channels (Telegram, WhatsApp, etc.)
    - Start/stop channels
    - Route outbound messages
    """

    def __init__(self, config: Config, bus: MessageBus):
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self._dispatch_task: asyncio.Task | None = None

        self._init_channels()

    def _init_channels(self) -> None:
        """Initialize channels discovered via pkgutil scan + entry_points plugins."""
        from ithqbot.channels.registry import discover_all

        groq_key = self.config.providers.groq.api_key
        kafka = None
        get_active_kafka = getattr(self.config, "get_active_kafka_config", None)
        if callable(get_active_kafka):
            kafka = get_active_kafka()
        else:
            infrastructure = getattr(self.config, "infrastructure", None)
            kafka = getattr(infrastructure, "kafka", None)

        for name, cls in discover_all().items():
            section = getattr(self.config.channels, name, None)
            if section is None:
                continue
            
            # Inject bot_id from root config if it's a dict
            if isinstance(section, dict) and "bot_id" not in section:
                if hasattr(self.config, "bot") and self.config.bot:
                    section["bot_id"] = self.config.bot.id
            if name == "icatmsg" and kafka is not None:
                if isinstance(section, dict):
                    section["kafka_servers"] = getattr(kafka, "servers", "localhost:9092")
                    section["kafka_security_protocol"] = getattr(kafka, "security_protocol", "")
                    section["kafka_sasl_mechanism"] = getattr(kafka, "sasl_mechanism", "PLAIN")
                    section["kafka_username"] = getattr(kafka, "username", "")
                    section["kafka_password"] = getattr(kafka, "password", "")
                    section["inbound_topic"] = getattr(kafka, "inbound_topic", "icatmsg_inbound")
                    section["outbound_topic"] = getattr(kafka, "outbound_topic", "icatmsg_outbound")
                else:
                    setattr(section, "kafka_servers", getattr(kafka, "servers", "localhost:9092"))
                    setattr(section, "kafka_security_protocol", getattr(kafka, "security_protocol", ""))
                    setattr(section, "kafka_sasl_mechanism", getattr(kafka, "sasl_mechanism", "PLAIN"))
                    setattr(section, "kafka_username", getattr(kafka, "username", ""))
                    setattr(section, "kafka_password", getattr(kafka, "password", ""))
                    setattr(section, "inbound_topic", getattr(kafka, "inbound_topic", "icatmsg_inbound"))
                    setattr(section, "outbound_topic", getattr(kafka, "outbound_topic", "icatmsg_outbound"))

            enabled = (
                section.get("enabled", False)
                if isinstance(section, dict)
                else getattr(section, "enabled", False)
            )
            if not enabled:
                continue
            try:
                channel = cls(section, self.bus)
                channel.transcription_api_key = groq_key
                self.channels[name] = channel
                logger.info("{} channel enabled", cls.display_name)
            except Exception as e:
                logger.warning("{} channel not available: {}", name, e)

        self._validate_allow_from()

    def _validate_allow_from(self) -> None:
        for name, ch in self.channels.items():
            if getattr(ch.config, "allow_from", None) == []:
                raise SystemExit(
                    f'Error: "{name}" has empty allowFrom (denies all). '
                    f'Set ["*"] to allow everyone, or add specific user IDs.'
                )

    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        """Start a channel and log any exceptions."""
        try:
            await channel.start()
        except Exception as e:
            logger.error("Failed to start channel {}: {}", name, e)

    async def start_all(self) -> None:
        """Start all channels and the outbound dispatcher."""
        if not self.channels:
            logger.warning("No channels enabled")
            return

        # Start outbound dispatcher
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())

        # Start channels
        tasks = []
        for name, channel in self.channels.items():
            logger.info("Starting {} channel...", name)
            tasks.append(asyncio.create_task(self._start_channel(name, channel)))

        # Wait for all to complete (they should run forever)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_all(self) -> None:
        """Stop all channels and the dispatcher."""
        logger.info("Stopping all channels...")

        # Stop dispatcher
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass

        # Stop all channels
        for name, channel in self.channels.items():
            try:
                await channel.stop()
                logger.info("Stopped {} channel", name)
            except Exception as e:
                logger.error("Error stopping {}: {}", name, e)

    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to the appropriate channel."""
        logger.info("Outbound dispatcher started")

        while True:
            try:
                msg = await asyncio.wait_for(
                    self.bus.consume_outbound(),
                    timeout=1.0
                )

                if msg.metadata.get("_progress"):
                    if msg.metadata.get("_tool_hint") and not self.config.channels.send_tool_hints:
                        continue
                    if not msg.metadata.get("_tool_hint") and not self.config.channels.send_progress:
                        continue

                channel = self.channels.get(msg.channel)
                if channel:
                    try:
                        await channel.send(msg)
                    except Exception as e:
                        logger.error("Error sending to {}: {}", msg.channel, e)
                else:
                    logger.warning("Unknown channel: {}", msg.channel)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self.channels.get(name)

    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        return {
            name: {
                "enabled": True,
                "running": channel.is_running
            }
            for name, channel in self.channels.items()
        }

    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self.channels.keys())
