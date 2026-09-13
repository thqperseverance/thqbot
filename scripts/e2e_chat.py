"""端到端冒烟测试：登录 -> 取会话 -> 发消息 -> 等待 agent 回复。

覆盖完整链路：
    浏览器 API -> gateway(PG) -> Kafka inbound
    -> ithqbot AgentLoop（可能调用 skills）-> Kafka outbound
    -> gateway 出站消费 -> PG -> 轮询读回

用法::

    python scripts/e2e_chat.py
    python scripts/e2e_chat.py --message "统计这段话：你好世界，你好 thqbot" --timeout 180
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx


def _print_progress(meta: dict) -> None:
    progress = (meta or {}).get("progress") or {}
    if not progress:
        print("  progress: (无)")
        return
    print(
        "  progress: stage={stage} percent={percent} skills={skills} tools={tools}".format(
            stage=progress.get("stage"),
            percent=progress.get("percent"),
            skills=progress.get("skills"),
            tools=progress.get("tools"),
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="thqbot MVP end-to-end smoke test")
    parser.add_argument("--base", default="http://127.0.0.1:8090", help="gateway base url")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin123456")
    parser.add_argument("--message", default="你好，请用一句话介绍你自己")
    parser.add_argument(
        "--attach",
        action="append",
        default=[],
        help="先上传的本地文件路径（可重复传入，验证附件链路）",
    )
    parser.add_argument("--timeout", type=float, default=180.0, help="等待回复的秒数")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args(argv)

    # trust_env=False：本地回环不需要代理，同时避开 httpx 解析 NO_PROXY 中
    # "[::1]" 这类条目时报 InvalidURL 的问题。
    with httpx.Client(base_url=args.base, timeout=30.0, trust_env=False) as client:
        # 1) 健康
        ready = client.get("/ready")
        print(f"[0/5] /ready -> {ready.status_code} {ready.json().get('status')}")

        # 2) 登录
        response = client.post(
            "/api/auth/login", json={"username": args.username, "password": args.password}
        )
        if response.status_code != 200:
            print(f"[1/5] 登录失败: {response.status_code} {response.text}", file=sys.stderr)
            return 1
        user = response.json()["user"]
        print(f"[1/5] 登录成功: {user['display_name']} ({user['username']})")

        # 3) 会话上下文 + 会话列表
        context = client.get("/api/session/context").json()
        print(
            f"[2/5] 上下文: bot={context['bot_id']} tenant={context['tenant_id']} "
            f"未读={context['total_unread_count']}"
        )
        conversations = client.get("/api/conversations").json()
        if not conversations:
            print("[3/5] 没有会话（异常）", file=sys.stderr)
            return 1
        conversation = conversations[0]
        conversation_id = conversation["conversation_id"]
        print(f"[3/5] 会话: {conversation_id} 「{conversation['title']}」")

        before = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
        before_ids = {item["message_id"] for item in before}

        # 4) 上传附件（可选）+ 发消息
        file_ids: list[str] = []
        for raw_path in args.attach:
            path = Path(raw_path).expanduser()
            if not path.is_file():
                print(f"[4/5] 附件不存在：{path}", file=sys.stderr)
                return 1
            with path.open("rb") as handle:
                upload = client.post(
                    f"/api/conversations/{conversation_id}/files",
                    files={"file": (path.name, handle, "application/octet-stream")},
                )
            if upload.status_code != 201:
                print(
                    f"[4/5] 上传失败 {path.name}: HTTP {upload.status_code} {upload.text}",
                    file=sys.stderr,
                )
                return 1
            meta = upload.json()
            file_ids.append(meta["file_id"])
            print(
                f"[4/5] 已上传 {meta['name']} ({meta['size']} B) -> {meta['storage_uri']}"
            )

        response = client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"content": args.message, "file_ids": file_ids},
        )
        if response.status_code != 200:
            print(f"[4/5] 发送失败: {response.status_code} {response.text}", file=sys.stderr)
            return 1
        sent = response.json()
        request_id = (sent.get("message") or {}).get("request_msg_id")
        print(
            f"[4/5] 已发送 queued={sent['queued']} degraded={sent['degraded']} "
            f"attachments={len(file_ids)} request_msg_id={request_id}"
        )
        if sent.get("warning"):
            print(f"      warning: {sent['warning']}")

        # 5) 轮询等待 agent 回复
        deadline = time.time() + args.timeout
        reply = None
        while time.time() < deadline:
            messages = client.get(
                f"/api/conversations/{conversation_id}/messages", params={"limit": 50}
            ).json()["messages"]
            fresh = [
                item
                for item in messages
                if item["sender"] == "bot" and item["message_id"] not in before_ids
            ]
            if fresh:
                reply = fresh[-1]
                break
            time.sleep(args.poll_interval)

        if reply is None:
            print(f"[5/5] 超时（{args.timeout:.0f}s）未收到 agent 回复", file=sys.stderr)
            return 1

        print("[5/5] 收到 agent 回复:")
        print("  " + (reply["content"] or "").replace("\n", "\n  ")[:1200])
        _print_progress(reply.get("meta") or {})
        print(f"  status={reply['status']} created_at={reply['created_at']}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
