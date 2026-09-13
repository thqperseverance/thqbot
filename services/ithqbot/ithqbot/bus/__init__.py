"""Message bus module for decoupled channel-agent communication."""

from ithqbot.bus.events import InboundMessage, OutboundMessage
from ithqbot.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
