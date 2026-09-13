import asyncio
import json
import httpx
import time
import uuid
import os
import pytest
from aiokafka import AIOKafkaProducer

API_BASE = "http://localhost:8000"
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_SERVERS", "localhost:9092")
OUTBOUND_TOPIC = "icatmsg_outbound"
pytestmark = pytest.mark.skipif(
    os.getenv("ITHQBOT_RUN_INTEGRATION_TESTS") != "1",
    reason="requires running gateway and Kafka; set ITHQBOT_RUN_INTEGRATION_TESTS=1",
)

async def test_flow():
    account_id = f"test_user_{uuid.uuid4().hex[:6]}"
    chat_a = "chat_alpha"
    chat_b = "chat_beta"
    client_id = "test_script"
    
    print(f"--- Starting Verification for Account: {account_id} ---")
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Upload two files to Chat A
        print("1. Uploading test files to Chat A...")
        f1_content = b"File 1 content"
        f2_content = b"File 2 content"
        
        files1 = {"file": ("f1.txt", f1_content, "text/plain")}
        resp1 = await client.post(f"{API_BASE}/upload?account_id={account_id}&chat_id={chat_a}", files=files1)
        assert resp1.status_code == 200
        path1 = resp1.json()["rel_path"]
        
        files2 = {"file": ("f2.txt", f2_content, "text/plain")}
        resp2 = await client.post(f"{API_BASE}/upload?account_id={account_id}&chat_id={chat_a}", files=files2)
        assert resp2.status_code == 200
        path2 = resp2.json()["rel_path"]
        
        print(f"   Success: Uploaded {path1} and {path2}")
        
        # 2. Send message with multiple attachments
        print("2. Sending message with multiple attachments...")
        msg_data = {
            "account_id": account_id,
            "chat_id": chat_a,
            "content": "Check these two files",
            "attachments": [path1, path2]
        }
        resp = await client.post(f"{API_BASE}/send", json=msg_data)
        assert resp.status_code == 200
        print("   Success: Message with attachments sent to Kafka")

        # 3. Test Session Isolation
        print("3. Testing Session Isolation...")
        # Register two sessions
        await client.get(f"{API_BASE}/receive/{account_id}?chat_id={chat_a}&client_id={client_id}")
        await client.get(f"{API_BASE}/receive/{account_id}?chat_id={chat_b}&client_id={client_id}")
        
        # Simulate Bot Response to Chat A
        print("   Simulating Bot Response to Chat A...")
        bot_payload = {
            "account_id": account_id,
            "chat_id": chat_a,
            "content": "I got your files!",
            "attachments": [path1]
        }
        
        producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
        await producer.start()
        try:
            await producer.send_and_wait(OUTBOUND_TOPIC, json.dumps(bot_payload).encode("utf-8"))
        finally:
            await producer.stop()
        
        # Poll Chat B (should be empty)
        print("   Polling Chat B (should be empty)...")
        time.sleep(1)
        resp_b = await client.get(f"{API_BASE}/receive/{account_id}?chat_id={chat_b}&client_id={client_id}")
        msgs_b = resp_b.json()["messages"]
        assert len(msgs_b) == 0, f"Chat B should be empty, but got: {msgs_b}"
        print("   Success: Chat B is empty")
        
        # Poll Chat A (should have the message)
        print("   Polling Chat A (should have message)...")
        resp_a = await client.get(f"{API_BASE}/receive/{account_id}?chat_id={chat_a}&client_id={client_id}")
        msgs_a = resp_a.json()["messages"]
        assert len(msgs_a) == 1, f"Chat A should have 1 message, but got: {msgs_a}"
        assert msgs_a[0]["content"] == "I got your files!"
        assert path1 in msgs_a[0]["attachments"]
        print("   Success: Chat A received the correct message and attachments")

    print("\n[VERIFICATION PASSED] Multi-attachment and Session (chat_id) isolation is working!")

if __name__ == "__main__":
    asyncio.run(test_flow())
