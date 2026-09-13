"""Async message queue for decoupled channel-agent communication."""

import asyncio

from ithqbot.bus.events import InboundMessage, OutboundMessage, RuntimeCommand


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.
    """

    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self.runtime_ingress: asyncio.Queue[RuntimeCommand] = asyncio.Queue()
        self.runtime_execution: asyncio.Queue[RuntimeCommand] = asyncio.Queue()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        return await self.outbound.get()

    async def publish_runtime_ingress(self, command: RuntimeCommand) -> None:
        """Publish ingress commands from transport adapters."""
        await self.runtime_ingress.put(command)

    async def consume_runtime_ingress(self) -> RuntimeCommand:
        """Consume the next ingress command."""
        return await self.runtime_ingress.get()

    async def publish_runtime_execution(self, command: RuntimeCommand) -> None:
        """Publish a command to execution workers."""
        await self.runtime_execution.put(command)

    async def consume_runtime_execution(self) -> RuntimeCommand:
        """Consume the next runtime execution command."""
        return await self.runtime_execution.get()

    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()

    @property
    def runtime_ingress_size(self) -> int:
        return self.runtime_ingress.qsize()

    @property
    def runtime_execution_size(self) -> int:
        return self.runtime_execution.qsize()
