import asyncio
import json
import httpx
import time
import uuid
import os
from aiokafka import AIOKafkaProducer

API_BASE = "http://localhost:8000"
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_SERVERS", "localhost:9092")
OUTBOUND_TOPIC = "icatmsg_outbound"

async def verify_flow():
    account_id = f"test_user_{uuid.uuid4().hex[:6]}"
    chat_id = "test_chat"
    client_id = "test_script"
    
    print(f"--- Starting Final Verification for Account: {account_id} ---")
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Upload a file
        print("1. Uploading test file...")
        content = b"Hello Minio! This is a test file."
        files = {"file": ("test.txt", content, "text/plain")}
        resp = await client.post(f"{API_BASE}/upload?account_id={account_id}&chat_id={chat_id}", files=files)
        assert resp.status_code == 200, f"Upload failed: {resp.text}"
        file_meta = resp.json()
        print(f"   Success: rel_path={file_meta['rel_path']}")
        
        # 2. Send message with file_meta
        print("2. Sending message with file metadata...")
        msg_data = {
            "account_id": account_id,
            "chat_id": chat_id,
            "content": "Please check this file",
            "content_type": "file",
            "file_meta": file_meta
        }
        resp = await client.post(f"{API_BASE}/send", json=msg_data)
        assert resp.status_code == 200
        print("   Success: Message sent to Kafka")

        # 2b. Register client by polling once
        print("2b. Registering client...")
        await client.get(f"{API_BASE}/receive/{account_id}?client_id={client_id}")

        # 3. Simulate Bot Response with NEW file
        # We need to push directly to icatmsg_outbound to simulate what the gateway does
        print("3. Simulating Bot Response (pushing to Kafka)...")
        bot_file_id = str(uuid.uuid4())
        bot_rel_path = f"{account_id}/{chat_id}/{bot_file_id}.txt"
        bot_content = b"Processed content by bot"
        
        # Manually put the "bot's file" into Minio first (simulating MinioPushTool)
        from minio import Minio
        minio_endpoint = os.getenv("MINIO_ENDPOINT", "localhost:9000")
        minio_access_key = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
        minio_secret_key = os.getenv("MINIO_SECRET_KEY", "minioadmin")
        minio_secure = os.getenv("MINIO_SECURE", "").strip().lower() in {"1", "true", "yes"}
        mc = Minio(
            minio_endpoint,
            access_key=minio_access_key,
            secret_key=minio_secret_key,
            secure=minio_secure,
        )
        import io
        mc.put_object("ithqbot-storage", bot_rel_path, io.BytesIO(bot_content), len(bot_content))
        
        bot_payload = {
            "account_id": account_id,
            "chat_id": chat_id,
            "content": f"I processed your file. Here is the result: \n```json\n{{\"file_meta\": {{\"rel_path\": \"{bot_rel_path}\", \"name\": \"bot_result.txt\", \"size\": {len(bot_content)}, \"mime\": \"text/plain\"}}}}\n```",
            "metadata": {}
        }
        
        producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
        await producer.start()
        try:
            await producer.send_and_wait(OUTBOUND_TOPIC, json.dumps(bot_payload).encode("utf-8"))
        finally:
            await producer.stop()
        print("   Success: Bot response pushed")

        # 4. Poll and verify
        print("4. Polling for responses...")
        # Wait for consumer to process
        time.sleep(2)
        resp = await client.get(f"{API_BASE}/receive/{account_id}?client_id={client_id}")
        assert resp.status_code == 200
        msgs = resp.json()["messages"]
        assert len(msgs) > 0, "No messages received"
        
        file_msg = next((m for m in msgs if m.get("type") == "file"), None)
        assert file_msg is not None, "Bot response was not identified as a file type"
        assert file_msg["file_meta"]["rel_path"] == bot_rel_path
        print(f"   Success: Received file message with path: {file_msg['file_meta']['rel_path']}")
        
        # 5. Download bot's file
        print("5. Verifying download endpoint...")
        dl_resp = await client.get(f"{API_BASE}/download?path={bot_rel_path}")
        assert dl_resp.status_code == 200
        assert dl_resp.content == bot_content
        print("   Success: Download content matches!")

    print("\n[VERIFICATION PASSED] Bi-directional file transfer is working correctly.")

if __name__ == "__main__":
    asyncio.run(verify_flow())
