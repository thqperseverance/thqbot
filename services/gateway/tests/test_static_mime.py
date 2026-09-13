"""静态资源 MIME 回归测试。

背景：Windows 的 ``mimetypes`` 注册表不含 woff2，Starlette 的 StaticFiles 会把
字体发成 ``text/plain``；在严格 CSP 或 ``X-Content-Type-Options: nosniff`` 下
浏览器会拒绝加载字体。``app.main`` 导入时会显式注册这些类型。
"""

from __future__ import annotations

import mimetypes


def test_font_mime_types_registered():
    # 导入 app.main 触发注册
    import app.main  # noqa: F401

    assert mimetypes.guess_type("inter-latin-wght-normal.woff2")[0] == "font/woff2"
    assert mimetypes.guess_type("legacy.woff")[0] == "font/woff"
    assert mimetypes.guess_type("legacy.ttf")[0] == "font/ttf"


def test_manifest_mime_type_registered():
    import app.main  # noqa: F401

    assert mimetypes.guess_type("manifest.webmanifest")[0] == "application/manifest+json"


def test_static_files_uses_registered_type(client):
    """端到端：请求一个 woff2 时 Content-Type 必须是 font/woff2。"""
    import re
    from pathlib import Path

    page = client.get("/")
    if page.status_code != 200:
        return  # 未挂载前端产物（仅 API 模式）时跳过

    dist = Path(__file__).resolve().parent.parent / "web"
    fonts = list(dist.glob("assets/*.woff2")) if dist.exists() else []
    if not fonts:
        return  # 测试环境可能没有构建产物

    html = page.text
    match = re.search(r"/assets/([A-Za-z0-9._-]+\.woff2)", html)
    if not match:
        return

    response = client.get(f"/assets/{match.group(1)}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("font/woff2")
