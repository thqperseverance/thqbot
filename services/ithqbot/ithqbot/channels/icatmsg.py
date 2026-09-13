import asyncio
import json
import logging
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger
from ithqbot.bus.events import InboundMessage, OutboundMessage
from ithqbot.agent.runtime import RuntimeIngressAdapter
from ithqbot.channels.base import BaseChannel
from ithqbot.config.paths import get_workspace_path
from ithqbot.observability import record_trace_event
from ithqbot.storage import normalize_storage_backend, parse_storage_uri
import pathlib
import time
import uuid

PROGRESS_STAGES = {
    "queued",
    "parsing",
    "extracting",
    "comparing",
    "finalizing",
    "tool_call",
    "skill_call",
    "graph_planning",
    "graph_ready",
    "graph_running",
    "graph_waiting",
}
MESSAGE_PRIORITY_LEVELS = {"critical", "high", "normal", "low"}
REBALANCE_RELATED_ERROR_NAMES = {
    "RebalanceInProgressError",
    "IllegalGenerationError",
    "UnknownMemberIdError",
    "CommitFailedError",
}


def _is_rebalance_related_exception(exc: Exception) -> bool:
    error_name = exc.__class__.__name__
    if error_name in REBALANCE_RELATED_ERROR_NAMES:
        return True
    message = str(exc).lower()
    return "rebalanc" in message or "group" in message and "heartbeat failed" in message


class _AIOKafkaNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage().lower()
        return not ("heartbeat failed for group" in message and "rebalanc" in message)

try:
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
    HAS_AIOKAFKA = True
except ImportError:
    HAS_AIOKAFKA = False


class AdapterValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ChannelAdapter:
    def __init__(
        self,
        name: str,
        *,
        processing_receipt: bool,
        inbound_events: set[str] | None = None,
        required_fields: tuple[str, ...] = ("account_id", "chat_id"),
    ):
        self.name = name
        self.processing_receipt = processing_receipt
        self.inbound_events = inbound_events or {"message.user"}
        self.required_fields = required_fields

    def _is_non_empty_string(self, value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def validate(self, body: dict[str, Any], event_type: str) -> None:
        if event_type not in self.inbound_events:
            raise AdapterValidationError(
                code="ICATMSG_ADAPTER_UNSUPPORTED_EVENT",
                message=f"event {event_type} is not supported by channel {self.name}",
            )
        for field in self.required_fields:
            if not self._is_non_empty_string(body.get(field)):
                raise AdapterValidationError(
                    code="ICATMSG_ADAPTER_MISSING_FIELD",
                    message=f"{field} is required",
                )
        chat_id = body.get("chat_id")
        if chat_id is not None and not isinstance(chat_id, str):
            raise AdapterValidationError(
                code="ICATMSG_ADAPTER_INVALID_CHAT_ID",
                message="chat_id must be a string",
            )
        bot_id = body.get("bot_id")
        if bot_id is not None and not isinstance(bot_id, str):
            raise AdapterValidationError(
                code="ICATMSG_ADAPTER_INVALID_BOT_ID",
                message="bot_id must be a string",
            )
        attachments = body.get("attachments")
        if attachments is None:
            return
        if not isinstance(attachments, list):
            raise AdapterValidationError(
                code="ICATMSG_ADAPTER_INVALID_ATTACHMENTS",
                message="attachments must be a list",
            )
        for item in attachments:
            if isinstance(item, str):
                continue
            if not isinstance(item, dict):
                raise AdapterValidationError(
                    code="ICATMSG_ADAPTER_INVALID_ATTACHMENTS",
                    message="attachments item must be string or object",
                )
            storage = item.get("storage")
            rel_path = item.get("rel_path")
            if isinstance(storage, dict):
                has_path = self._is_non_empty_string(storage.get("path"))
                has_url = self._is_non_empty_string(storage.get("url"))
                if has_path or has_url:
                    continue
            if self._is_non_empty_string(rel_path):
                continue
            raise AdapterValidationError(
                code="ICATMSG_ADAPTER_INVALID_ATTACHMENTS",
                message="attachments item must include storage.path/storage.url or rel_path",
            )

    def build_session_key(
        self,
        session_scope: str,
        account_id: str,
        chat_id: str,
        tenant_id: str | None = None,
        bot_id: str | None = None,
    ) -> str:
        tenant_segment = str(tenant_id or "default").strip() or "default"
        bot_segment = str(bot_id or "").strip()
        if session_scope == "account_chat":
            base_key = f"shared:{tenant_segment}:{account_id}:{chat_id}"
        else:
            base_key = f"{self.name}:{tenant_segment}:{account_id}:{chat_id}"
        return f"{base_key}:{bot_segment}" if bot_segment else base_key


class ICatMsgChannel(BaseChannel):
    """
    iCatMsg Channel implementation using Kafka.
    
    Acts as a bridge connecting the ithqbot MessageBus to Kafka topics.
    """
    name = "icatmsg"
    display_name = "iCatMsg"
    contract_version = "v1.2"
    _aiokafka_filter_installed = False

    def __init__(self, config: Any, bus: Any):
        super().__init__(config, bus)
        
        # Load Kafka config only from channel config
        self.bootstrap_servers = self.config.get("kafka_servers", "localhost:9092") if isinstance(self.config, dict) else getattr(self.config, "kafka_servers", "localhost:9092")
        self.inbound_topic = self.config.get("inbound_topic", "icatmsg_inbound") if isinstance(self.config, dict) else getattr(self.config, "inbound_topic", "icatmsg_inbound")
        self.outbound_topic = self.config.get("outbound_topic", "icatmsg_outbound") if isinstance(self.config, dict) else getattr(self.config, "outbound_topic", "icatmsg_outbound")
        self.security_protocol = self.config.get("kafka_security_protocol", "") if isinstance(self.config, dict) else getattr(self.config, "kafka_security_protocol", "")
        self.sasl_mechanism = self.config.get("kafka_sasl_mechanism", "PLAIN") if isinstance(self.config, dict) else getattr(self.config, "kafka_sasl_mechanism", "PLAIN")
        self.sasl_username = self.config.get("kafka_username", "") if isinstance(self.config, dict) else getattr(self.config, "kafka_username", "")
        self.sasl_password = self.config.get("kafka_password", "") if isinstance(self.config, dict) else getattr(self.config, "kafka_password", "")
        
        # Load optional bot_id filtering
        self.bot_id = self.config.get("bot_id", None) if isinstance(self.config, dict) else getattr(self.config, "bot_id", None)
        self.session_scope = self.config.get("session_scope", "channel") if isinstance(self.config, dict) else getattr(self.config, "session_scope", "channel")
        if isinstance(self.config, dict):
            self.shared_workspace_root = (
                self.config.get("shared_workspace_root")
                or self.config.get("sharedWorkspaceRoot")
            )
        else:
            self.shared_workspace_root = (
                getattr(self.config, "shared_workspace_root", None)
                or getattr(self.config, "sharedWorkspaceRoot", None)
            )
        # Stable group ID based on bot_id if set, otherwise default
        group_suffix = f"-{self.bot_id}" if self.bot_id else ""
        self.group_id = self.config.get("group_id", f"ithqbot-icatmsg-group{group_suffix}") if isinstance(self.config, dict) else getattr(self.config, "group_id", f"ithqbot-icatmsg-group{group_suffix}")
        self.contract_warning_cooldown_seconds = self.config.get("contract_warning_cooldown_seconds", 300) if isinstance(self.config, dict) else getattr(self.config, "contract_warning_cooldown_seconds", 300)
        self._contract_warning_last_ts: dict[str, int] = {}
        self._adapters: dict[str, ChannelAdapter] = {}
        self._workspace_cache: set[str] = set()
        self._loop_task: Optional[asyncio.Task] = None
        self._runtime_ingress = RuntimeIngressAdapter(bus) if hasattr(bus, "publish_runtime_ingress") else None
        self.register_channel_adapter(ChannelAdapter("icatmsg", processing_receipt=True))
        self.register_channel_adapter(ChannelAdapter("feishu", processing_receipt=False))
        self.kafka_reconnect_max_attempts = int(
            self.config.get("kafka_reconnect_max_attempts", 6)
            if isinstance(self.config, dict)
            else getattr(self.config, "kafka_reconnect_max_attempts", 6)
        )
        self.kafka_reconnect_base_delay_ms = int(
            self.config.get("kafka_reconnect_base_delay_ms", 500)
            if isinstance(self.config, dict)
            else getattr(self.config, "kafka_reconnect_base_delay_ms", 500)
        )
        self.kafka_reconnect_max_delay_ms = int(
            self.config.get("kafka_reconnect_max_delay_ms", 10000)
            if isinstance(self.config, dict)
            else getattr(self.config, "kafka_reconnect_max_delay_ms", 10000)
        )
        self.kafka_session_timeout_ms = max(
            6000,
            int(
                self.config.get("kafka_session_timeout_ms", 45000)
                if isinstance(self.config, dict)
                else getattr(self.config, "kafka_session_timeout_ms", 45000)
            ),
        )
        self.kafka_heartbeat_interval_ms = min(
            max(
                1000,
                int(
                    self.config.get("kafka_heartbeat_interval_ms", 15000)
                    if isinstance(self.config, dict)
                    else getattr(self.config, "kafka_heartbeat_interval_ms", 15000)
                ),
            ),
            max(1000, self.kafka_session_timeout_ms // 3),
        )
        self.kafka_rebalance_timeout_ms = max(
            self.kafka_session_timeout_ms,
            int(
                self.config.get("kafka_rebalance_timeout_ms", 90000)
                if isinstance(self.config, dict)
                else getattr(self.config, "kafka_rebalance_timeout_ms", 90000)
            ),
        )
        self.kafka_max_poll_interval_ms = max(
            self.kafka_rebalance_timeout_ms,
            int(
                self.config.get("kafka_max_poll_interval_ms", 300000)
                if isinstance(self.config, dict)
                else getattr(self.config, "kafka_max_poll_interval_ms", 300000)
            ),
        )
        self.kafka_auto_commit_interval_ms = max(
            1000,
            int(
                self.config.get("kafka_auto_commit_interval_ms", 5000)
                if isinstance(self.config, dict)
                else getattr(self.config, "kafka_auto_commit_interval_ms", 5000)
            ),
        )
        self.kafka_consume_batch_size = max(
            1,
            int(
                self.config.get("kafka_consume_batch_size", 100)
                if isinstance(self.config, dict)
                else getattr(self.config, "kafka_consume_batch_size", 100)
            ),
        )
        self.kafka_consume_wait_ms = max(
            1,
            int(
                self.config.get("kafka_consume_wait_ms", 100)
                if isinstance(self.config, dict)
                else getattr(self.config, "kafka_consume_wait_ms", 100)
            ),
        )
        self.kafka_connections_max_idle_ms = int(
            self.config.get("kafka_connections_max_idle_ms", 30000)
            if isinstance(self.config, dict)
            else getattr(self.config, "kafka_connections_max_idle_ms", 30000)
        )
        self.kafka_request_timeout_ms = int(
            self.config.get("kafka_request_timeout_ms", max(self.kafka_rebalance_timeout_ms, 40000))
            if isinstance(self.config, dict)
            else getattr(self.config, "kafka_request_timeout_ms", max(self.kafka_rebalance_timeout_ms, 40000))
        )
        self.kafka_retry_backoff_ms = int(
            self.config.get("kafka_retry_backoff_ms", 200)
            if isinstance(self.config, dict)
            else getattr(self.config, "kafka_retry_backoff_ms", 200)
        )

        self.consumer: Optional['AIOKafkaConsumer'] = None
        self.producer: Optional['AIOKafkaProducer'] = None
        self._consumer_task: Optional[asyncio.Task] = None
        self._install_aiokafka_noise_filter()

    @staticmethod
    def _sanitize_workspace_segment(value: Any, fallback: str) -> str:
        normalized = "".join(
            ch if str(ch).isalnum() or ch in {"-", "_", "."} else "-"
            for ch in str(value or "").strip()
        )
        compact = normalized.strip("-._")
        return compact or fallback

    def _build_user_workspace(
        self,
        account_id: str,
        tenant_id: str | None,
        bot_id: str | None,
    ) -> pathlib.Path | None:
        """Build a per-user shared workspace from explicit shared storage config."""
        root = str(self.shared_workspace_root or "").strip()
        if not root:
            raise RuntimeError(
                "icatmsg shared_workspace_root is required in enterprise strict mode; "
                "local session workspaces are disabled"
            )
        root_path = pathlib.Path(root).expanduser()
        if not root_path.is_absolute():
            root_path = (get_workspace_path() / root_path).resolve()
        tenant_segment = self._sanitize_workspace_segment(tenant_id or "default", "default")
        bot_segment = self._sanitize_workspace_segment(bot_id or self.bot_id or "default", "default")
        account_segment = self._sanitize_workspace_segment(account_id, "default")
        workspace = root_path / tenant_segment / bot_segment / account_segment
        
        ws_str = str(workspace)
        if ws_str not in self._workspace_cache:
            workspace.mkdir(parents=True, exist_ok=True)
            self._workspace_cache.add(ws_str)
            # Limit cache size if needed, but per-bot workspace count is usually small
            if len(self._workspace_cache) > 2000:
                self._workspace_cache.clear()
        return workspace

    @classmethod
    def _install_aiokafka_noise_filter(cls) -> None:
        if cls._aiokafka_filter_installed:
            return
        logging.getLogger("aiokafka.consumer.group_coordinator").addFilter(_AIOKafkaNoiseFilter())
        cls._aiokafka_filter_installed = True

    def register_channel_adapter(self, adapter: ChannelAdapter) -> None:
        self._adapters[adapter.name] = adapter

    def _resolve_channel_adapter(self, channel_name: str) -> ChannelAdapter | None:
        return self._adapters.get(channel_name)

    def _warn_with_cooldown(self, dedup_key: str, message: str, *args: Any) -> None:
        now_ms = int(time.time() * 1000)
        cooldown_ms = int(self.contract_warning_cooldown_seconds) * 1000
        last_ts = self._contract_warning_last_ts.get(dedup_key)
        if last_ts is not None and now_ms - last_ts < cooldown_ms:
            return
        self._contract_warning_last_ts[dedup_key] = now_ms
        logger.warning(message, *args)

    def _warn_missing_inbound_contract_fields(
        self,
        *,
        body: dict[str, Any],
        header: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        optional_fields = ("tenant_id", "bot_id", "client_id")
        for field in optional_fields:
            if body.get(field) is None and metadata.get(field) is None:
                self._warn_with_cooldown(
                    f"inbound_optional:{field}",
                    "ICATMSG_INBOUND_MISSING_OPTIONAL_FIELD: {}",
                    field,
                )
        if not isinstance(body.get("channel"), str) or not str(body.get("channel")).strip():
            self._warn_with_cooldown(
                "inbound_optional:channel",
                "ICATMSG_INBOUND_MISSING_OPTIONAL_FIELD: channel",
            )
        if not isinstance(header.get("msg_id"), str) or not str(header.get("msg_id")).strip():
            self._warn_with_cooldown(
                "inbound_trace:header.msg_id",
                "ICATMSG_INBOUND_MISSING_TRACE_ID: header.msg_id",
            )

    def _warn_missing_outbound_contract_fields(
        self,
        *,
        body: dict[str, Any],
        metadata: dict[str, Any],
        request_msg_id: Any,
        event_type: str,
    ) -> None:
        if not isinstance(body.get("tenant_id"), str) or not str(body.get("tenant_id")).strip():
            self._warn_with_cooldown(
                "outbound_optional:tenant_id",
                "ICATMSG_OUTBOUND_MISSING_OPTIONAL_FIELD: tenant_id",
            )
        if not isinstance(body.get("bot_id"), str) or not str(body.get("bot_id")).strip():
            self._warn_with_cooldown(
                "outbound_optional:bot_id",
                "ICATMSG_OUTBOUND_MISSING_OPTIONAL_FIELD: bot_id",
            )
        if not isinstance(body.get("client_id"), str) or not str(body.get("client_id")).strip():
            self._warn_with_cooldown(
                "outbound_optional:client_id",
                "ICATMSG_OUTBOUND_MISSING_OPTIONAL_FIELD: client_id",
            )
        if not isinstance(metadata.get("trace_id"), str) or not str(metadata.get("trace_id")).strip():
            self._warn_with_cooldown(
                "outbound_optional:trace_id",
                "ICATMSG_OUTBOUND_MISSING_OPTIONAL_FIELD: trace_id",
            )
        if event_type == "status.processing" and (
            not isinstance(request_msg_id, str) or not request_msg_id.strip()
        ):
            self._warn_with_cooldown(
                "outbound_trace:request_msg_id",
                "ICATMSG_OUTBOUND_MISSING_TRACE_ID: request_msg_id",
            )

    def _normalize_priority(self, value: Any) -> str:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in MESSAGE_PRIORITY_LEVELS:
                return normalized
            aliases = {"urgent": "critical", "p0": "critical", "p1": "high", "p2": "normal", "p3": "low"}
            if normalized in aliases:
                return aliases[normalized]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            num = int(value)
            if num <= 0:
                return "critical"
            if num == 1:
                return "high"
            if num == 2:
                return "normal"
            return "low"
        return "normal"

    def _kafka_auth_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "connections_max_idle_ms": self.kafka_connections_max_idle_ms,
            "request_timeout_ms": self.kafka_request_timeout_ms,
            "retry_backoff_ms": self.kafka_retry_backoff_ms,
        }
        protocol = self.security_protocol.strip() if isinstance(self.security_protocol, str) else ""
        if not protocol:
            return options
        options["security_protocol"] = protocol
        if protocol.startswith("SASL"):
            if isinstance(self.sasl_username, str) and self.sasl_username.strip():
                options["sasl_plain_username"] = self.sasl_username.strip()
            if isinstance(self.sasl_password, str) and self.sasl_password.strip():
                options["sasl_plain_password"] = self.sasl_password.strip()
            if isinstance(self.sasl_mechanism, str) and self.sasl_mechanism.strip():
                options["sasl_mechanism"] = self.sasl_mechanism.strip()
        return options

    def _kafka_consumer_options(self) -> dict[str, Any]:
        return {
            "auto_offset_reset": "earliest",
            "group_id": self.group_id,
            "session_timeout_ms": self.kafka_session_timeout_ms,
            "heartbeat_interval_ms": self.kafka_heartbeat_interval_ms,
            "rebalance_timeout_ms": self.kafka_rebalance_timeout_ms,
            "max_poll_interval_ms": self.kafka_max_poll_interval_ms,
            "auto_commit_interval_ms": self.kafka_auto_commit_interval_ms,
        }

    def _kafka_connection_context(self) -> dict[str, Any]:
        return {
            "bootstrap_servers": self.bootstrap_servers,
            "inbound_topic": self.inbound_topic,
            "outbound_topic": self.outbound_topic,
            "group_id": self.group_id,
            "security_protocol": self.security_protocol or "PLAINTEXT",
        }

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {
            "enabled": False,
            "kafka_servers": "localhost:9092",
            "kafka_security_protocol": "",
            "kafka_sasl_mechanism": "PLAIN",
            "kafka_username": "",
            "kafka_password": "",
            "inbound_topic": "icatmsg_inbound",
            "outbound_topic": "icatmsg_outbound",
            "contract_warning_cooldown_seconds": 300,
            "kafka_reconnect_max_attempts": 6,
            "kafka_reconnect_base_delay_ms": 500,
            "kafka_reconnect_max_delay_ms": 10000,
            "kafka_session_timeout_ms": 45000,
            "kafka_heartbeat_interval_ms": 15000,
            "kafka_rebalance_timeout_ms": 90000,
            "kafka_max_poll_interval_ms": 300000,
            "kafka_auto_commit_interval_ms": 5000,
            "kafka_consume_batch_size": 100,
            "kafka_consume_wait_ms": 100,
            "shared_workspace_root": "",
            "allow_from": ["*"]
        }

    async def _process_kafka_record(self, msg: Any) -> None:
        try:
            raw_event = json.loads(msg.value.decode("utf-8"))
            body, header, meta, event_type = self._decode_contract(raw_event)

            msg_id = str(header.get("msg_id") or "")
            trace_id = str(header.get("trace_id") or meta.get("trace_id") or msg_id)

            with logger.contextualize(trace_id=trace_id, msg_id=msg_id):
                await self._handle_contract_event(body, header, meta, event_type)

        except json.JSONDecodeError:
            logger.warning(f"Failed to decode Kafka message: {msg.value}")
        except AdapterValidationError as e:
            logger.warning(f"[{e.code}] {e.message}")
        except Exception as e:
            logger.error(f"Error processing iCatMsg Kafka message: {e}")

    async def _process_kafka_batch(self, records: list[Any]) -> None:
        for msg in records:
            if not self._running:
                break
            await self._process_kafka_record(msg)

    async def start(self) -> None:
        """Start the Kafka consumer and producer."""
        if not HAS_AIOKAFKA:
            logger.error("aiokafka is not installed. Please install it to use the ICatMsg channel.")
            return

        self._running = True
        logger.info(
            "iCatMsg channel starting with Kafka config: {}",
            self._kafka_connection_context(),
        )
        ok = await self._connect_kafka_clients()
        if not ok:
            logger.error("Failed to start iCatMsg Kafka channel after retries")
            self._running = False
            return
        self._consumer_task = asyncio.create_task(self._kafka_receiver_loop())

    async def _connect_kafka_clients(self) -> bool:
        kafka_auth_options = self._kafka_auth_options()
        for attempt in range(1, max(1, self.kafka_reconnect_max_attempts) + 1):
            try:
                if self.producer is None:
                    self.producer = AIOKafkaProducer(
                        bootstrap_servers=self.bootstrap_servers,
                        **kafka_auth_options,
                    )
                    await self.producer.start()
                    logger.info("iCatMsg Kafka Producer started.")
                if self.consumer is None:
                    self.consumer = AIOKafkaConsumer(
                        self.inbound_topic,
                        bootstrap_servers=self.bootstrap_servers,
                        **self._kafka_consumer_options(),
                        **kafka_auth_options,
                    )
                    await self.consumer.start()
                    logger.info(f"iCatMsg Kafka Consumer started. Listening on topic: {self.inbound_topic}")
                return True
            except Exception as e:
                hint = ""
                if e.__class__.__name__ == "GroupCoordinatorNotAvailableError":
                    hint = (
                        " Kafka group coordinator is not ready; verify broker readiness, "
                        "__consumer_offsets health, and bootstrap/advertised listener settings."
                    )
                logger.warning(
                    "iCatMsg Kafka connect failed on attempt {}/{} for {}: {} ({}){}",
                    attempt,
                    self.kafka_reconnect_max_attempts,
                    self._kafka_connection_context(),
                    e,
                    e.__class__.__name__,
                    hint,
                )
                await self._close_kafka_clients()
                if attempt >= self.kafka_reconnect_max_attempts:
                    break
                delay_ms = min(
                    self.kafka_reconnect_base_delay_ms * (2 ** (attempt - 1)),
                    self.kafka_reconnect_max_delay_ms,
                )
                await asyncio.sleep(max(0, delay_ms) / 1000)
        return False

    async def _close_kafka_clients(self) -> None:
        if self.consumer:
            try:
                await self.consumer.stop()
            except Exception:
                pass
        if self.producer:
            try:
                await self.producer.stop()
            except Exception:
                 pass
            self.producer = None

    async def _handle_contract_event(
        self, body: dict[str, Any], header: dict[str, Any], meta: dict[str, Any], event_type: str
    ) -> None:
        """Process a single decoded contract event."""
        account_id = body.get("account_id")
        tenant_id = body.get("tenant_id")
        chat_id = body.get("chat_id", "default_chat")
        content = body.get("content", "")
        client_id = body.get("client_id") or meta.get("client_id")
        priority = self._normalize_priority(body.get("priority") if body.get("priority") is not None else meta.get("priority"))

        msg_channel = body.get("channel", "icatmsg")
        adapter = self._resolve_channel_adapter(msg_channel)
        if not adapter:
            return
        adapter.validate(body, event_type)
        self._warn_missing_inbound_contract_fields(body=body, header=header, metadata=meta)
        if not account_id:
            return

        msg_bot_id = body.get("bot_id")
        if self.bot_id and msg_bot_id != self.bot_id:
            return

        logger.debug(f"iCatMsg received from {account_id} (tenant: {tenant_id}) for bot {msg_bot_id}: {content[:50]}")

        session_key = adapter.build_session_key(
            self.session_scope,
            account_id,
            chat_id,
            str(tenant_id) if tenant_id else None,
            str(msg_bot_id) if msg_bot_id else None,
        )

        metadata = dict(meta)
        metadata["account_id"] = account_id
        metadata["tenant_id"] = tenant_id
        metadata["source_channel"] = adapter.name
        metadata["channel"] = adapter.name
        metadata["chat_id"] = chat_id
        metadata["bot_id"] = msg_bot_id
        metadata["client_id"] = client_id
        metadata["contract_version"] = header.get("version", "legacy")
        metadata["event_type"] = event_type
        metadata["priority"] = priority
        trace_id = header.get("trace_id") or metadata.get("trace_id")
        if isinstance(trace_id, str) and trace_id:
            metadata["trace_id"] = trace_id
        if "msg_id" in header:
            metadata["message_id"] = header["msg_id"]
            metadata["parent_msg_id"] = header["msg_id"]
            metadata.setdefault("request_msg_id", header["msg_id"])
        if "content_type" in body:
            metadata["content_type"] = body["content_type"]
        if isinstance(body.get("interaction"), dict):
            metadata["interaction_response"] = dict(body["interaction"])
        uploaded_at = self._coerce_header_uploaded_at(header.get("timestamp")) or datetime.now(timezone.utc).isoformat()
        if "file_meta" in body:
            file_meta = body["file_meta"]
            if isinstance(file_meta, dict):
                file_meta = dict(file_meta)
                if uploaded_at:
                    file_meta.setdefault("uploaded_at", uploaded_at)
                file_meta.setdefault(
                    "original_file_name",
                    str(file_meta.get("name") or file_meta.get("file_name") or "").strip() or None,
                )
            metadata["file_meta"] = file_meta
        attachments = self._normalize_attachments(body.get("attachments", []))
        if uploaded_at:
            for item in attachments:
                if isinstance(item, dict):
                    item.setdefault("uploaded_at", uploaded_at)
                    item.setdefault(
                        "original_file_name",
                        str(item.get("name") or item.get("file_name") or "").strip() or None,
                    )
        if attachments:
            metadata["attachments"] = attachments

        user_workspace = self._build_user_workspace(
            account_id=str(account_id),
            tenant_id=str(tenant_id) if tenant_id else None,
            bot_id=str(msg_bot_id) if msg_bot_id else None,
        )

        media = body.get("media", [])
        if attachments:
            media.extend([path for path in [self._extract_attachment_path(a) for a in attachments] if path])

        inbound = InboundMessage(
            channel="icatmsg",
            sender_id=str(account_id),
            chat_id=str(chat_id),
            content=str(content),
            media=media,
            metadata=metadata,
            account_id=str(account_id),
            tenant_id=str(tenant_id) if tenant_id else None,
            bot_id=str(msg_bot_id) if msg_bot_id else None,
            session_key_override=session_key,
            workspace_override=str(user_workspace),
        )
        record_trace_event(
            event_name="kafka.inbound.consumed",
            phase="point",
            status="ok",
            component="ithqbot_channel",
            source="icatmsg_server",
            account_id=str(account_id),
            tenant_id=str(tenant_id) if tenant_id else None,
            bot_id=str(msg_bot_id) if msg_bot_id else None,
            channel=str(adapter.name),
            chat_id=str(chat_id),
            client_id=str(client_id) if client_id else None,
            request_msg_id=str(metadata.get("request_msg_id") or header.get("msg_id") or ""),
            trace_id=str(metadata.get("trace_id") or header.get("trace_id") or metadata.get("request_msg_id") or header.get("msg_id") or ""),
            content_preview=str(content or ""),
            event_type=event_type,
            details={
                "topic": self.inbound_topic,
                "priority": priority,
                "attachments_count": len(attachments),
            },
        )
        if adapter.processing_receipt and "msg_id" in header:
            await self._send_processing_receipt(
                channel_name=adapter.name,
                account_id=str(account_id),
                chat_id=str(chat_id),
                tenant_id=str(tenant_id) if tenant_id else None,
                bot_id=str(msg_bot_id) if msg_bot_id else None,
                client_id=str(client_id) if client_id else None,
                request_msg_id=str(header["msg_id"]),
                trace_id=metadata.get("trace_id"),
            )
        if self._runtime_ingress is not None:
            await self._runtime_ingress.publish_message(inbound, command_type="message")
        else:
            await self.bus.publish_inbound(inbound)

    async def _kafka_receiver_loop(self) -> None:
        """Continuously receive messages from Kafka and forward to ithqbot."""
        reconnect_round = 0
        while self._running:
            if self.consumer is None or self.producer is None:
                ok = await self._connect_kafka_clients()
                if not ok:
                    await asyncio.sleep(1.0)
                    continue
            try:
                while self._running:
                    if hasattr(self.consumer, "getmany"):
                        batches = await self.consumer.getmany(
                            timeout_ms=self.kafka_consume_wait_ms,
                            max_records=self.kafka_consume_batch_size,
                        )
                        if not batches:
                            continue
                        for records in batches.values():
                            if not records:
                                continue
                            await self._process_kafka_batch(list(records))
                    else:
                        async for msg in self.consumer:
                            if not self._running:
                                break
                            await self._process_kafka_record(msg)
                        break
                reconnect_round = 0
                if self._running:
                    await self._close_kafka_clients()
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if _is_rebalance_related_exception(e):
                    reconnect_round = 0
                    logger.warning("iCatMsg Kafka consumer is rebalancing, reconnecting: {}", e)
                    await self._close_kafka_clients()
                    await asyncio.sleep(0.2)
                    continue
                reconnect_round += 1
                logger.error("iCatMsg Kafka consumer loop error: {}", e)
                await self._close_kafka_clients()
                delay_ms = min(
                    self.kafka_reconnect_base_delay_ms * (2 ** max(0, reconnect_round - 1)),
                    self.kafka_reconnect_max_delay_ms,
                )
                await asyncio.sleep(max(0, delay_ms) / 1000)

    async def _send_processing_receipt(
        self,
        channel_name: str,
        account_id: str,
        chat_id: str,
        tenant_id: str | None,
        bot_id: str | None,
        client_id: str | None,
        request_msg_id: str,
        trace_id: str | None = None,
    ) -> None:
        if not self.producer and not await self._connect_kafka_clients():
            return
        processing_event = self._build_envelope(
            event="status.processing",
            source="ithqbot_channel",
            body={
                "channel": channel_name,
                "account_id": account_id,
                "chat_id": chat_id,
                "tenant_id": tenant_id,
                "bot_id": bot_id,
                "client_id": client_id,
                "priority": "high",
                "status": {
                    "phase": "processing",
                    "request_msg_id": request_msg_id,
                },
            },
            metadata={
                "request_msg_id": request_msg_id,
                "tenant_id": tenant_id,
                "client_id": client_id,
                "trace_id": trace_id,
                "priority": "high",
            },
            parent_msg_id=request_msg_id,
            msg_id=f"s-{uuid.uuid4()}",
        )
        payload = json.dumps(processing_event, ensure_ascii=False).encode("utf-8")
        for attempt in range(1, 3):
            try:
                await self.producer.send_and_wait(
                    self.outbound_topic,
                    value=payload,
                    key=account_id.encode("utf-8"),
                )
                return
            except Exception as e:
                logger.warning("Failed to send processing receipt on attempt {}/2: {}", attempt, e)
                await self._close_kafka_clients()
                if attempt == 1:
                    await self._connect_kafka_clients()

    def _decode_contract(self, raw_event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
        # --- 1. Smart Envelope Detection (Legacy Support) ---
        # If "payload" is missing but "account_id" is at the root, it's a legacy flat envelope (v1.1 or older)
        if "payload" not in raw_event and "account_id" in raw_event:
            raw_event = {
                "header": {
                    "version": "v1.2",  # Auto-upgrade to v1.2 for internal processing
                    "msg_id": f"legacy-{uuid.uuid4().hex[:8]}",
                    "event": raw_event.get("event", "message.user")
                },
                "payload": raw_event,
                "metadata": raw_event.get("metadata", {})
            }

        if "payload" not in raw_event or not isinstance(raw_event.get("payload"), dict):
            raise AdapterValidationError(
                code="ICATMSG_ADAPTER_INVALID_ENVELOPE",
                message="v1.2 envelope is required: payload must be an object",
            )
            
        body = raw_event["payload"]
        header = raw_event.get("header", {})
        if not isinstance(header, dict):
            header = {} # Graceful fallback
            
        # --- 2. Graceful Version Handling ---
        version = header.get("version")
        if not (isinstance(version, str) and version.strip()):
            # If version is missing, assume v1.2 but potentially warn (implementing cooldown-based warning is optional but safe)
            version = self.contract_version
            
        if version != self.contract_version:
             # If it's a known older version, we can still process it but it's good to note
             pass

        # --- 3. Mandatory Field Recovery (Defaults) ---
        # msg_id / trace_id
        msg_id = header.get("msg_id") or header.get("trace_id")
        if not (isinstance(msg_id, str) and msg_id.strip()):
            if version == "v1.2":
                 raise AdapterValidationError(code="ICATMSG_ADAPTER_MISSING_MSG_ID", message="v1.2 requires msg_id or trace_id")
            msg_id = f"gen-{uuid.uuid4().hex}"
            header["msg_id"] = msg_id

        # tenant_id
        tenant_id = body.get("tenant_id")
        if not (isinstance(tenant_id, str) and tenant_id.strip()):
            if version == "v1.2":
                 raise AdapterValidationError(code="ICATMSG_ADAPTER_MISSING_TENANT_ID", message="v1.2 requires tenant_id")
            tenant_id = "winlmp" 
            body["tenant_id"] = tenant_id

        # client_id
        client_id = body.get("client_id")
        if not (isinstance(client_id, str) and client_id.strip()):
            if version == "v1.2":
                 raise AdapterValidationError(code="ICATMSG_ADAPTER_MISSING_CLIENT_ID", message="v1.2 requires client_id")
            body["client_id"] = "unknown"

        meta = raw_event.get("metadata", {})
        event_type = header.get("event") or body.get("event") or "message.user"
        return body, header, meta, event_type

    def _normalize_attachments(self, attachments: Any) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        if not isinstance(attachments, list):
            return normalized
        for item in attachments:
            if isinstance(item, str):
                storage = self._normalize_storage_payload({"path": item})
                if storage:
                    payload = {"kind": "file", "storage": storage}
                    payload.update(self._build_storage_compatibility_fields(storage))
                    normalized.append(payload)
                    continue
                normalized.append({
                    "kind": "file",
                    "storage": {"path": item},
                })
                continue
            if not isinstance(item, dict):
                continue
            if "storage" in item and isinstance(item["storage"], dict):
                storage = self._normalize_storage_payload(item["storage"])
                payload = {**item, "storage": storage}
                if not str(payload.get("original_file_name") or "").strip():
                    payload["original_file_name"] = str(payload.get("name") or payload.get("file_name") or "").strip() or None
                payload.update(self._build_storage_compatibility_fields(storage, source=item))
                normalized.append(payload)
                continue
            storage_uri = (
                str(item.get("storage_uri") or "").strip()
                or str(item.get("s3_uri") or "").strip()
                or str(item.get("minio_uri") or "").strip()
            )
            if storage_uri:
                storage = self._normalize_storage_payload({"storage_uri": storage_uri})
                if storage:
                    payload = {**item, "storage": storage}
                    if not str(payload.get("original_file_name") or "").strip():
                        payload["original_file_name"] = str(payload.get("name") or payload.get("file_name") or "").strip() or None
                    payload.update(self._build_storage_compatibility_fields(storage, source=item))
                    normalized.append(payload)
                    continue
            rel_path = str(item.get("rel_path") or item.get("path") or "").strip()
            if rel_path:
                storage = self._normalize_storage_payload(
                    {
                        "backend": item.get("storage_backend") or item.get("backend"),
                        "bucket": item.get("storage_bucket") or item.get("bucket"),
                        "path": rel_path,
                    }
                )
                payload = {
                    "kind": item.get("kind", "file"),
                    "name": item.get("original_file_name") or item.get("name") or item.get("file_name"),
                    "original_file_name": item.get("original_file_name") or item.get("name") or item.get("file_name"),
                    "mime": item.get("mime"),
                    "size": item.get("size"),
                    "storage": storage or {"path": rel_path},
                    "checksum": item.get("checksum"),
                    "uploaded_at": item.get("uploaded_at") or item.get("upload_time") or item.get("timestamp"),
                }
                payload.update(self._build_storage_compatibility_fields(storage, source=item))
                normalized.append(payload)
                continue
            normalized.append(item)
        return normalized

    @staticmethod
    def _normalize_storage_payload(storage: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(storage)
        storage_uri = str(normalized.get("storage_uri") or normalized.get("uri") or "").strip()
        path_value = str(normalized.get("path") or "").strip()

        if path_value:
            parsed_backend, parsed_bucket, parsed_path = parse_storage_uri(path_value)
            if parsed_backend and parsed_path:
                storage_uri = storage_uri or path_value
                normalized["path"] = parsed_path
                if parsed_bucket:
                    normalized.setdefault("bucket", parsed_bucket)
                normalized["backend"] = normalize_storage_backend(
                    normalized.get("backend") or parsed_backend
                )

        if storage_uri:
            parsed_backend, parsed_bucket, parsed_path = parse_storage_uri(storage_uri)
            if parsed_backend and parsed_path:
                normalized["path"] = parsed_path
                if parsed_bucket:
                    normalized.setdefault("bucket", parsed_bucket)
                normalized["backend"] = normalize_storage_backend(
                    normalized.get("backend") or parsed_backend
                )

        bucket = str(normalized.get("bucket") or "").strip()
        path_text = str(normalized.get("path") or "").strip().strip("/")
        if path_text:
            normalized["path"] = path_text
        if bucket and path_text:
            backend = normalize_storage_backend(normalized.get("backend") or "minio")
            normalized["backend"] = backend
            normalized["storage_uri"] = f"{backend}://{bucket}/{path_text}"
        elif storage_uri:
            normalized["storage_uri"] = storage_uri
        return normalized

    @staticmethod
    def _build_storage_compatibility_fields(
        storage: dict[str, Any] | None,
        *,
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(storage, dict):
            return {}
        payload: dict[str, Any] = {}
        storage_uri = str(storage.get("storage_uri") or "").strip()
        backend = str(storage.get("backend") or "").strip()
        if storage_uri:
            payload["storage_uri"] = storage_uri
            if backend == "minio":
                payload["minio_uri"] = str(
                    (source or {}).get("minio_uri") or storage_uri
                ).strip() or storage_uri
            elif backend == "s3":
                payload["s3_uri"] = str(
                    (source or {}).get("s3_uri") or storage_uri
                ).strip() or storage_uri
        return payload

    @staticmethod
    def _coerce_header_uploaded_at(timestamp_value: Any) -> str | None:
        if isinstance(timestamp_value, (int, float)):
            ts = float(timestamp_value)
            if ts > 1e12:
                ts /= 1000.0
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            except Exception:
                return None
        if isinstance(timestamp_value, str) and timestamp_value.strip():
            s = timestamp_value.strip()
            if s.isdigit():
                ts = float(s)
                if ts > 1e12:
                    ts /= 1000.0
                try:
                    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                except Exception:
                    return None
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.isoformat()
            except Exception:
                return None
        return None

    def _extract_attachment_path(self, attachment: dict[str, Any]) -> str | None:
        storage = attachment.get("storage")
        if isinstance(storage, dict):
            path = storage.get("path")
            if isinstance(path, str) and path:
                return path
        rel_path = attachment.get("rel_path")
        if isinstance(rel_path, str) and rel_path:
            return rel_path
        return None

    def _build_envelope(
        self,
        event: str,
        source: str,
        body: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        parent_msg_id: str | None = None,
        msg_id: str | None = None,
    ) -> dict[str, Any]:
        header: dict[str, Any] = {
            "version": self.contract_version,
            "msg_id": msg_id or f"m-{uuid.uuid4()}",
            "timestamp": int(time.time() * 1000),
            "source": source,
            "event": event,
        }
        if parent_msg_id:
            header["parent_msg_id"] = parent_msg_id
        return {
            "header": header,
            "payload": body,
            "metadata": metadata or {},
        }

    async def stop(self) -> None:
        """Stop Kafka connections."""
        self._running = False
        logger.info("iCatMsg channel stopping...")
        
        if self._consumer_task and not self._consumer_task.done():
            self._consumer_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._consumer_task
        await self._close_kafka_clients()
            
        logger.info("iCatMsg channel stopped.")

    async def send(self, msg: OutboundMessage) -> None:
        """Send response back to Kafka."""
        if not self.producer and not await self._connect_kafka_clients():
            logger.warning("iCatMsg channel cannot send message: producer not initialized.")
            return

        # Try to retrieve target_account from msg or fallback to metadata
        account_id = msg.account_id or (msg.metadata.get("account_id") if msg.metadata else None)
        tenant_id = msg.tenant_id or (msg.metadata.get("tenant_id") if msg.metadata else None)
        bot_id = msg.bot_id or (msg.metadata.get("bot_id") if msg.metadata else None)
        client_id = (msg.metadata.get("client_id") if msg.metadata else None)
        source_channel = "icatmsg"
        if msg.metadata:
            source_channel = msg.metadata.get("source_channel") or msg.channel or msg.metadata.get("channel") or source_channel
        raw_target_chat_id = msg.chat_id or ((msg.metadata or {}).get("chat_id") if msg.metadata else None)
        target_chat_id = str(raw_target_chat_id).strip() if isinstance(raw_target_chat_id, str) else ""
        if (
            source_channel != "feishu"
            and client_id
            and bot_id
            and target_chat_id in {"", "default_chat"}
        ):
            target_chat_id = str(bot_id).strip()
        if not target_chat_id:
            target_chat_id = "default_chat"
        
        attachments = self._normalize_attachments((msg.metadata or {}).get("attachments", []))
        if not attachments and msg.media:
            attachments = self._normalize_attachments(list(msg.media))
        files = (msg.metadata or {}).get("files") if msg.metadata else None
        normalized_files = self._normalize_attachments(files) if isinstance(files, list) else []
        if normalized_files:
            attachments.extend(normalized_files)
        file_meta = (msg.metadata or {}).get("file_meta") if msg.metadata else None
        interaction = (msg.metadata or {}).get("interaction") if msg.metadata else None
        is_progress = bool((msg.metadata or {}).get("_progress"))
        progress_percent = (msg.metadata or {}).get("_progress_percent")
        progress_kind = (msg.metadata or {}).get("_progress_kind")
        progress_stage = (msg.metadata or {}).get("_progress_stage")
        status_details = (msg.metadata or {}).get("_status_details")
        if progress_stage not in PROGRESS_STAGES:
            progress_stage = None
        request_msg_id = (
            (msg.metadata or {}).get("request_msg_id")
            or (msg.metadata or {}).get("parent_msg_id")
            or msg.reply_to
        )
        priority = self._normalize_priority((msg.metadata or {}).get("priority"))
        event_type = "message.reply"
        if is_progress or (msg.metadata or {}).get("status_event") == "processing":
            event_type = "status.processing"
        if isinstance(interaction, dict):
            event_type = "message.interaction"
        if event_type not in {"status.processing", "message.interaction"} and (
            (msg.metadata or {}).get("content_type") == "file" or file_meta or attachments
        ):
            event_type = "message.file"
        body = {
            "channel": source_channel,
            "account_id": account_id,
            "tenant_id": tenant_id,
            "bot_id": bot_id,
            "client_id": client_id,
            "chat_id": target_chat_id,
            "content": msg.content,
            "content_type": (msg.metadata or {}).get("content_type", "text"),
            "attachments": attachments,
            "files": files if isinstance(files, list) else None,
            "file_meta": file_meta,
            "reply_to": msg.reply_to or (msg.metadata or {}).get("parent_msg_id"),
            "interaction": interaction if isinstance(interaction, dict) else None,
            "priority": priority,
        }
        outbound_metadata = dict(msg.metadata or {})
        if account_id:
            outbound_metadata.setdefault("account_id", account_id)
        if tenant_id:
            outbound_metadata.setdefault("tenant_id", tenant_id)
        if bot_id:
            outbound_metadata.setdefault("bot_id", bot_id)
        if client_id:
            outbound_metadata.setdefault("client_id", client_id)
        outbound_metadata.setdefault("channel", source_channel)
        outbound_metadata["priority"] = priority
        if request_msg_id:
            outbound_metadata.setdefault("request_msg_id", request_msg_id)
        if (
            not outbound_metadata.get("trace_id")
            and isinstance(request_msg_id, str)
            and request_msg_id.strip()
        ):
            outbound_metadata["trace_id"] = request_msg_id
        if event_type == "status.processing":
            hide_progress_percent = (
                isinstance(status_details, dict)
                and status_details.get("show_progress_percent") is False
            )
            body["status"] = {
                "phase": "processing",
                "request_msg_id": request_msg_id,
                "message": msg.content,
                "progress_percent": None if hide_progress_percent else progress_percent,
                "kind": progress_kind,
                "stage": progress_stage,
                "tool_name": (msg.metadata or {}).get("_tool_name"),
                "skill_name": (msg.metadata or {}).get("_skill_name"),
                "call_type": (msg.metadata or {}).get("_call_type"),
                "details": status_details if isinstance(status_details, dict) else None,
            }
        self._warn_missing_outbound_contract_fields(
            body=body,
            metadata=outbound_metadata,
            request_msg_id=request_msg_id,
            event_type=event_type,
        )
        try:
            self._validate_outbound_contract(body)
        except AdapterValidationError as e:
            logger.warning(f"[{e.code}] {e.message}")
            return
        kafka_payload = self._build_envelope(
            event=event_type,
            source="ithqbot_channel",
            body=body,
            metadata=outbound_metadata,
            parent_msg_id=(msg.metadata or {}).get("parent_msg_id") or msg.reply_to,
        )
        
        payload_bytes = json.dumps(kafka_payload, ensure_ascii=False).encode("utf-8")
        partition_key = account_id.encode("utf-8") if account_id else None
        trace_request_id = str(request_msg_id or "")
        trace_id = str(
            outbound_metadata.get("trace_id")
            or kafka_payload.get("header", {}).get("trace_id")
            or trace_request_id
        )
        
        for attempt in range(1, 3):
            try:
                await self.producer.send_and_wait(
                    self.outbound_topic,
                    value=payload_bytes,
                    key=partition_key
                )
                record_trace_event(
                    event_name="kafka.outbound.produced",
                    phase="point",
                    status="ok",
                    component="ithqbot_channel",
                    source="ithqbot_channel",
                    account_id=str(account_id) if account_id else None,
                    tenant_id=str(tenant_id) if tenant_id else None,
                    bot_id=str(bot_id) if bot_id else None,
                    channel=str(source_channel),
                    chat_id=str(target_chat_id),
                    client_id=str(client_id) if client_id else None,
                    request_msg_id=trace_request_id or None,
                    trace_id=trace_id or None,
                    content_preview=str(msg.content or ""),
                    event_type=event_type,
                    details={
                        "topic": self.outbound_topic,
                        "attempt": attempt,
                        "priority": priority,
                    },
                )
                logger.debug(f"iCatMsg sent response to Kafka for account {account_id}")
                return
            except Exception as e:
                record_trace_event(
                    event_name="kafka.outbound.produce_failed",
                    phase="point",
                    status="error",
                    component="ithqbot_channel",
                    source="ithqbot_channel",
                    account_id=str(account_id) if account_id else None,
                    tenant_id=str(tenant_id) if tenant_id else None,
                    bot_id=str(bot_id) if bot_id else None,
                    channel=str(source_channel),
                    chat_id=str(target_chat_id),
                    client_id=str(client_id) if client_id else None,
                    request_msg_id=trace_request_id or None,
                    trace_id=trace_id or None,
                    content_preview=str(msg.content or ""),
                    event_type=event_type,
                    details={
                        "topic": self.outbound_topic,
                        "attempt": attempt,
                        "error": str(e),
                    },
                )
                logger.warning("Failed to send message to Kafka on attempt {}/2: {}", attempt, e)
                await self._close_kafka_clients()
                if attempt == 1:
                    await self._connect_kafka_clients()

    def _validate_outbound_contract(self, body: dict[str, Any]) -> None:
        account_id = body.get("account_id")
        if not isinstance(account_id, str) or not account_id.strip():
            raise AdapterValidationError(
                code="ICATMSG_OUTBOUND_MISSING_ACCOUNT_ID",
                message="outbound account_id is required",
            )
        chat_id = body.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id.strip():
            raise AdapterValidationError(
                code="ICATMSG_OUTBOUND_MISSING_CHAT_ID",
                message="outbound chat_id is required",
            )
        channel = body.get("channel")
        if not isinstance(channel, str) or not channel.strip():
            raise AdapterValidationError(
                code="ICATMSG_OUTBOUND_MISSING_CHANNEL",
                message="outbound channel is required",
            )
        tenant_id = body.get("tenant_id")
        if tenant_id is not None and not isinstance(tenant_id, str):
            raise AdapterValidationError(
                code="ICATMSG_OUTBOUND_INVALID_TENANT_ID",
                message="outbound tenant_id must be a string",
            )
        client_id = body.get("client_id")
        if client_id is not None and not isinstance(client_id, str):
            raise AdapterValidationError(
                code="ICATMSG_OUTBOUND_INVALID_CLIENT_ID",
                message="outbound client_id must be a string",
            )
        adapter = self._resolve_channel_adapter(str(body.get("channel") or "icatmsg"))
        if adapter:
            adapter.validate(
                {
                    "account_id": account_id,
                    "chat_id": chat_id,
                    "bot_id": body.get("bot_id"),
                    "attachments": body.get("attachments", []),
                },
                "message.user",
            )
