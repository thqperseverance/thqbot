import asyncio
from aiokafka import AIOKafkaProducer
import json
import uuid

async def send_mock_message():
    producer = AIOKafkaProducer(
        bootstrap_servers='localhost:9092',
        value_serializer=lambda v: json.dumps(v).encode('utf-8')
    )
    await producer.start()
    try:
        msg_id = f"test_msg_{uuid.uuid4().hex[:8]}"
        payload = {
            "channel": "feishu",
            "account_id": "test_user",
            "chat_id": "test_chat",
            "content": "你是谁",
            "bot_id": "bot_A",
            "media": [],
            "attachments": []
        }
        await producer.send_and_wait("icatmsg_inbound", value=payload)
        print(f"Mock message {msg_id} sent")
    finally:
        await producer.stop()

if __name__ == "__main__":
    asyncio.run(send_mock_message())
