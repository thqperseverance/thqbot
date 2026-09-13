"""附件链路的真实往返验证（不打桩，直连 gateway + MinIO）。

验证：
  1. 上传 -> 返回 storage_uri / size
  2. 下载 -> 字节与上传内容完全一致
  3. 越权（另一个会话 id 伪造不算，用未登录取）-> 401/404
  4. 删除 -> 再下载得到 404

用法::

    python scripts/verify_attachment.py
"""

from __future__ import annotations

import argparse
import sys

import httpx

PAYLOAD = "thqbot attachment round-trip ✅\n第二行内容\n".encode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="attachment round-trip check")
    parser.add_argument("--base", default="http://127.0.0.1:8090")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin123456")
    args = parser.parse_args(argv)

    with httpx.Client(base_url=args.base, timeout=30.0, trust_env=False) as client:
        login = client.post(
            "/api/auth/login", json={"username": args.username, "password": args.password}
        )
        if login.status_code != 200:
            print(f"登录失败：{login.status_code} {login.text}", file=sys.stderr)
            return 1
        conversation_id = client.get("/api/conversations").json()[0]["conversation_id"]

        # 1) 上传
        upload = client.post(
            f"/api/conversations/{conversation_id}/files",
            files={"file": ("round-trip.txt", PAYLOAD, "text/plain")},
        )
        if upload.status_code != 201:
            print(f"上传失败：{upload.status_code} {upload.text}", file=sys.stderr)
            return 1
        meta = upload.json()
        print(f"[1/4] 上传成功 file_id={meta['file_id']} size={meta['size']}")
        print(f"      storage_uri={meta['storage_uri']}")
        if meta["size"] != len(PAYLOAD):
            print("size 与上传内容不一致", file=sys.stderr)
            return 1

        # 2) 下载并比对字节
        download = client.get(f"/api/files/{meta['file_id']}")
        if download.status_code != 200:
            print(f"下载失败：{download.status_code} {download.text}", file=sys.stderr)
            return 1
        if download.content != PAYLOAD:
            print("下载内容与上传内容不一致", file=sys.stderr)
            return 1
        print(f"[2/4] 下载一致（{len(download.content)} 字节）content-type={download.headers.get('content-type')}")

        # 3) 未认证访问必须被拒
        with httpx.Client(base_url=args.base, timeout=15.0, trust_env=False) as anonymous:
            denied = anonymous.get(f"/api/files/{meta['file_id']}")
        if denied.status_code not in (401, 403, 404):
            print(f"未认证访问未被拒绝：{denied.status_code}", file=sys.stderr)
            return 1
        print(f"[3/4] 未认证访问被拒（HTTP {denied.status_code}）")

        # 4) 删除后应 404
        removed = client.delete(f"/api/files/{meta['file_id']}")
        if removed.status_code != 200:
            print(f"删除失败：{removed.status_code} {removed.text}", file=sys.stderr)
            return 1
        after = client.get(f"/api/files/{meta['file_id']}")
        if after.status_code != 404:
            print(f"删除后仍可下载：{after.status_code}", file=sys.stderr)
            return 1
        print(f"[4/4] 删除成功，对象已清理={removed.json().get('object_removed')}，再取为 404")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
