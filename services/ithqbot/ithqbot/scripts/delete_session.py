import os
import sys

import redis


def main():
    if len(sys.argv) < 2:
        raise SystemExit("用法: python3 delete_session.py <redis_key>")
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    password = os.getenv("REDIS_PASSWORD") or None
    db = int(os.getenv("REDIS_DB", "0"))
    client = redis.Redis(host=host, port=port, password=password, db=db)
    deleted = client.delete(sys.argv[1])
    print(f"Deleted: {deleted}")


if __name__ == "__main__":
    main()
