import json
import os
import sys

import redis


def main():
    if len(sys.argv) < 2:
        raise SystemExit("用法: python3 dump_redis.py <redis_key>")
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    password = os.getenv("REDIS_PASSWORD") or None
    db = int(os.getenv("REDIS_DB", "0"))
    client = redis.Redis(host=host, port=port, password=password, db=db)
    data = client.get(sys.argv[1])
    if not data:
        print("Key not found")
        return
    parsed = json.loads(data.decode("utf-8"))
    for index, msg in enumerate(parsed.get("messages", [])):
        print(f"{index}: {msg.get('role')} | tc: {'tool_calls' in msg}")


if __name__ == "__main__":
    main()
