import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ithqbot.bus.events import OutboundMessage
from ithqbot.bus.queue import MessageBus
from ithqbot.channels.icatmsg import ICatMsgChannel


class MockConfig:
    def __init__(self, data):
        for key, value in data.items():
            setattr(self, key, value)


@pytest.fixture
def mock_bus():
    bus = MagicMock(spec=MessageBus)
    bus.publish_inbound = AsyncMock()
    return bus


@pytest.fixture
def channel_config():
    return MockConfig(
        {
            "enabled": True,
            "kafka_servers": "test:9092",
            "inbound_topic": "test_inbound",
            "outbound_topic": "test_outbound",
            "allow_from": ["*"],
        }
    )


def test_normalize_attachments_builds_storage_uri_for_minio(mock_bus, channel_config) -> None:
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)

    normalized = channel._normalize_attachments(["minio://ithqbot-storage/u1/c1/a.txt"])

    assert normalized[0]["storage_uri"] == "minio://ithqbot-storage/u1/c1/a.txt"
    assert normalized[0]["minio_uri"] == normalized[0]["storage_uri"]
    assert normalized[0]["storage"]["backend"] == "minio"
    assert normalized[0]["storage"]["bucket"] == "ithqbot-storage"
    assert normalized[0]["storage"]["path"] == "u1/c1/a.txt"


def test_normalize_attachments_accepts_s3_storage_uri_object(mock_bus, channel_config) -> None:
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)

    normalized = channel._normalize_attachments(
        [{"kind": "file", "name": "a.txt", "storage_uri": "s3://bucket-a/u1/c1/a.txt"}]
    )

    assert normalized[0]["storage_uri"] == "s3://bucket-a/u1/c1/a.txt"
    assert normalized[0]["s3_uri"] == normalized[0]["storage_uri"]
    assert "minio_uri" not in normalized[0]
    assert normalized[0]["storage"]["backend"] == "s3"
    assert normalized[0]["storage"]["bucket"] == "bucket-a"
    assert normalized[0]["storage"]["path"] == "u1/c1/a.txt"


@pytest.mark.asyncio
async def test_send_uses_storage_uri_without_hardcoded_minio(mock_bus, channel_config) -> None:
    channel = ICatMsgChannel(config=channel_config, bus=mock_bus)

    with patch("ithqbot.channels.icatmsg.AIOKafkaProducer") as mock_producer_cls, patch(
        "ithqbot.channels.icatmsg.AIOKafkaConsumer"
    ) as mock_consumer_cls:
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
        assert attachment["storage_uri"] == "s3://bucket-a/user123/chat456/r1.xlsx"
        assert attachment["s3_uri"] == attachment["storage_uri"]
        assert "minio_uri" not in attachment
        assert attachment["storage"]["backend"] == "s3"
        assert attachment["storage"]["bucket"] == "bucket-a"
        assert attachment["storage"]["path"] == "user123/chat456/r1.xlsx"

        await channel.stop()
