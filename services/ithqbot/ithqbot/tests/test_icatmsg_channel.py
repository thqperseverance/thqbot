import asyncio
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from ithqbot.bus.events import OutboundMessage
from ithqbot.bus.queue import MessageBus
from ithqbot.channels.icatmsg import (
    ChannelAdapter,
    ICatMsgChannel,
    _AIOKafkaNoiseFilter,
    _is_rebalance_related_exception,
)

_ROOT = Path(__file__).resolve().parents[2]
_FEISHU_BOT_PATH = _ROOT / "icatmsg" / "server" / "feishu_bot.py"
_APP_PATH = _ROOT / "icatmsg" / "server" / "app.py"
if not _FEISHU_BOT_PATH.exists() or not _APP_PATH.exists():
    pytest.skip("icatmsg legacy server files are missing in current repository layout", allow_module_level=True)

_feishu_spec = importlib.util.spec_from_file_location("icatmsg_server_feishu_bot", _FEISHU_BOT_PATH)
_feishu_mod = importlib.util.module_from_spec(_feishu_spec)
assert _feishu_spec and _feishu_spec.loader
_feishu_spec.loader.exec_module(_feishu_mod)
FeishuBot = _feishu_mod.FeishuBot

_app_spec = importlib.util.spec_from_file_location("icatmsg_server_app", _APP_PATH)
icatmsg_server_app = importlib.util.module_from_spec(_app_spec)
assert _app_spec and _app_spec.loader
_app_spec.loader.exec_module(icatmsg_server_app)


@pytest.fixture
def mock_bus():
    bus = MagicMock(spec=MessageBus)
    bus.publish_inbound = AsyncMock()
    return bus

class MockConfig:
    def __init__(self, d):
        for k, v in d.items():
            setattr(self, k, v)
    def getattr(self, key, default):
        return getattr(self, key, default)


class _FakeKafkaMessage:
    def __init__(self, payload: dict):
        self.value = json.dumps(payload).encode("utf-8")


class _FakeBatchConsumer:
    def __init__(self, batches: list[dict[object, list[object]]], channel: ICatMsgChannel):
        self._batches = list(batches)
        self._channel = channel

    async def getmany(self, timeout_ms: int, max_records: int):
        _ = timeout_ms, max_records
        if self._batches:
            batch = self._batches.pop(0)
            self._channel._running = False
            return batch
        return {}

@pytest.fixture
def channel_config():
    return MockConfig({
        "enabled": True,
        "kafka_servers": "test:9092",
        "inbound_topic": "test_inbound",
        "outbound_topic": "test_outbound",
        "allow_from": ["*"]
    })


def test_contract_warning_is_rate_limited(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)
    with patch("ithqbot.channels.icatmsg.logger.warning") as mock_warning, \
         patch("ithqbot.channels.icatmsg.time.time", side_effect=[1000.0, 1000.1, 1301.0]):
        channel._warn_with_cooldown("outbound_optional:tenant_id", "ICATMSG_OUTBOUND_MISSING_OPTIONAL_FIELD: tenant_id")
        channel._warn_with_cooldown("outbound_optional:tenant_id", "ICATMSG_OUTBOUND_MISSING_OPTIONAL_FIELD: tenant_id")
        channel._warn_with_cooldown("outbound_optional:tenant_id", "ICATMSG_OUTBOUND_MISSING_OPTIONAL_FIELD: tenant_id")
    assert mock_warning.call_count == 2


def test_channel_adapter_build_session_key_isolated_by_bot():
    adapter = ChannelAdapter("icatmsg", processing_receipt=True)

    assert adapter.build_session_key("channel", "user123", "chat456", "tenant-a", "bot-a") == "icatmsg:tenant-a:user123:chat456:bot-a"
    assert adapter.build_session_key("account_chat", "user123", "chat456", "tenant-b", "bot-b") == "shared:tenant-b:user123:chat456:bot-b"
    assert adapter.build_session_key("channel", "user123", "chat456", None, None) == "icatmsg:default:user123:chat456"


@pytest.mark.asyncio
async def test_icatmsg_start_and_stop(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)
    
    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, \
         patch("ithqbot.channels.icatmsg.AIOKafkaConsumer") as mock_consumer_cls:
        
        mock_producer = mock_producer_cls.return_value
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()
        
        mock_consumer = mock_consumer_cls.return_value
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()
        
        await channel.start()
        
        assert channel.is_running
        mock_producer_cls.assert_called_once_with(
            bootstrap_servers="test:9092",
            connections_max_idle_ms=30000,
            request_timeout_ms=90000,
            retry_backoff_ms=200,
        )
        mock_consumer_cls.assert_called_once_with(
            "test_inbound",
            bootstrap_servers="test:9092",
            auto_offset_reset="earliest",
            group_id="ithqbot-icatmsg-group",
            session_timeout_ms=45000,
            heartbeat_interval_ms=15000,
            rebalance_timeout_ms=90000,
            max_poll_interval_ms=300000,
            auto_commit_interval_ms=5000,
            connections_max_idle_ms=30000,
            request_timeout_ms=90000,
            retry_backoff_ms=200,
        )
        mock_producer.start.assert_called_once()
        mock_consumer.start.assert_called_once()
        
        await channel.stop()
        
        assert not channel.is_running
        mock_producer.stop.assert_called_once()
        mock_consumer.stop.assert_called_once()


@pytest.mark.asyncio
async def test_icatmsg_start_logs_effective_kafka_config(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)

    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, \
         patch("ithqbot.channels.icatmsg.AIOKafkaConsumer") as mock_consumer_cls, \
         patch("ithqbot.channels.icatmsg.logger.info") as mock_info:

        mock_producer = mock_producer_cls.return_value
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()

        mock_consumer = mock_consumer_cls.return_value
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()

        await channel.start()
        await channel.stop()

    assert mock_info.call_args_list[0].args == (
        "iCatMsg channel starting with Kafka config: {}",
        {
            "bootstrap_servers": "test:9092",
            "inbound_topic": "test_inbound",
            "outbound_topic": "test_outbound",
            "group_id": "ithqbot-icatmsg-group",
            "security_protocol": "PLAINTEXT",
        },
    )


@pytest.mark.asyncio
async def test_feishu_bot_stop_without_ws_thread_is_safe() -> None:
    bot = FeishuBot(
        {
            "app_id": "test_app_id",
            "app_secret": "test_app_secret",
        },
        bot_id="bot_test",
    )
    bot._ws_thread = None

    await bot.stop()

    assert bot._ws_thread is None


@pytest.mark.asyncio
async def test_trace_task_list_prefers_explicit_account_id_filter() -> None:
    with patch.object(icatmsg_server_app, "get_trace_tasks", return_value=[]) as mock_get_trace_tasks:
        await icatmsg_server_app.list_trace_tasks(
            account_id="target_account",
            chat_id="bot_B",
            client_id="web_x",
            bot_id="bot_B",
            status=None,
            keyword=None,
            limit=20,
            user_id="admin_user",
        )

    mock_get_trace_tasks.assert_called_once_with(
        account_id="target_account",
        chat_id="bot_B",
        client_id="web_x",
        bot_id="bot_B",
        channel="icatmsg",
        status=None,
        keyword=None,
        limit=20,
    )


@pytest.mark.asyncio
async def test_trace_task_detail_allows_authenticated_cross_account_lookup() -> None:
    with patch.object(
        icatmsg_server_app,
        "get_trace_task",
        return_value={"request_msg_id": "u-1", "account_id": "other_account"},
    ), patch.object(
        icatmsg_server_app,
        "get_trace_task_events",
        return_value=[{"event_name": "client.message.accepted"}],
    ):
        result = await icatmsg_server_app.get_trace_task_detail("u-1", user_id="admin_user")

    assert result["task"]["account_id"] == "other_account"
    assert result["events"][0]["event_name"] == "client.message.accepted"


@pytest.mark.asyncio
async def test_shutdown_event_cancels_workers_before_closing_kafka() -> None:
    original_outbound_kafka_worker_task = icatmsg_server_app.outbound_kafka_worker_task
    original_outbound_retry_worker_task = icatmsg_server_app.outbound_retry_worker_task
    original_pdf_conversion_tasks = list(icatmsg_server_app.pdf_conversion_tasks)
    try:
        icatmsg_server_app.outbound_kafka_worker_task = asyncio.create_task(asyncio.sleep(60))
        icatmsg_server_app.outbound_retry_worker_task = asyncio.create_task(asyncio.sleep(60))
        outbound_kafka_worker_task = icatmsg_server_app.outbound_kafka_worker_task
        outbound_retry_worker_task = icatmsg_server_app.outbound_retry_worker_task
        icatmsg_server_app.pdf_conversion_tasks.clear()
        call_order: list[tuple[str, object | None]] = []

        async def fake_cancel(task, timeout=2.0):
            call_order.append(("cancel", task))
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        async def fake_close():
            call_order.append(("close", None))

        with patch.object(icatmsg_server_app, "configure_observability") as mock_configure, \
             patch.object(icatmsg_server_app, "_cancel_task", side_effect=fake_cancel), \
             patch.object(icatmsg_server_app, "_close_kafka_clients", side_effect=fake_close):
            await icatmsg_server_app.shutdown_event()

        mock_configure.assert_called_once_with(None)
        assert call_order[0] == ("cancel", outbound_kafka_worker_task)
        assert call_order[1] == ("cancel", outbound_retry_worker_task)
        assert call_order[2] == ("close", None)
    finally:
        for task in (icatmsg_server_app.outbound_kafka_worker_task, icatmsg_server_app.outbound_retry_worker_task):
            if task is not None and not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        icatmsg_server_app.outbound_kafka_worker_task = original_outbound_kafka_worker_task
        icatmsg_server_app.outbound_retry_worker_task = original_outbound_retry_worker_task
        icatmsg_server_app.pdf_conversion_tasks.clear()
        icatmsg_server_app.pdf_conversion_tasks.update(original_pdf_conversion_tasks)


def test_icatmsg_normalizes_heartbeat_window(mock_bus):
    config = MockConfig({
        "enabled": True,
        "kafka_servers": "test:9092",
        "inbound_topic": "test_inbound",
        "outbound_topic": "test_outbound",
        "kafka_session_timeout_ms": 9000,
        "kafka_heartbeat_interval_ms": 9000,
        "kafka_rebalance_timeout_ms": 4000,
        "kafka_max_poll_interval_ms": 5000,
        "kafka_auto_commit_interval_ms": 200,
        "allow_from": ["*"],
    })
    channel = ICatMsgChannel(config=config, bus=mock_bus)

    assert channel.kafka_session_timeout_ms == 9000
    assert channel.kafka_heartbeat_interval_ms == 3000
    assert channel.kafka_rebalance_timeout_ms == 9000
    assert channel.kafka_max_poll_interval_ms == 9000
    assert channel.kafka_auto_commit_interval_ms == 1000


def test_detect_rebalance_related_exception_by_name() -> None:
    class RebalanceInProgressError(Exception):
        pass

    assert _is_rebalance_related_exception(RebalanceInProgressError("whatever")) is True
    assert _is_rebalance_related_exception(Exception("regular failure")) is False


def test_detect_rebalance_related_exception_by_message() -> None:
    exc = Exception("Heartbeat failed for group g1 because it is rebalancing")
    assert _is_rebalance_related_exception(exc) is True


def test_aiokafka_noise_filter_suppresses_rebalance_heartbeat_log() -> None:
    noise_record = MagicMock()
    noise_record.getMessage.return_value = "Heartbeat failed for group g1 because it is rebalancing"
    normal_record = MagicMock()
    normal_record.getMessage.return_value = "Kafka consumer started"
    noise_filter = _AIOKafkaNoiseFilter()
    assert noise_filter.filter(noise_record) is False
    assert noise_filter.filter(normal_record) is True


@pytest.mark.asyncio
async def test_icatmsg_processes_batched_kafka_records(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)
    channel._running = True
    channel.producer = MagicMock()
    channel.consumer = _FakeBatchConsumer(
        [
            {
                object(): [
                    _FakeKafkaMessage(
                        {
                            "header": {"version": "v1.2", "msg_id": "u-1", "event": "message.user"},
                            "payload": {"account_id": "user1", "chat_id": "chat1", "content": "hello"},
                            "metadata": {},
                        }
                    )
                ]
            }
        ],
        channel,
    )

    with patch.object(channel, "_handle_contract_event", new=AsyncMock()) as handle_event, \
         patch.object(channel, "_close_kafka_clients", new=AsyncMock()):
        await channel._kafka_receiver_loop()

    handle_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_icatmsg_server_connect_kafka_clients_sets_consumer_timeouts() -> None:
    original_producer = icatmsg_server_app.producer
    original_consumer = icatmsg_server_app.consumer
    try:
        icatmsg_server_app.producer = None
        icatmsg_server_app.consumer = None

        with patch.object(icatmsg_server_app, "AIOKafkaProducer") as mock_producer_cls, \
             patch.object(icatmsg_server_app, "AIOKafkaConsumer") as mock_consumer_cls:
            mock_producer = mock_producer_cls.return_value
            mock_producer.start = AsyncMock()
            mock_consumer = mock_consumer_cls.return_value
            mock_consumer.start = AsyncMock()

            ok = await icatmsg_server_app._connect_kafka_clients()

        assert ok is True
        mock_producer_cls.assert_called_once_with(
            bootstrap_servers=icatmsg_server_app.KAFKA_BOOTSTRAP_SERVERS,
        )
        mock_consumer_cls.assert_called_once_with(
            icatmsg_server_app.OUTBOUND_TOPIC,
            bootstrap_servers=icatmsg_server_app.KAFKA_BOOTSTRAP_SERVERS,
            group_id=icatmsg_server_app.OUTBOUND_GROUP_ID,
            session_timeout_ms=icatmsg_server_app.KAFKA_SESSION_TIMEOUT_MS,
            heartbeat_interval_ms=icatmsg_server_app.KAFKA_HEARTBEAT_INTERVAL_MS,
            rebalance_timeout_ms=icatmsg_server_app.KAFKA_REBALANCE_TIMEOUT_MS,
            max_poll_interval_ms=icatmsg_server_app.KAFKA_MAX_POLL_INTERVAL_MS,
            auto_commit_interval_ms=icatmsg_server_app.KAFKA_AUTO_COMMIT_INTERVAL_MS,
        )
        mock_producer.start.assert_called_once()
        mock_consumer.start.assert_called_once()
    finally:
        icatmsg_server_app.producer = original_producer
        icatmsg_server_app.consumer = original_consumer


@pytest.mark.asyncio
async def test_icatmsg_receive_v1_2_format(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)
    
    # Mock consumer iteration
    mock_msg = MagicMock()
    payload = {
        "header": {
            "version": "v1.2",
            "msg_id": "u-1234",
            "source": "icatmsg_server",
            "trace_id": "trace-001",
            "timestamp": 1774611916000,
        },
        "payload": {
            "account_id": "user123",
            "tenant_id": "tenant-01",
            "chat_id": "chat456",
            "content": "hello world",
            "content_type": "text",
            "client_id": "web-client-1",
            "priority": "high",
            "interaction": {
                "type": "otp",
                "context": {"tool": "doc_compare"},
                "values": {"otp_code": "246810"},
            },
            "file_meta": {"name": "1.docx", "rel_path": "u1/c1/1.docx"},
            "attachments": [{"kind": "file", "name": "2.docx", "storage": {"path": "u1/c1/2.docx"}}],
        },
        "metadata": {
            "source": "web"
        }
    }
    mock_msg.value = json.dumps(payload).encode("utf-8")
    
    async def mock_aiter():
        yield mock_msg
        
    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, \
         patch("ithqbot.channels.icatmsg.AIOKafkaConsumer") as mock_consumer_cls:
        
        mock_producer = mock_producer_cls.return_value
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()
        
        mock_consumer = mock_consumer_cls.return_value
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()
        mock_consumer.__aiter__ = MagicMock(return_value=mock_aiter())
        
        await channel.start()
        
        # Give the event loop a moment to run the consumer task
        await asyncio.sleep(0.1)
        
        mock_bus.publish_inbound.assert_called_once()
        inbound = mock_bus.publish_inbound.call_args[0][0]
        
        assert inbound.channel == "icatmsg"
        assert inbound.sender_id == "user123"
        assert inbound.chat_id == "chat456"
        assert inbound.content == "hello world"
        assert inbound.session_key_override == "icatmsg:tenant-01:user123:chat456:bot_A"
        assert inbound.metadata["account_id"] == "user123"
        assert inbound.metadata["parent_msg_id"] == "u-1234"
        assert inbound.metadata["content_type"] == "text"
        assert inbound.metadata["source"] == "web"
        assert inbound.metadata["contract_version"] == "v1.2"
        assert inbound.metadata["event_type"] == "message.user"
        assert inbound.metadata["client_id"] == "web-client-1"
        assert inbound.metadata["request_msg_id"] == "u-1234"
        assert inbound.metadata["trace_id"] == "trace-001"
        assert inbound.metadata["priority"] == "high"
        assert inbound.metadata["interaction_response"]["values"]["otp_code"] == "246810"
        assert inbound.metadata["file_meta"]["uploaded_at"] == "2026-03-27T11:45:16+00:00"
        assert inbound.metadata["attachments"][0]["uploaded_at"] == "2026-03-27T11:45:16+00:00"
        
        await channel.stop()


@pytest.mark.asyncio
async def test_icatmsg_receive_rejects_missing_v1_2_required_fields(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)

    mock_msg = MagicMock()
    payload = {
        "header": {
            "version": "v1.2",
            "source": "icatmsg_server",
            "event": "message.user",
        },
        "payload": {
            "account_id": "user123",
            "chat_id": "chat456",
            "content": "hello world",
        },
        "metadata": {
            "source": "web"
        }
    }
    mock_msg.value = json.dumps(payload).encode("utf-8")

    async def mock_aiter():
        yield mock_msg

    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, \
         patch("ithqbot.channels.icatmsg.AIOKafkaConsumer") as mock_consumer_cls, \
         patch("ithqbot.channels.icatmsg.logger.warning") as mock_warning:

        mock_producer = mock_producer_cls.return_value
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()

        mock_consumer = mock_consumer_cls.return_value
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()
        mock_consumer.__aiter__ = MagicMock(return_value=mock_aiter())

        await channel.start()
        await asyncio.sleep(0.1)

        mock_bus.publish_inbound.assert_not_called()
        assert mock_warning.called

        await channel.stop()


@pytest.mark.asyncio
async def test_icatmsg_send(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)
    
    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, \
         patch("ithqbot.channels.icatmsg.AIOKafkaConsumer") as mock_consumer_cls:
        
        mock_producer = mock_producer_cls.return_value
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()
        
        mock_consumer = mock_consumer_cls.return_value
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()
        
        await channel.start()
        
        outbound = OutboundMessage(
            channel="icatmsg",
            chat_id="chat456",
            content="response text",
            metadata={"account_id": "user123", "client_id": "client-a", "extra": "data"}
        )
        
        await channel.send(outbound)
        
        mock_producer.send_and_wait.assert_called_once()
        
        topic, kwargs = mock_producer.send_and_wait.call_args
        assert topic[0] == "test_outbound"
        
        sent_payload = json.loads(kwargs["value"].decode("utf-8"))
        assert sent_payload["header"]["version"] == "v1.2"
        assert sent_payload["header"]["event"] == "message.reply"
        assert sent_payload["payload"]["account_id"] == "user123"
        assert sent_payload["payload"]["chat_id"] == "chat456"
        assert sent_payload["payload"]["content"] == "response text"
        assert sent_payload["payload"]["client_id"] == "client-a"
        assert sent_payload["payload"]["priority"] == "normal"
        assert sent_payload["metadata"]["extra"] == "data"
        assert sent_payload["metadata"]["client_id"] == "client-a"
        assert sent_payload["metadata"]["channel"] == "icatmsg"
        assert sent_payload["metadata"]["priority"] == "normal"
        
        assert kwargs["key"] == b"user123"
        
        await channel.stop()


@pytest.mark.asyncio
async def test_icatmsg_send_uses_bot_id_for_web_default_chat_fallback(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)

    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, \
         patch("ithqbot.channels.icatmsg.AIOKafkaConsumer") as mock_consumer_cls:
        mock_producer = mock_producer_cls.return_value
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()

        mock_consumer = mock_consumer_cls.return_value
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()

        await channel.start()

        outbound = OutboundMessage(
            channel="icatmsg",
            chat_id="default_chat",
            content="response text",
            metadata={"account_id": "user123", "client_id": "web_abc", "bot_id": "bot_doc_compare"},
        )

        await channel.send(outbound)

        _, kwargs = mock_producer.send_and_wait.call_args
        sent_payload = json.loads(kwargs["value"].decode("utf-8"))
        assert sent_payload["payload"]["chat_id"] == "bot_doc_compare"

        await channel.stop()


@pytest.mark.asyncio
async def test_icatmsg_send_reconnects_after_send_failure(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)

    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, \
         patch("ithqbot.channels.icatmsg.AIOKafkaConsumer") as mock_consumer_cls:
        first_producer = MagicMock()
        first_producer.start = AsyncMock()
        first_producer.stop = AsyncMock()
        first_producer.send_and_wait = AsyncMock(side_effect=RuntimeError("kafka disconnected"))

        second_producer = MagicMock()
        second_producer.start = AsyncMock()
        second_producer.stop = AsyncMock()
        second_producer.send_and_wait = AsyncMock()

        mock_producer_cls.side_effect = [first_producer, second_producer]

        mock_consumer = mock_consumer_cls.return_value
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()

        await channel.start()

        outbound = OutboundMessage(
            channel="icatmsg",
            chat_id="chat456",
            content="retry send",
            metadata={"account_id": "user123", "client_id": "client-a"},
        )

        await channel.send(outbound)

        assert mock_producer_cls.call_count >= 2
        second_producer.send_and_wait.assert_called_once()

        await channel.stop()


@pytest.mark.asyncio
async def test_icatmsg_processing_receipt_contains_contract_fields(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)

    mock_msg = MagicMock()
    payload = {
        "header": {
            "version": "v1.2",
            "msg_id": "u-req-1",
            "source": "icatmsg_server",
            "event": "message.user",
            "trace_id": "trace-in-1",
        },
        "payload": {
            "channel": "icatmsg",
            "account_id": "user123",
            "tenant_id": "tenant01",
            "chat_id": "chat456",
            "client_id": "cli-1",
            "content": "hello",
        },
        "metadata": {},
    }
    mock_msg.value = json.dumps(payload).encode("utf-8")

    async def mock_aiter():
        yield mock_msg

    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, \
         patch("ithqbot.channels.icatmsg.AIOKafkaConsumer") as mock_consumer_cls:
        mock_producer = mock_producer_cls.return_value
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()

        mock_consumer = mock_consumer_cls.return_value
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()
        mock_consumer.__aiter__ = MagicMock(return_value=mock_aiter())

        await channel.start()
        await asyncio.sleep(0.1)

        _, kwargs = mock_producer.send_and_wait.call_args
        sent_payload = json.loads(kwargs["value"].decode("utf-8"))
        assert sent_payload["header"]["event"] == "status.processing"
        assert sent_payload["payload"]["tenant_id"] == "tenant01"
        assert sent_payload["payload"]["client_id"] == "cli-1"
        assert sent_payload["metadata"]["request_msg_id"] == "u-req-1"
        assert sent_payload["metadata"]["trace_id"] == "trace-in-1"

        await channel.stop()


@pytest.mark.asyncio
async def test_icatmsg_send_file_attachments(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)

    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, \
         patch("ithqbot.channels.icatmsg.AIOKafkaConsumer") as mock_consumer_cls:

        mock_producer = mock_producer_cls.return_value
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()

        mock_consumer = mock_consumer_cls.return_value
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()

        await channel.start()

        outbound = OutboundMessage(
            channel="icatmsg",
            chat_id="chat456",
            content="file ready",
            media=["u1/c1/f1.txt"],
            metadata={"account_id": "user123", "content_type": "file"},
        )

        await channel.send(outbound)

        _, kwargs = mock_producer.send_and_wait.call_args
        sent_payload = json.loads(kwargs["value"].decode("utf-8"))
        assert sent_payload["header"]["event"] == "message.file"
        assert sent_payload["payload"]["attachments"][0]["storage"]["path"] == "u1/c1/f1.txt"
        assert sent_payload["payload"]["content_type"] == "file"

        await channel.stop()


@pytest.mark.asyncio
async def test_icatmsg_send_accepts_minio_uri_media(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)

    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, \
         patch("ithqbot.channels.icatmsg.AIOKafkaConsumer") as mock_consumer_cls:

        mock_producer = mock_producer_cls.return_value
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()

        mock_consumer = mock_consumer_cls.return_value
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()

        await channel.start()

        outbound = OutboundMessage(
            channel="icatmsg",
            chat_id="chat456",
            content="result file ready",
            media=["minio://ithqbot-storage/user123/chat456/r1.xlsx"],
            metadata={"account_id": "user123", "content_type": "file"},
        )

        await channel.send(outbound)

        _, kwargs = mock_producer.send_and_wait.call_args
        sent_payload = json.loads(kwargs["value"].decode("utf-8"))
        attachment = sent_payload["payload"]["attachments"][0]
        assert sent_payload["header"]["event"] == "message.file"
        assert attachment["storage_uri"] == "minio://ithqbot-storage/user123/chat456/r1.xlsx"
        assert attachment["minio_uri"] == attachment["storage_uri"]
        assert attachment["storage"]["backend"] == "minio"
        assert attachment["storage"]["bucket"] == "ithqbot-storage"
        assert attachment["storage"]["path"] == "user123/chat456/r1.xlsx"

        await channel.stop()


@pytest.mark.asyncio
async def test_icatmsg_send_accepts_s3_uri_media(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)

    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, \
         patch("ithqbot.channels.icatmsg.AIOKafkaConsumer") as mock_consumer_cls:

        mock_producer = mock_producer_cls.return_value
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()

        mock_consumer = mock_consumer_cls.return_value
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()

        await channel.start()

        outbound = OutboundMessage(
            channel="icatmsg",
            chat_id="chat456",
            content="result file ready",
            media=["s3://bucket-a/user123/chat456/r1.xlsx"],
            metadata={"account_id": "user123", "content_type": "file"},
        )

        await channel.send(outbound)

        _, kwargs = mock_producer.send_and_wait.call_args
        sent_payload = json.loads(kwargs["value"].decode("utf-8"))
        attachment = sent_payload["payload"]["attachments"][0]
        assert sent_payload["header"]["event"] == "message.file"
        assert attachment["storage_uri"] == "s3://bucket-a/user123/chat456/r1.xlsx"
        assert attachment["s3_uri"] == attachment["storage_uri"]
        assert "minio_uri" not in attachment
        assert attachment["storage"]["backend"] == "s3"
        assert attachment["storage"]["bucket"] == "bucket-a"
        assert attachment["storage"]["path"] == "user123/chat456/r1.xlsx"

        await channel.stop()


def test_icatmsg_normalize_attachments_accepts_storage_uri_objects(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)

    normalized = channel._normalize_attachments(
        [
            {
                "kind": "file",
                "name": "a.txt",
                "storage_uri": "s3://bucket-a/u1/c1/a.txt",
            }
        ]
    )

    assert normalized[0]["storage_uri"] == "s3://bucket-a/u1/c1/a.txt"
    assert normalized[0]["s3_uri"] == "s3://bucket-a/u1/c1/a.txt"
    assert normalized[0]["storage"]["backend"] == "s3"
    assert normalized[0]["storage"]["bucket"] == "bucket-a"
    assert normalized[0]["storage"]["path"] == "u1/c1/a.txt"


@pytest.mark.asyncio
async def test_icatmsg_send_supports_metadata_files_and_reply_to(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)

    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, \
         patch("ithqbot.channels.icatmsg.AIOKafkaConsumer") as mock_consumer_cls:
        mock_producer = mock_producer_cls.return_value
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()

        mock_consumer = mock_consumer_cls.return_value
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()

        await channel.start()

        outbound = OutboundMessage(
            channel="icatmsg",
            chat_id="chat456",
            content="结果已生成",
            reply_to="u-ask-1",
            metadata={
                "account_id": "user123",
                "content_type": "file",
                "files": [{"kind": "file", "storage": {"path": "user123/chat456/r1.xlsx"}}],
            },
        )
        await channel.send(outbound)

        _, kwargs = mock_producer.send_and_wait.call_args
        sent_payload = json.loads(kwargs["value"].decode("utf-8"))
        assert sent_payload["header"]["event"] == "message.file"
        assert sent_payload["header"]["parent_msg_id"] == "u-ask-1"
        assert sent_payload["payload"]["reply_to"] == "u-ask-1"
        assert sent_payload["payload"]["attachments"][0]["storage"]["path"] == "user123/chat456/r1.xlsx"
        assert sent_payload["payload"]["files"][0]["storage"]["path"] == "user123/chat456/r1.xlsx"

        await channel.stop()


@pytest.mark.asyncio
async def test_icatmsg_send_accepts_metadata_files_with_file_id_and_storage(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)

    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, \
         patch("ithqbot.channels.icatmsg.AIOKafkaConsumer") as mock_consumer_cls:
        mock_producer = mock_producer_cls.return_value
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()

        mock_consumer = mock_consumer_cls.return_value
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()

        await channel.start()

        outbound = OutboundMessage(
            channel="icatmsg",
            chat_id="chat456",
            content="摘要文件已生成",
            metadata={
                "account_id": "user123",
                "content_type": "file",
                "files": [
                    {
                        "file_id": "f_summary_123",
                        "name": "summary.txt",
                        "storage": {"backend": "minio", "bucket": "ithqbot-storage", "path": "t1/u1/b1/c1/r1/f_summary_123/summary.txt"},
                    }
                ],
            },
        )
        await channel.send(outbound)

        _, kwargs = mock_producer.send_and_wait.call_args
        sent_payload = json.loads(kwargs["value"].decode("utf-8"))
        assert sent_payload["header"]["event"] == "message.file"
        assert sent_payload["payload"]["attachments"][0]["storage"]["path"] == "t1/u1/b1/c1/r1/f_summary_123/summary.txt"
        assert sent_payload["payload"]["files"][0]["file_id"] == "f_summary_123"

        await channel.stop()


@pytest.mark.asyncio
async def test_icatmsg_send_interaction_event(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)

    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, \
         patch("ithqbot.channels.icatmsg.AIOKafkaConsumer") as mock_consumer_cls:
        mock_producer = mock_producer_cls.return_value
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()

        mock_consumer = mock_consumer_cls.return_value
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()

        await channel.start()

        outbound = OutboundMessage(
            channel="icatmsg",
            chat_id="chat456",
            content="请选择环境",
            reply_to="u-req-1",
            metadata={
                "account_id": "user123",
                "interaction": {
                    "type": "select",
                    "title": "金库环境",
                    "options": [{"label": "prod", "value": "prod"}],
                },
            },
        )
        await channel.send(outbound)

        _, kwargs = mock_producer.send_and_wait.call_args
        sent_payload = json.loads(kwargs["value"].decode("utf-8"))
        assert sent_payload["header"]["event"] == "message.interaction"
        assert sent_payload["payload"]["interaction"]["type"] == "select"
        assert sent_payload["payload"]["reply_to"] == "u-req-1"

        await channel.stop()


@pytest.mark.asyncio
async def test_icatmsg_send_progress_event_with_percentage(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)

    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, \
         patch("ithqbot.channels.icatmsg.AIOKafkaConsumer") as mock_consumer_cls:
        mock_producer = mock_producer_cls.return_value
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()

        mock_consumer = mock_consumer_cls.return_value
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()

        await channel.start()

        outbound = OutboundMessage(
            channel="icatmsg",
            chat_id="chat456",
            content="正在读取附件2",
            metadata={
                "account_id": "user123",
                "parent_msg_id": "u-123",
                "_progress": True,
                "_progress_percent": 45,
                "_progress_kind": "tool_hint",
                "_progress_stage": "comparing",
                "_tool_name": "web_search",
                "_call_type": "tool",
                "_status_details": {"graph": {"run_id": "run-123"}, "show_progress_percent": False},
                "attachments": [{"kind": "file", "storage": {"path": "user123/chat456/a.xlsx"}}],
            },
        )

        await channel.send(outbound)

        _, kwargs = mock_producer.send_and_wait.call_args
        sent_payload = json.loads(kwargs["value"].decode("utf-8"))
        assert sent_payload["header"]["event"] == "status.processing"
        assert sent_payload["payload"]["status"]["request_msg_id"] == "u-123"
        assert sent_payload["payload"]["status"]["progress_percent"] is None
        assert sent_payload["payload"]["status"]["message"] == "正在读取附件2"
        assert sent_payload["payload"]["status"]["kind"] == "tool_hint"
        assert sent_payload["payload"]["status"]["stage"] == "comparing"
        assert sent_payload["payload"]["status"]["tool_name"] == "web_search"
        assert sent_payload["payload"]["status"]["call_type"] == "tool"
        assert sent_payload["payload"]["status"]["details"] == {
            "graph": {"run_id": "run-123"},
            "show_progress_percent": False,
        }

        await channel.stop()


@pytest.mark.asyncio
async def test_icatmsg_receive_with_custom_adapter(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)
    channel.register_channel_adapter(ChannelAdapter("custom", processing_receipt=False))

    mock_msg = MagicMock()
    payload = {
        "header": {
            "version": "v1.2",
            "msg_id": "u-custom-1",
            "source": "icatmsg_server",
            "event": "message.user",
        },
        "payload": {
            "channel": "custom",
            "account_id": "user123",
            "tenant_id": "tenant01",
            "chat_id": "chat456",
            "client_id": "cli-1",
            "content": "hello custom",
            "content_type": "text",
        },
        "metadata": {
            "source": "web"
        }
    }
    mock_msg.value = json.dumps(payload).encode("utf-8")

    async def mock_aiter():
        yield mock_msg

    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, \
         patch("ithqbot.channels.icatmsg.AIOKafkaConsumer") as mock_consumer_cls:
        mock_producer = mock_producer_cls.return_value
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()

        mock_consumer = mock_consumer_cls.return_value
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()
        mock_consumer.__aiter__ = MagicMock(return_value=mock_aiter())

        await channel.start()
        await asyncio.sleep(0.1)

        mock_bus.publish_inbound.assert_called_once()
        inbound = mock_bus.publish_inbound.call_args[0][0]
        assert inbound.session_key_override == "custom:tenant01:user123:chat456:bot_A"
        assert inbound.metadata["source_channel"] == "custom"
        assert inbound.metadata["event_type"] == "message.user"
        assert mock_producer.send_and_wait.call_count == 0

        await channel.stop()


@pytest.mark.asyncio
async def test_icatmsg_receive_unsupported_event_skips_publish(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)

    mock_msg = MagicMock()
    payload = {
        "header": {
            "version": "v1.2",
            "msg_id": "u-status-1",
            "source": "icatmsg_server",
            "event": "status.processing",
        },
        "payload": {
            "channel": "icatmsg",
            "account_id": "user123",
            "tenant_id": "tenant01",
            "chat_id": "chat456",
            "client_id": "cli-1",
            "content": "ignored",
        },
        "metadata": {},
    }
    mock_msg.value = json.dumps(payload).encode("utf-8")

    async def mock_aiter():
        yield mock_msg

    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, \
         patch("ithqbot.channels.icatmsg.AIOKafkaConsumer") as mock_consumer_cls:
        mock_producer = mock_producer_cls.return_value
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()

        mock_consumer = mock_consumer_cls.return_value
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()
        mock_consumer.__aiter__ = MagicMock(return_value=mock_aiter())

        await channel.start()
        await asyncio.sleep(0.1)

        mock_bus.publish_inbound.assert_not_called()
        mock_producer.send_and_wait.assert_not_called()

        await channel.stop()


@pytest.mark.asyncio
async def test_icatmsg_send_invalid_attachment_contract_is_blocked(mock_bus, channel_config):
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)

    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, \
         patch("ithqbot.channels.icatmsg.AIOKafkaConsumer") as mock_consumer_cls:
        mock_producer = mock_producer_cls.return_value
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()
        mock_producer.send_and_wait = AsyncMock()

        mock_consumer = mock_consumer_cls.return_value
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()

        await channel.start()

        outbound = OutboundMessage(
            channel="icatmsg",
            chat_id="chat456",
            content="file reply",
            metadata={
                "account_id": "user123",
                "content_type": "file",
                "attachments": [{"kind": "file"}],
            },
        )
        await channel.send(outbound)
        mock_producer.send_and_wait.assert_not_called()

        await channel.stop()


def test_feishu_preflight_marks_app_id_invalid() -> None:
    bot = FeishuBot(config={"app_id": "cli_invalid", "app_secret": "secret"}, bot_id="bot_bad")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"code": 99991663, "msg": "app_id invalid"}'

    with patch("icatmsg.server.feishu_bot.urllib.request.urlopen", return_value=_Resp()):
        ok = bot._preflight_credentials_sync()

    health = bot.get_channel_health()
    assert ok is False
    assert health["status"] == "unhealthy"
    assert health["error_code"] == "FEISHU_APP_ID_INVALID"
    assert "app_id" in health["suggestion"]


def test_feishu_preflight_marks_healthy() -> None:
    bot = FeishuBot(config={"app_id": "cli_ok", "app_secret": "secret"}, bot_id="bot_ok")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"code": 0, "msg": "success"}'

    with patch("icatmsg.server.feishu_bot.urllib.request.urlopen", return_value=_Resp()):
        ok = bot._preflight_credentials_sync()

    health = bot.get_channel_health()
    assert ok is True
    assert health["status"] == "healthy"
    assert health["error_code"] == "FEISHU_HEALTHY"


def test_normalize_interaction_builds_otp_defaults() -> None:
    interaction = icatmsg_server_app._normalize_interaction({
        "type": "otp",
        "title": "短信验证",
        "prompt": "请输入验证码",
    })
    assert interaction is not None
    assert interaction["type"] == "otp"
    assert interaction["version"] == "v1"
    assert interaction["sensitive"] is True
    assert interaction["fields"][0]["input_type"] == "otp"


def test_build_feishu_outbound_message_normalizes_interaction_payload() -> None:
    body = {
        "account_id": "u1",
        "chat_id": "c1",
        "content": "请确认",
        "interaction": {
            "type": "confirm",
            "prompt": "确认发布吗？",
        },
    }
    header = {"event": "message.interaction"}
    metadata = {}
    outbound = icatmsg_server_app._build_feishu_outbound_message(body, header, metadata)
    assert outbound["interaction"]["type"] == "confirm"
    assert len(outbound["interaction"]["options"]) == 2
    assert outbound["interaction"]["options"][0]["value"] == "yes"


@pytest.mark.asyncio
async def test_feishu_send_renders_interaction_card() -> None:
    bot = FeishuBot(config={"app_id": "cli_ok", "app_secret": "secret"}, bot_id="bot_interaction")
    bot._client = MagicMock()
    with patch.object(bot, "_send_message_sync", return_value=True) as mock_send:
        await bot.send({
            "chat_id": "ou_xxx",
            "content": "请选择环境",
            "interaction": {
                "type": "select",
                "title": "环境选择",
                "prompt": "请选择部署环境",
                "options": [
                    {"label": "生产", "value": "prod"},
                    {"label": "预发", "value": "staging"},
                ],
            },
            "metadata": {},
        })
    args = mock_send.call_args.args
    assert args[2] == "interactive"
    card = json.loads(args[3])
    assert card["header"]["title"]["content"] == "[select] 环境选择"
    assert card["elements"][0]["tag"] == "div"
    assert card["elements"][0]["text"]["tag"] == "lark_md"
    assert "可选项" in card["elements"][0]["text"]["content"]
    assert "回复格式" in card["elements"][0]["text"]["content"]


@pytest.mark.asyncio
async def test_feishu_send_renders_otp_input_box() -> None:
    bot = FeishuBot(config={"app_id": "cli_ok", "app_secret": "secret"}, bot_id="bot_interaction_otp")
    bot._client = MagicMock()
    with patch.object(bot, "_send_message_sync", return_value=True) as mock_send:
        await bot.send({
            "chat_id": "ou_xxx",
            "content": "请输入验证码",
            "interaction": {
                "type": "otp",
                "title": "下载对比结果需要验证码",
                "prompt": "请输入验证码后下载本次文档对比结果",
                "fields": [
                    {"key": "otp_code", "label": "验证码", "input_type": "otp", "required": True}
                ],
            },
            "metadata": {},
        })
    args = mock_send.call_args.args
    assert args[2] == "interactive"
    card = json.loads(args[3])
    assert card["header"]["title"]["content"] == "[otp] 下载对比结果需要验证码"
    assert "otp_code" in card["elements"][0]["text"]["content"]
    assert "请输入验证码后下载本次文档对比结果" in card["elements"][0]["text"]["content"]


@pytest.mark.asyncio
async def test_feishu_send_parses_legacy_interaction_text_to_card() -> None:
    bot = FeishuBot(config={"app_id": "cli_ok", "app_secret": "secret"}, bot_id="bot_interaction_legacy")
    bot._client = MagicMock()
    with patch.object(bot, "_send_message_sync", return_value=True) as mock_send:
        await bot.send({
            "chat_id": "ou_xxx",
            "content": "[otp] 下载对比结果需要验证码\n请输入验证码后下载本次文档对比结果\n输入字段\n- 验证码 (otp_code) · otp · 必填\n回复格式\n请回复：验证码",
            "metadata": {},
        })
    args = mock_send.call_args.args
    assert args[2] == "interactive"
    card = json.loads(args[3])
    assert card["header"]["title"]["content"] == "[otp] 下载对比结果需要验证码"
    assert "otp_code" in card["elements"][0]["text"]["content"]


@pytest.mark.asyncio
async def test_feishu_send_parses_legacy_interaction_text_with_code_fence_and_fullwidth_brackets() -> None:
    bot = FeishuBot(config={"app_id": "cli_ok", "app_secret": "secret"}, bot_id="bot_interaction_legacy_fullwidth")
    bot._client = MagicMock()
    with patch.object(bot, "_send_message_sync", return_value=True) as mock_send:
        await bot.send({
            "chat_id": "ou_xxx",
            "content": "```markdown\n［otp］ 下载对比结果需要验证码\n请输入验证码后下载本次文档对比结果\n* 验证码 (otp_code) · otp · 必填\n```",
            "metadata": {},
        })
    args = mock_send.call_args.args
    assert args[2] == "interactive"
    card = json.loads(args[3])
    assert card["header"]["title"]["content"] == "[otp] 下载对比结果需要验证码"
    assert "otp_code" in card["elements"][0]["text"]["content"]


@pytest.mark.asyncio
async def test_feishu_handle_message_uses_configured_account_id_mapping() -> None:
    mock_producer = MagicMock()
    mock_producer.send_and_wait = AsyncMock()
    bot = FeishuBot(
        config={
            "app_id": "cli_test_app",
            "app_secret": "secret",
            "account_id": "acct_fixed_001",
            "inbound_topic": "icatmsg_inbound",
        },
        producer=mock_producer,
        bot_id="bot_A",
    )

    await bot._handle_message(
        sender_id="ou_sender_123",
        chat_id="oc_chat_123",
        content="hello",
        media=[],
        metadata={"message_id": "m_001"},
    )

    args, kwargs = mock_producer.send_and_wait.call_args
    assert args[0] == "icatmsg_inbound"
    payload = json.loads(kwargs["value"].decode("utf-8"))
    assert payload["payload"]["account_id"] == "acct_fixed_001"
    assert payload["metadata"]["channel_user_id"] == "ou_sender_123"
    assert payload["metadata"]["feishu_app_id"] == "cli_test_app"
    assert kwargs["key"] == b"acct_fixed_001"


@pytest.mark.asyncio
async def test_channels_health_endpoint_returns_feishu_structured_status() -> None:
    class _FakeBot:
        def __init__(self, status: str):
            self._status = status

        def get_channel_health(self):
            return {
                "channel": "feishu",
                "bot_id": f"bot_{self._status}",
                "status": self._status,
                "error_code": "FEISHU_HEALTHY" if self._status == "healthy" else "FEISHU_TOKEN_FETCH_FAILED",
                "message": "ok" if self._status == "healthy" else "token failed",
                "suggestion": "none" if self._status == "healthy" else "check app_secret",
                "checked_at_ms": 1,
                "app_id": "cli_test",
                "details": {},
            }

    icatmsg_server_app.feishu_bots.clear()
    icatmsg_server_app.channel_health.clear()
    icatmsg_server_app.channel_health.update({"feishu": {"status": "unknown", "checked_at_ms": 0, "bots": {}}})
    icatmsg_server_app.feishu_bots["bot_healthy"] = _FakeBot("healthy")
    icatmsg_server_app.feishu_bots["bot_unhealthy"] = _FakeBot("unhealthy")

    payload = await icatmsg_server_app.get_channels_health()

    assert payload["status"] == "ok"
    assert payload["channels"]["feishu"]["status"] == "unhealthy"
    assert payload["channels"]["feishu"]["bots"]["bot_unhealthy"]["error_code"] == "FEISHU_TOKEN_FETCH_FAILED"


@pytest.mark.asyncio
async def test_upload_file_falls_back_to_local_storage(tmp_path) -> None:
    original_root = icatmsg_server_app.LOCAL_UPLOAD_ROOT
    icatmsg_server_app.LOCAL_UPLOAD_ROOT = tmp_path
    try:
        fake_file = MagicMock()
        fake_file.filename = "a.txt"
        fake_file.content_type = "text/plain"
        fake_file.read = AsyncMock(return_value=b"abc")

        with patch.object(icatmsg_server_app, "_run_minio_io", side_effect=RuntimeError("minio down")):
            payload = await icatmsg_server_app.upload_file("user1", "chat1", fake_file, None)

        assert payload["storage_backend"] == "local"
        local_file = (tmp_path / payload["rel_path"]).resolve()
        assert local_file.exists()
        assert local_file.read_bytes() == b"abc"
    finally:
        icatmsg_server_app.LOCAL_UPLOAD_ROOT = original_root


@pytest.mark.asyncio
async def test_download_file_prefers_local_storage(tmp_path) -> None:
    original_root = icatmsg_server_app.LOCAL_UPLOAD_ROOT
    icatmsg_server_app.LOCAL_UPLOAD_ROOT = tmp_path
    try:
        rel_path = "user1/chat1/a.txt"
        local_file = (tmp_path / rel_path).resolve()
        local_file.parent.mkdir(parents=True, exist_ok=True)
        local_file.write_bytes(b"hello")

        response = await icatmsg_server_app.download_file(rel_path, "user1")

        assert response.path == str(local_file)
        assert response.media_type == "application/octet-stream"
    finally:
        icatmsg_server_app.LOCAL_UPLOAD_ROOT = original_root


@pytest.mark.asyncio
async def test_download_file_requires_authenticated_user() -> None:
    with pytest.raises(HTTPException) as exc:
        await icatmsg_server_app.download_file("u1/c1/a.txt", None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_download_file_accepts_minio_uri_path() -> None:
    fake_response = MagicMock()
    fake_response.stream.return_value = [b"abc"]
    fake_response.close = MagicMock()
    fake_response.release_conn = MagicMock()
    with patch.object(icatmsg_server_app, "_run_minio_io", new=AsyncMock(return_value=fake_response)) as mock_io:
        response = await icatmsg_server_app.download_file("minio://other-bucket/u1/c1/a.txt", user_id="u1")

    assert response.status_code == 200
    assert mock_io.await_count == 1
    get_object_call = mock_io.await_args.args[1]
    assert callable(get_object_call)


def test_server_normalize_attachments_accepts_minio_uri() -> None:
    normalized = icatmsg_server_app._normalize_attachments(["minio://ithqbot-storage/u1/c1/a.txt"])
    assert normalized[0]["storage"]["backend"] == "minio"
    assert normalized[0]["storage"]["bucket"] == "ithqbot-storage"
    assert normalized[0]["storage"]["path"] == "u1/c1/a.txt"


def test_server_attachments_to_files_builds_file_meta() -> None:
    attachments = [
        {
            "kind": "file",
            "name": "a.txt",
            "size": 3,
            "mime": "text/plain",
            "download_url": "https://example.com/a.txt",
            "storage": {"backend": "minio", "bucket": "ithqbot-storage", "path": "u1/c1/a.txt"},
        }
    ]
    files = icatmsg_server_app._attachments_to_files(attachments)
    assert len(files) == 1
    assert files[0]["rel_path"] == "u1/c1/a.txt"
    assert files[0]["storage_backend"] == "minio"
    assert files[0]["storage_bucket"] == "ithqbot-storage"
    assert files[0]["minio_uri"] == "minio://ithqbot-storage/u1/c1/a.txt"
    assert files[0]["download_url"] == "https://example.com/a.txt"
    assert files[0]["name"] == "a.txt"


def test_server_attachments_for_client_prefers_minio_uri() -> None:
    attachments = [
        {
            "kind": "file",
            "storage": {"backend": "minio", "bucket": "ithqbot-storage", "path": "u1/c1/a.txt"},
        }
    ]
    rendered = icatmsg_server_app._attachments_for_client(attachments)
    assert rendered == ["minio://ithqbot-storage/u1/c1/a.txt"]


def test_status_delivery_dedup_respects_progress_content_key() -> None:
    icatmsg_server_app.recent_deliveries.clear()
    first = icatmsg_server_app._mark_delivery("u1", "c1", "status", "req-1", "processing:parsing:10:分析中:")
    second = icatmsg_server_app._mark_delivery("u1", "c1", "status", "req-1", "processing:parsing:10:分析中:")
    third = icatmsg_server_app._mark_delivery("u1", "c1", "status", "req-1", "processing:comparing:40:读取附件:")
    assert first is True
    assert second is False
    assert third is True


def test_progress_stage_fallback_from_percent() -> None:
    assert icatmsg_server_app._normalize_progress_stage(None, 5) == "queued"
    assert icatmsg_server_app._normalize_progress_stage(None, 20) == "parsing"
    assert icatmsg_server_app._normalize_progress_stage(None, 40) == "extracting"
    assert icatmsg_server_app._normalize_progress_stage(None, 70) == "comparing"
    assert icatmsg_server_app._normalize_progress_stage(None, 95) == "finalizing"
    assert icatmsg_server_app._normalize_progress_stage("unknown", 70) == "comparing"
    assert icatmsg_server_app._normalize_progress_stage("comparing", 10) == "comparing"
    assert icatmsg_server_app._normalize_progress_stage("tool_call", 10) == "tool_call"
    assert icatmsg_server_app._normalize_progress_stage("skill_call", 10) == "skill_call"


def test_build_feishu_outbound_message_uses_payload_and_merged_metadata() -> None:
    body = {
        "channel": "feishu",
        "account_id": "u1",
        "chat_id": "oc_xxx",
        "content": "hello",
        "metadata": {"from_body": 1},
    }
    header = {"parent_msg_id": "m-parent"}
    metadata = {"from_envelope": 2}

    msg = icatmsg_server_app._build_feishu_outbound_message(body, header, metadata)

    assert msg["chat_id"] == "oc_xxx"
    assert msg["content"] == "hello"
    assert msg["metadata"]["from_body"] == 1
    assert msg["metadata"]["from_envelope"] == 2
    assert msg["metadata"]["message_id"] == "m-parent"


def test_merge_bot_config_metadata_injects_profile_when_missing() -> None:
    with patch.object(
        icatmsg_server_app,
        "_resolve_bot_config",
        return_value={"id": "bot_A", "name": "运维专家", "description": "ops-only"},
    ):
        merged = icatmsg_server_app._merge_bot_config_metadata({"trace_id": "t-1"}, "bot_A")
    assert merged["trace_id"] == "t-1"
    assert merged["bot_config"]["id"] == "bot_A"


def test_merge_bot_config_metadata_keeps_existing_profile() -> None:
    original = {"bot_config": {"id": "custom", "description": "custom-desc"}, "x": 1}
    merged = icatmsg_server_app._merge_bot_config_metadata(original, "bot_A")
    assert merged["x"] == 1
    assert merged["bot_config"]["id"] == "custom"


def test_resolve_tenant_id_prefers_explicit_then_metadata_then_default() -> None:
    assert icatmsg_server_app._resolve_tenant_id("tenant_explicit", {"tenant_id": "tenant_meta"}) == "tenant_explicit"
    assert icatmsg_server_app._resolve_tenant_id(None, {"tenant_id": "tenant_meta"}) == "tenant_meta"
    assert icatmsg_server_app._resolve_tenant_id(None, {}) == icatmsg_server_app.DEFAULT_TENANT_ID


def test_resolve_feishu_target_bot_prefers_account_app_over_bot_id() -> None:
    bot_a1 = object()
    bot_a2 = object()
    original_by_id = dict(icatmsg_server_app.feishu_bots)
    original_by_app = dict(icatmsg_server_app.feishu_bots_by_app_id)
    original_by_account = dict(icatmsg_server_app.feishu_bots_by_account_id)
    original_by_account_app = dict(icatmsg_server_app.feishu_bots_by_account_app)
    original_by_bot_id = {
        key: list(value) for key, value in icatmsg_server_app.feishu_bots_by_bot_id.items()
    }
    try:
        icatmsg_server_app.feishu_bots.clear()
        icatmsg_server_app.feishu_bots_by_app_id.clear()
        icatmsg_server_app.feishu_bots_by_account_id.clear()
        icatmsg_server_app.feishu_bots_by_account_app.clear()
        icatmsg_server_app.feishu_bots_by_bot_id.clear()
        icatmsg_server_app.feishu_bots["bot_A#0"] = bot_a1
        icatmsg_server_app.feishu_bots["bot_A#1"] = bot_a2
        icatmsg_server_app.feishu_bots_by_bot_id["bot_A"] = [bot_a1, bot_a2]
        icatmsg_server_app.feishu_bots_by_app_id["cli_first"] = bot_a1
        icatmsg_server_app.feishu_bots_by_app_id["cli_second"] = bot_a2
        icatmsg_server_app.feishu_bots_by_account_id["acct-1"] = bot_a1
        icatmsg_server_app.feishu_bots_by_account_id["acct-2"] = bot_a2
        icatmsg_server_app.feishu_bots_by_account_app[("acct-1", "cli_second")] = bot_a2

        resolved = icatmsg_server_app._resolve_feishu_target_bot(
            "bot_A",
            {"feishu_app_id": "cli_second"},
            "acct-1",
        )
        assert resolved is bot_a2
    finally:
        icatmsg_server_app.feishu_bots.clear()
        icatmsg_server_app.feishu_bots.update(original_by_id)
        icatmsg_server_app.feishu_bots_by_app_id.clear()
        icatmsg_server_app.feishu_bots_by_app_id.update(original_by_app)
        icatmsg_server_app.feishu_bots_by_account_id.clear()
        icatmsg_server_app.feishu_bots_by_account_id.update(original_by_account)
        icatmsg_server_app.feishu_bots_by_account_app.clear()
        icatmsg_server_app.feishu_bots_by_account_app.update(original_by_account_app)
        icatmsg_server_app.feishu_bots_by_bot_id.clear()
        icatmsg_server_app.feishu_bots_by_bot_id.update(original_by_bot_id)


@pytest.mark.asyncio
async def test_upload_file_rejects_invalid_path_segment() -> None:
    fake_file = MagicMock()
    fake_file.filename = "a.txt"
    fake_file.content_type = "text/plain"
    fake_file.read = AsyncMock(return_value=b"abc")
    with pytest.raises(HTTPException) as exc:
        await icatmsg_server_app.upload_file("user1", "../chat1", fake_file, None)
    assert exc.value.status_code == 400


def test_pdf_source_extension_validation() -> None:
    assert icatmsg_server_app._is_supported_pdf_source("u1/c1/a.docx") is True
    assert icatmsg_server_app._is_supported_pdf_source("u1/c1/a.ppt") is True
    assert icatmsg_server_app._is_supported_pdf_source("u1/c1/a.txt") is False


def test_pdf_conversion_request_text_detection() -> None:
    assert icatmsg_server_app._is_pdf_conversion_request_text("请帮我转PDF") is True
    assert icatmsg_server_app._is_pdf_conversion_request_text("convert this file to pdf") is True
    assert icatmsg_server_app._is_pdf_conversion_request_text("PDF 是什么") is False


def test_resolve_pdf_conversion_source_prefers_supported_file() -> None:
    source = icatmsg_server_app._resolve_pdf_conversion_source(
        {"name": "a.txt", "rel_path": "u1/c1/a.txt"},
        [
            {"kind": "file", "name": "b.docx", "storage": {"path": "u1/c1/b.docx"}},
            {"kind": "file", "name": "c.pptx", "storage": {"path": "u1/c1/c.pptx"}},
        ],
    )
    assert source == "u1/c1/b.docx"


@pytest.mark.asyncio
async def test_convert_document_to_pdf_accepts_and_enqueues_task() -> None:
    original_producer = icatmsg_server_app.producer
    try:
        icatmsg_server_app.producer = MagicMock()
        req = icatmsg_server_app.PdfConversionRequest(
            account_id="u1",
            chat_id="c1",
            rel_path="u1/c1/source.docx",
            priority="high",
            metadata={},
        )
        with patch.object(icatmsg_server_app, "_run_pdf_conversion_job", new=AsyncMock()) as mock_job:
            payload = await icatmsg_server_app.convert_document_to_pdf(req, user_id="u1")
            await asyncio.sleep(0)
        assert payload["status"] == "accepted"
        assert payload["skill"] == "doc-ppt-to-pdf"
        assert payload["request_msg_id"].startswith("u-")
        assert mock_job.await_count == 1
    finally:
        icatmsg_server_app.producer = original_producer


@pytest.mark.asyncio
async def test_send_message_routes_pdf_conversion_from_conversation() -> None:
    original_producer = icatmsg_server_app.producer
    try:
        icatmsg_server_app.producer = MagicMock()
        msg = icatmsg_server_app.Message(
            account_id="u1",
            chat_id="c1",
            content="请帮我把这个文档转成PDF",
            content_type="file",
            file_meta={
                "name": "方案.docx",
                "rel_path": "u1/c1/方案.docx",
                "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "size": 1024,
            },
            metadata={},
        )
        with patch.object(icatmsg_server_app, "_run_pdf_conversion_job", new=AsyncMock()) as mock_job:
            payload = await icatmsg_server_app.send_message(msg, user_id="u1")
            await asyncio.sleep(0)
        assert payload["status"] == "accepted"
        assert payload["skill"] == "doc-ppt-to-pdf"
        assert payload["trigger"] == "conversation"
        assert payload["rel_path"] == "u1/c1/方案.docx"
        assert mock_job.await_count == 1
        assert not hasattr(icatmsg_server_app.producer, "send_and_wait") or icatmsg_server_app.producer.send_and_wait.call_count == 0
    finally:
        icatmsg_server_app.producer = original_producer


@pytest.mark.asyncio
async def test_send_message_injects_default_tenant_id_when_missing() -> None:
    original_producer = icatmsg_server_app.producer
    mock_producer = MagicMock()
    mock_producer.send_and_wait = AsyncMock()
    icatmsg_server_app.producer = mock_producer
    try:
        msg = icatmsg_server_app.Message(
            account_id="u1",
            chat_id="c1",
            content="hello",
            metadata={},
        )
        await icatmsg_server_app.send_message(msg, None)
        args, _ = mock_producer.send_and_wait.call_args
        payload = json.loads(args[1].decode("utf-8"))
        assert payload["payload"]["tenant_id"] == icatmsg_server_app.DEFAULT_TENANT_ID
        assert payload["metadata"]["tenant_id"] == icatmsg_server_app.DEFAULT_TENANT_ID
        assert payload["payload"]["priority"] == "normal"
        assert payload["metadata"]["priority"] == "normal"
    finally:
        icatmsg_server_app.producer = original_producer


@pytest.mark.asyncio
async def test_send_message_accepts_priority_alias() -> None:
    original_producer = icatmsg_server_app.producer
    mock_producer = MagicMock()
    mock_producer.send_and_wait = AsyncMock()
    icatmsg_server_app.producer = mock_producer
    try:
        msg = icatmsg_server_app.Message(
            account_id="u1",
            chat_id="c1",
            content="告警",
            metadata={},
            priority="urgent",
        )
        await icatmsg_server_app.send_message(msg, None)
        args, _ = mock_producer.send_and_wait.call_args
        payload = json.loads(args[1].decode("utf-8"))
        assert payload["payload"]["priority"] == "critical"
        assert payload["metadata"]["priority"] == "critical"
    finally:
        icatmsg_server_app.producer = original_producer


@pytest.mark.asyncio
async def test_send_message_includes_normalized_interaction_payload() -> None:
    original_producer = icatmsg_server_app.producer
    mock_producer = MagicMock()
    mock_producer.send_and_wait = AsyncMock()
    icatmsg_server_app.producer = mock_producer
    try:
        msg = icatmsg_server_app.Message(
            account_id="u1",
            chat_id="c1",
            content="246810",
            metadata={},
            interaction={
                "type": "otp",
                "title": "下载验证码",
                "prompt": "请输入验证码",
                "values": {"otp_code": "246810"},
            },
        )
        await icatmsg_server_app.send_message(msg, "u1")
        args, _ = mock_producer.send_and_wait.call_args
        payload = json.loads(args[1].decode("utf-8"))
        assert payload["payload"]["interaction"]["type"] == "otp"
        assert payload["payload"]["interaction"]["fields"][0]["key"] == "otp_code"
        assert payload["metadata"]["interaction"]["type"] == "otp"
    finally:
        icatmsg_server_app.producer = original_producer


@pytest.mark.asyncio
async def test_send_message_forwards_reply_to() -> None:
    original_producer = icatmsg_server_app.producer
    mock_producer = MagicMock()
    mock_producer.send_and_wait = AsyncMock()
    icatmsg_server_app.producer = mock_producer
    try:
        msg = icatmsg_server_app.Message(
            account_id="u1",
            chat_id="c1",
            content="246810",
            metadata={},
            reply_to="u-otp-1",
        )
        await icatmsg_server_app.send_message(msg, "u1")
        args, _ = mock_producer.send_and_wait.call_args
        payload = json.loads(args[1].decode("utf-8"))
        assert payload["payload"]["reply_to"] == "u-otp-1"
    finally:
        icatmsg_server_app.producer = original_producer


@pytest.mark.asyncio
async def test_outbound_retry_succeeds_on_second_attempt() -> None:
    original_queue = icatmsg_server_app.outbound_retry_queue
    icatmsg_server_app.outbound_retry_queue = asyncio.PriorityQueue()
    icatmsg_server_app.outbound_retry_history.clear()
    icatmsg_server_app.outbound_dead_letters.clear()
    try:
        item = icatmsg_server_app._build_retry_item(
            body={
                "channel": "feishu",
                "account_id": "u1",
                "chat_id": "oc_1",
                "bot_id": "bot_A",
                "content": "hello",
            },
            header={"msg_id": "m_retry_ok"},
            metadata={},
            event_type="message.reply",
            attempt=1,
        )
        with patch.object(
            icatmsg_server_app,
            "_deliver_feishu_message",
            new=AsyncMock(side_effect=[RuntimeError("network timeout"), None]),
        ):
            await icatmsg_server_app._process_retry_item(item)
            assert icatmsg_server_app.outbound_retry_queue.qsize() == 1
            _, _, _, retry_item = icatmsg_server_app.outbound_retry_queue.get_nowait()
            await icatmsg_server_app._process_retry_item(retry_item)

        statuses = [entry.get("status") for entry in icatmsg_server_app.outbound_retry_history]
        assert "retry_scheduled" in statuses
        assert "delivered" in statuses
        assert icatmsg_server_app.outbound_dead_letters == []
    finally:
        icatmsg_server_app.outbound_retry_queue = original_queue


@pytest.mark.asyncio
async def test_outbound_retry_moves_to_dead_letter_after_max_attempts() -> None:
    original_queue = icatmsg_server_app.outbound_retry_queue
    icatmsg_server_app.outbound_retry_queue = asyncio.PriorityQueue()
    icatmsg_server_app.outbound_retry_history.clear()
    icatmsg_server_app.outbound_dead_letters.clear()
    try:
        item = icatmsg_server_app._build_retry_item(
            body={
                "channel": "feishu",
                "account_id": "u1",
                "chat_id": "oc_1",
                "bot_id": "bot_A",
                "content": "hello",
            },
            header={"msg_id": "m_dead_letter"},
            metadata={},
            event_type="message.reply",
            attempt=icatmsg_server_app.OUTBOUND_RETRY_MAX_ATTEMPTS,
        )
        with patch.object(
            icatmsg_server_app,
            "_deliver_feishu_message",
            new=AsyncMock(side_effect=RuntimeError("hard failure")),
        ):
            await icatmsg_server_app._process_retry_item(item)

        assert icatmsg_server_app.outbound_retry_queue.qsize() == 0
        assert len(icatmsg_server_app.outbound_dead_letters) == 1
        dead_letter = icatmsg_server_app.outbound_dead_letters[0]
        assert dead_letter["msg_id"] == "m_dead_letter"
        assert dead_letter["status"] == "dead_letter"
        assert dead_letter["attempts"] == icatmsg_server_app.OUTBOUND_RETRY_MAX_ATTEMPTS
    finally:
        icatmsg_server_app.outbound_retry_queue = original_queue


def test_verify_login_credentials_supports_user_map_and_shared_password() -> None:
    original_users = icatmsg_server_app.AUTH_USERS
    original_shared_password = icatmsg_server_app.AUTH_SHARED_PASSWORD
    try:
        icatmsg_server_app.AUTH_USERS = {"alice": "wonderland"}
        icatmsg_server_app.AUTH_SHARED_PASSWORD = "shared-secret"

        assert icatmsg_server_app._verify_login_credentials("alice", "wonderland") is True
        assert icatmsg_server_app._verify_login_credentials("bob", "shared-secret") is True
        assert icatmsg_server_app._verify_login_credentials("alice", "wrong") is False
    finally:
        icatmsg_server_app.AUTH_USERS = original_users
        icatmsg_server_app.AUTH_SHARED_PASSWORD = original_shared_password


@pytest.mark.asyncio
async def test_receive_messages_merges_fallback_client_queues() -> None:
    original_redis_client = icatmsg_server_app.redis_client
    original_bot_responses = dict(icatmsg_server_app.bot_responses)
    original_account_clients = {k: set(v) for k, v in icatmsg_server_app.account_clients.items()}
    try:
        icatmsg_server_app.redis_client = None
        icatmsg_server_app.bot_responses.clear()
        icatmsg_server_app.account_clients.clear()
        icatmsg_server_app.bot_responses[("u1", "c1", "u1")] = [{"content": "default"}]
        icatmsg_server_app.bot_responses[("u1", "c1", "web")] = [{"content": "web"}]

        messages = await icatmsg_server_app._pop_pending_messages("u1", "c1", "cli")

        assert [message["content"] for message in messages] == ["default", "web"]
        assert icatmsg_server_app.bot_responses[("u1", "c1", "cli")] == []
        assert icatmsg_server_app.bot_responses[("u1", "c1", "u1")] == []
        assert icatmsg_server_app.bot_responses[("u1", "c1", "web")] == []
    finally:
        icatmsg_server_app.redis_client = original_redis_client
        icatmsg_server_app.bot_responses.clear()
        icatmsg_server_app.bot_responses.update(original_bot_responses)
        icatmsg_server_app.account_clients.clear()
        icatmsg_server_app.account_clients.update(original_account_clients)


@pytest.mark.asyncio
async def test_receive_messages_prefers_direct_client_queue_over_fallback() -> None:
    original_redis_client = icatmsg_server_app.redis_client
    original_bot_responses = dict(icatmsg_server_app.bot_responses)
    original_account_clients = {k: set(v) for k, v in icatmsg_server_app.account_clients.items()}
    try:
        icatmsg_server_app.redis_client = None
        icatmsg_server_app.bot_responses.clear()
        icatmsg_server_app.account_clients.clear()
        icatmsg_server_app.bot_responses[("u1", "c1", "cli")] = [{"content": "direct"}]
        icatmsg_server_app.bot_responses[("u1", "c1", "u1")] = [{"content": "default"}]
        icatmsg_server_app.bot_responses[("u1", "c1", "web")] = [{"content": "web"}]

        messages = await icatmsg_server_app._pop_pending_messages("u1", "c1", "cli")

        assert [message["content"] for message in messages] == ["direct"]
        assert icatmsg_server_app.bot_responses[("u1", "c1", "cli")] == []
        assert icatmsg_server_app.bot_responses[("u1", "c1", "u1")] == [{"content": "default"}]
        assert icatmsg_server_app.bot_responses[("u1", "c1", "web")] == [{"content": "web"}]
    finally:
        icatmsg_server_app.redis_client = original_redis_client
        icatmsg_server_app.bot_responses.clear()
        icatmsg_server_app.bot_responses.update(original_bot_responses)
        icatmsg_server_app.account_clients.clear()
        icatmsg_server_app.account_clients.update(original_account_clients)


@pytest.mark.asyncio
async def test_receive_messages_dedups_and_sorts_across_client_queues() -> None:
    original_redis_client = icatmsg_server_app.redis_client
    original_bot_responses = dict(icatmsg_server_app.bot_responses)
    original_account_clients = {k: set(v) for k, v in icatmsg_server_app.account_clients.items()}
    try:
        icatmsg_server_app.redis_client = None
        icatmsg_server_app.bot_responses.clear()
        icatmsg_server_app.account_clients.clear()

        duplicate_reply = {
            "type": "text",
            "request_msg_id": "u-1",
            "content": "同一条回复",
            "timestamp": 20.0,
        }
        icatmsg_server_app.bot_responses[("u1", "c1", "u1")] = [duplicate_reply]
        icatmsg_server_app.bot_responses[("u1", "c1", "web")] = [dict(duplicate_reply)]
        icatmsg_server_app.bot_responses[("u1", "c1", "cli")] = [
            {"type": "status", "status": "processing", "request_msg_id": "u-1", "progress_stage": "queued", "progress_percent": 5, "progress_message": "任务已进入队列", "timestamp": 10.0},
            {"type": "status", "status": "processing", "request_msg_id": "u-1", "progress_stage": "finalizing", "progress_percent": 95, "progress_message": "正在整理最终结果", "timestamp": 30.0},
        ]

        messages = await icatmsg_server_app._pop_pending_messages("u1", "c1", "cli")

        assert [m.get("timestamp") for m in messages] == [10.0, 20.0, 30.0]
        assert [m.get("content") for m in messages if m.get("content")] == ["同一条回复"]
    finally:
        icatmsg_server_app.redis_client = original_redis_client
        icatmsg_server_app.bot_responses.clear()
        icatmsg_server_app.bot_responses.update(original_bot_responses)
        icatmsg_server_app.account_clients.clear()
        icatmsg_server_app.account_clients.update(original_account_clients)


@pytest.mark.asyncio
async def test_handle_outbound_event_uses_metadata_request_msg_id_for_reply_dedup() -> None:
    body = {
        "account_id": "u1",
        "chat_id": "c1",
        "content": "比较结果",
        "content_type": "text",
        "attachments": [],
    }
    header = {"version": "v1.2", "event": "message.reply"}
    metadata = {"request_msg_id": "u-req-42"}

    with patch.object(icatmsg_server_app, "_mark_delivery", return_value=True) as mock_mark, \
         patch.object(icatmsg_server_app, "_broadcast_to_clients") as mock_broadcast:
        await icatmsg_server_app._handle_outbound_event(body, header, metadata, "message.reply")

    assert mock_mark.call_count == 1
    args = mock_mark.call_args.args
    assert args[0] == "u1"
    assert args[1] == "c1"
    assert args[2] == "reply"
    assert args[3] == "u-req-42"
    payload = mock_broadcast.call_args.args[2]
    assert payload["request_msg_id"] == "u-req-42"


@pytest.mark.asyncio
async def test_handle_outbound_event_skips_feishu_progress_delivery() -> None:
    body = {
        "channel": "feishu",
        "account_id": "u1",
        "chat_id": "oc_1",
        "content": "正在意图解析",
        "status": {"phase": "processing", "request_msg_id": "u-req-10"},
    }
    header = {"version": "v1.2", "event": "status.processing"}
    metadata = {}
    with patch.object(icatmsg_server_app, "_process_retry_item", new=AsyncMock()) as mock_retry:
        await icatmsg_server_app._handle_outbound_event(body, header, metadata, "status.processing")
    mock_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_outbound_event_uses_reply_to_when_request_msg_id_missing() -> None:
    body = {
        "account_id": "u1",
        "chat_id": "c1",
        "content": "已收到",
        "content_type": "text",
        "reply_to": "u-req-99",
        "attachments": [],
    }
    header = {"version": "v1.2", "event": "message.reply"}
    metadata = {}

    with patch.object(icatmsg_server_app, "_mark_delivery", return_value=True) as mock_mark, \
         patch.object(icatmsg_server_app, "_broadcast_to_clients") as mock_broadcast:
        await icatmsg_server_app._handle_outbound_event(body, header, metadata, "message.reply")

    args = mock_mark.call_args.args
    assert args[3] == "u-req-99"
    payload = mock_broadcast.call_args.args[2]
    assert payload["request_msg_id"] == "u-req-99"
    assert payload["reply_to"] == "u-req-99"


@pytest.mark.asyncio
async def test_handle_outbound_event_broadcasts_interaction() -> None:
    body = {
        "account_id": "u1",
        "chat_id": "c1",
        "content": "请选择环境",
        "content_type": "text",
        "reply_to": "u-req-11",
        "interaction": {"type": "select", "title": "环境", "options": [{"label": "prod", "value": "prod"}]},
        "attachments": [],
    }
    header = {"version": "v1.2", "event": "message.interaction"}
    metadata = {}

    with patch.object(icatmsg_server_app, "_mark_delivery", return_value=True) as mock_mark, \
         patch.object(icatmsg_server_app, "_broadcast_to_clients") as mock_broadcast:
        await icatmsg_server_app._handle_outbound_event(body, header, metadata, "message.interaction")

    args = mock_mark.call_args.args
    assert args[2] == "interaction"
    payload = mock_broadcast.call_args.args[2]
    assert payload["type"] == "interaction"
    assert payload["request_msg_id"] == "u-req-11"
    assert payload["interaction"]["type"] == "select"


@pytest.mark.asyncio
async def test_icatmsg_interaction_reply_reaches_target_web_client(mock_bus, channel_config) -> None:
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)
    mock_producer = MagicMock()
    mock_producer.send_and_wait = AsyncMock()
    channel.producer = mock_producer

    original_redis_client = icatmsg_server_app.redis_client
    original_bot_responses = dict(icatmsg_server_app.bot_responses)
    original_account_clients = {k: set(v) for k, v in icatmsg_server_app.account_clients.items()}
    try:
        icatmsg_server_app.redis_client = None
        icatmsg_server_app.bot_responses.clear()
        icatmsg_server_app.account_clients.clear()

        outbound = OutboundMessage(
            channel="icatmsg",
            chat_id="c1",
            content="摘要：存在差异，请输入验证码下载完整报告。",
            account_id="u1",
            tenant_id="t1",
            reply_to="u-req-123",
            metadata={
                "client_id": "web_abc",
                "interaction": {
                    "type": "otp",
                    "interaction_id": "doc-compare-1",
                    "title": "下载对比结果需要验证码",
                    "fields": [{"key": "otp_code", "label": "验证码", "input_type": "otp", "required": True}],
                },
            },
        )

        await channel.send(outbound)

        _, kwargs = mock_producer.send_and_wait.call_args
        kafka_payload = json.loads(kwargs["value"].decode("utf-8"))
        await icatmsg_server_app._handle_outbound_event(
            kafka_payload["payload"],
            kafka_payload["header"],
            kafka_payload["metadata"],
            kafka_payload["header"]["event"],
        )

        messages = await icatmsg_server_app._pop_pending_messages("u1", "c1", "web_abc")

        assert len(messages) == 1
        assert messages[0]["type"] == "interaction"
        assert messages[0]["content"] == "摘要：存在差异，请输入验证码下载完整报告。"
        assert messages[0]["request_msg_id"] == "u-req-123"
        assert messages[0]["interaction"]["type"] == "otp"
    finally:
        icatmsg_server_app.redis_client = original_redis_client
        icatmsg_server_app.bot_responses.clear()
        icatmsg_server_app.bot_responses.update(original_bot_responses)
        icatmsg_server_app.account_clients.clear()
        icatmsg_server_app.account_clients.update(original_account_clients)


@pytest.mark.asyncio
async def test_handle_outbound_event_treats_invalid_feishu_chat_id_as_icatmsg() -> None:
    body = {
        "channel": "feishu",
        "account_id": "u1",
        "chat_id": "bot_doc_compare",
        "content": "[otp] 下载对比结果需要验证码",
        "content_type": "text",
        "reply_to": "u-req-200",
        "attachments": [],
    }
    header = {"version": "v1.2", "event": "message.reply"}
    metadata = {}

    with patch.object(icatmsg_server_app, "_process_retry_item", new=AsyncMock()) as mock_retry, \
         patch.object(icatmsg_server_app, "_mark_delivery", return_value=True) as mock_mark, \
         patch.object(icatmsg_server_app, "_broadcast_to_clients") as mock_broadcast:
        await icatmsg_server_app._handle_outbound_event(body, header, metadata, "message.reply")

    mock_retry.assert_not_awaited()
    assert mock_mark.call_args.args[1] == "bot_doc_compare"
    assert mock_broadcast.call_args.args[1] == "bot_doc_compare"


@pytest.mark.asyncio
async def test_handle_outbound_event_uses_bot_id_as_chat_id_for_web_when_missing() -> None:
    body = {
        "account_id": "u1",
        "chat_id": None,
        "bot_id": "bot_doc_compare",
        "client_id": "web_abc",
        "content": "比较完成",
        "content_type": "text",
        "attachments": [],
    }
    header = {"version": "v1.2", "event": "message.reply"}
    metadata = {"request_msg_id": "u-req-100"}

    with patch.object(icatmsg_server_app, "_mark_delivery", return_value=True) as mock_mark, \
         patch.object(icatmsg_server_app, "_broadcast_to_clients") as mock_broadcast:
        await icatmsg_server_app._handle_outbound_event(body, header, metadata, "message.reply")

    assert mock_mark.call_args.args[1] == "bot_doc_compare"
    assert mock_broadcast.call_args.args[1] == "bot_doc_compare"


@pytest.mark.asyncio
async def test_handle_outbound_event_uses_bot_id_as_chat_id_for_web_when_default_chat() -> None:
    body = {
        "account_id": "u1",
        "chat_id": "default_chat",
        "bot_id": "bot_doc_compare",
        "client_id": "web_abc",
        "content": "比较完成",
        "content_type": "text",
        "attachments": [],
    }
    header = {"version": "v1.2", "event": "message.reply"}
    metadata = {"request_msg_id": "u-req-100"}

    with patch.object(icatmsg_server_app, "_mark_delivery", return_value=True) as mock_mark, \
         patch.object(icatmsg_server_app, "_broadcast_to_clients") as mock_broadcast:
        await icatmsg_server_app._handle_outbound_event(body, header, metadata, "message.reply")

    assert mock_mark.call_args.args[1] == "bot_doc_compare"
    assert mock_broadcast.call_args.args[1] == "bot_doc_compare"


@pytest.mark.asyncio
async def test_handle_outbound_event_defaults_chat_id_when_missing_without_web_context() -> None:
    body = {
        "account_id": "u1",
        "chat_id": None,
        "bot_id": "bot_doc_compare",
        "content": "比较完成",
        "content_type": "text",
        "attachments": [],
    }
    header = {"version": "v1.2", "event": "message.reply"}
    metadata = {"request_msg_id": "u-req-100"}

    with patch.object(icatmsg_server_app, "_mark_delivery", return_value=True) as mock_mark, \
         patch.object(icatmsg_server_app, "_broadcast_to_clients") as mock_broadcast:
        await icatmsg_server_app._handle_outbound_event(body, header, metadata, "message.reply")

    assert mock_mark.call_args.args[1] == "default_chat"
    assert mock_broadcast.call_args.args[1] == "default_chat"


@pytest.mark.asyncio
async def test_handle_outbound_event_default_chat_still_reaches_web_poll_queue() -> None:
    original_redis_client = icatmsg_server_app.redis_client
    original_bot_responses = dict(icatmsg_server_app.bot_responses)
    original_account_clients = {k: set(v) for k, v in icatmsg_server_app.account_clients.items()}
    original_recent_deliveries = dict(icatmsg_server_app.recent_deliveries)
    try:
        icatmsg_server_app.redis_client = None
        icatmsg_server_app.bot_responses.clear()
        icatmsg_server_app.account_clients.clear()
        icatmsg_server_app.recent_deliveries.clear()

        body = {
            "account_id": "u1",
            "chat_id": "default_chat",
            "bot_id": "bot_doc_compare",
            "client_id": "web_abc",
            "content": "比较完成",
            "content_type": "text",
            "attachments": [],
        }
        header = {"version": "v1.2", "event": "message.reply"}
        metadata = {"request_msg_id": "u-req-101", "client_id": "web_abc"}

        await icatmsg_server_app._handle_outbound_event(body, header, metadata, "message.reply")

        messages = await icatmsg_server_app._pop_pending_messages("u1", "bot_doc_compare", "web_abc")

        assert len(messages) == 1
        assert messages[0]["content"] == "比较完成"
        assert messages[0]["request_msg_id"] == "u-req-101"
    finally:
        icatmsg_server_app.redis_client = original_redis_client
        icatmsg_server_app.bot_responses.clear()
        icatmsg_server_app.bot_responses.update(original_bot_responses)
        icatmsg_server_app.account_clients.clear()
        icatmsg_server_app.account_clients.update(original_account_clients)
        icatmsg_server_app.recent_deliveries.clear()
        icatmsg_server_app.recent_deliveries.update(original_recent_deliveries)


@pytest.mark.asyncio
async def test_handle_outbound_event_missing_chat_id_still_reaches_web_poll_queue() -> None:
    original_redis_client = icatmsg_server_app.redis_client
    original_bot_responses = dict(icatmsg_server_app.bot_responses)
    original_account_clients = {k: set(v) for k, v in icatmsg_server_app.account_clients.items()}
    original_recent_deliveries = dict(icatmsg_server_app.recent_deliveries)
    try:
        icatmsg_server_app.redis_client = None
        icatmsg_server_app.bot_responses.clear()
        icatmsg_server_app.account_clients.clear()
        icatmsg_server_app.recent_deliveries.clear()

        body = {
            "account_id": "u1",
            "chat_id": None,
            "bot_id": "bot_doc_compare",
            "client_id": "web_abc",
            "content": "比较完成",
            "content_type": "text",
            "attachments": [],
        }
        header = {"version": "v1.2", "event": "message.reply"}
        metadata = {"request_msg_id": "u-req-102", "client_id": "web_abc"}

        await icatmsg_server_app._handle_outbound_event(body, header, metadata, "message.reply")

        messages = await icatmsg_server_app._pop_pending_messages("u1", "bot_doc_compare", "web_abc")

        assert len(messages) == 1
        assert messages[0]["content"] == "比较完成"
        assert messages[0]["request_msg_id"] == "u-req-102"
    finally:
        icatmsg_server_app.redis_client = original_redis_client
        icatmsg_server_app.bot_responses.clear()
        icatmsg_server_app.bot_responses.update(original_bot_responses)
        icatmsg_server_app.account_clients.clear()
        icatmsg_server_app.account_clients.update(original_account_clients)
        icatmsg_server_app.recent_deliveries.clear()
        icatmsg_server_app.recent_deliveries.update(original_recent_deliveries)


@pytest.mark.asyncio
async def test_receive_messages_keeps_latest_reply_per_request() -> None:
    original_redis_client = icatmsg_server_app.redis_client
    original_bot_responses = dict(icatmsg_server_app.bot_responses)
    original_account_clients = {k: set(v) for k, v in icatmsg_server_app.account_clients.items()}
    try:
        icatmsg_server_app.redis_client = None
        icatmsg_server_app.bot_responses.clear()
        icatmsg_server_app.account_clients.clear()
        icatmsg_server_app.bot_responses[("u1", "c1", "cli")] = [
            {"type": "text", "request_msg_id": "req-1", "content": "旧版结果", "timestamp": 10.0},
            {"type": "text", "request_msg_id": "req-1", "content": "最新版结果", "timestamp": 20.0},
        ]

        messages = await icatmsg_server_app._pop_pending_messages("u1", "c1", "cli")

        assert len(messages) == 1
        assert messages[0]["content"] == "最新版结果"
    finally:
        icatmsg_server_app.redis_client = original_redis_client
        icatmsg_server_app.bot_responses.clear()
        icatmsg_server_app.bot_responses.update(original_bot_responses)
        icatmsg_server_app.account_clients.clear()
        icatmsg_server_app.account_clients.update(original_account_clients)


def test_delivery_key_digest_is_stable_across_calls() -> None:
    digest_a = icatmsg_server_app._delivery_key_digest("processing:parsing:10:分析中")
    digest_b = icatmsg_server_app._delivery_key_digest("processing:parsing:10:分析中")
    digest_c = icatmsg_server_app._delivery_key_digest("processing:comparing:40:读取附件")

    assert digest_a == digest_b
    assert digest_a != digest_c
    assert len(digest_a) == 64
