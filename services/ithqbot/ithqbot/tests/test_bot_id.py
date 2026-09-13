import asyncio
import json
import httpx
import os
import uuid
import pytest
from aiokafka import AIOKafkaConsumer

API_BASE = "http://localhost:8000"
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_SERVERS", "localhost:9092")
INBOUND_TOPIC = "icatmsg_inbound"
pytestmark = pytest.mark.skipif(
    os.getenv("ITHQBOT_RUN_INTEGRATION_TESTS") != "1",
    reason="requires running gateway and Kafka; set ITHQBOT_RUN_INTEGRATION_TESTS=1",
)

async def test_bot_id():
    account_id = f"test_user_{uuid.uuid4().hex[:6]}"
    chat_id = "test_chat"
    
    print(f"--- Starting Bot ID Verification ---")
    
    # 1. Setup a consumer to simulate a bot with bot_id="excel_bot"
    consumer = AIOKafkaConsumer(
        INBOUND_TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        auto_offset_reset="latest",
        group_id=f"test-group-{uuid.uuid4().hex[:6]}"
    )
    await consumer.start()
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # 2. Send message with bot_id="excel_bot"
            print("2. Sending message for 'excel_bot'...")
            msg_excel = {
                "account_id": account_id,
                "chat_id": chat_id,
                "content": "Analyze this excel",
                "bot_id": "excel_bot"
            }
            resp = await client.post(f"{API_BASE}/send", json=msg_excel)
            assert resp.status_code == 200
            
            # 3. Verify consumer receives it and meta matches
            print("3. Checking if bot receives it...")
            async for msg in consumer:
                payload = json.loads(msg.value.decode("utf-8"))
                body = payload["payload"]
                print(f"   Received body: {body}")
                if body.get("account_id") == account_id:
                    assert body.get("bot_id") == "excel_bot"
                    print(f"   Success: Received message for bot_id: {body.get('bot_id')}")
                    break
            
            # 4. Send message for "other_bot"
            print("4. Sending message for 'other_bot'...")
            account_id_other = f"test_user_other_{uuid.uuid4().hex[:6]}"
            msg_other = {
                "account_id": account_id_other,
                "chat_id": chat_id,
                "content": "Do something else",
                "bot_id": "other_bot"
            }
            await client.post(f"{API_BASE}/send", json=msg_other)
            
            # In a real scenario, the "excel_bot" instance would filter this out.
            # Here we just verify the bot_id is correctly passed in Kafka.
            print("5. Verifying 'other_bot' message in Kafka...")
            async for msg in consumer:
                payload = json.loads(msg.value.decode("utf-8"))
                body = payload["payload"]
                if body.get("account_id") == account_id_other:
                    assert body.get("bot_id") == "other_bot"
                    print(f"   Success: Kafka contains bot_id: {body.get('bot_id')}")
                    break

    finally:
        await consumer.stop()

    print("\n[VERIFICATION PASSED] bot_id is correctly propagated through Kafka.")

if __name__ == "__main__":
    asyncio.run(test_bot_id())
