"""单应用后端入口：路由装配、生命周期、静态前端托管。"""

from __future__ import annotations

import json
import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import object_store, orchestrator, realtime, repository
from .config import get_settings
from .db import get_session_factory
from .routes import auth, chat, files, health, stream
from .security import hash_password
from .source_adapter import start_outbound_consumer, stop_gateway_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("thqbot.gateway")

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123456"

# Windows 的 mimetypes 注册表缺少字体类型，会把 woff2 发成 text/plain；
# 在严格 CSP / X-Content-Type-Options: nosniff 下浏览器会拒绝加载字体。
# 这里显式注册，保证静态资源返回正确的 MIME。
for _suffix, _mime in (
    (".woff2", "font/woff2"),
    (".woff", "font/woff"),
    (".ttf", "font/ttf"),
    (".webmanifest", "application/manifest+json"),
):
    mimetypes.add_type(_mime, _suffix)


def seed_users() -> int:
    """按 ``APP_AUTH_USERS`` 种入用户；未配置则创建默认演示账号。"""
    settings = get_settings()
    specs: list[dict] = []
    raw = (settings.auth_users or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            logger.warning("APP_AUTH_USERS 不是合法 JSON，回退到默认账号")
            parsed = None
        if isinstance(parsed, list):
            specs = [item for item in parsed if isinstance(item, dict)]
    if not specs:
        specs = [
            {
                "username": DEFAULT_ADMIN_USERNAME,
                "password": DEFAULT_ADMIN_PASSWORD,
                "display_name": "管理员",
            }
        ]

    session = get_session_factory()()
    created = 0
    try:
        for spec in specs:
            username = str(spec.get("username") or "").strip()
            password = str(spec.get("password") or "")
            if not username or not password:
                continue
            if repository.get_user_by_username(session, username) is not None:
                continue
            repository.create_user(
                session,
                username=username,
                password_hash=hash_password(password),
                display_name=str(spec.get("display_name") or username),
            )
            created += 1
        session.commit()
    finally:
        session.close()
    return created


async def _outbound_handler(body, header, metadata, event_type) -> None:
    """Kafka 出站事件的适配器：每个事件一个 DB 会话。"""
    session = get_session_factory()()
    try:
        await orchestrator.handle_outbound_event(session, body, header, metadata, event_type)
    except Exception:  # noqa: BLE001 - 单条事件失败不应终止消费循环
        logger.exception("处理出站事件失败 event_type=%s", event_type)
        session.rollback()
    finally:
        session.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    created = seed_users()
    if created:
        logger.info("种入 %s 个用户", created)

    settings = get_settings()
    if object_store.is_configured():
        try:
            bucket = await object_store.ensure_bucket()
            logger.info("对象存储就绪：bucket=%s", bucket)
        except Exception:  # noqa: BLE001 - 附件不可用不应阻止服务启动
            logger.exception("对象存储初始化失败（附件功能将不可用）")
    else:
        logger.info("未配置对象存储（APP_MINIO_*），附件功能关闭")

    if settings.kafka_enabled:
        try:
            await start_outbound_consumer(_outbound_handler)
            logger.info(
                "出站消费者已启动 topic=%s group=%s",
                settings.kafka_outbound_topic,
                settings.kafka_outbound_group,
            )
        except Exception:  # noqa: BLE001
            logger.exception("启动出站消费者失败（服务仍可用，但收不到 agent 回复）")
    yield
    await stop_gateway_runtime()
    await realtime.reset_redis()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="thqbot MVP", version="0.1.0", lifespan=lifespan)

    # 同源部署时不需要 CORS；这里仅服务前端 dev server（Vite :5173）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(chat.router)
    app.include_router(files.router)
    app.include_router(stream.router)

    _mount_frontend(app, settings.web_dist_dir)
    return app


def _mount_frontend(app: FastAPI, dist_dir: str) -> None:
    default_dist = Path(__file__).resolve().parent.parent / "web"
    dist = Path(dist_dir) if dist_dir else default_dist
    if not (dist / "index.html").exists():
        logger.info("未找到前端产物（%s），仅提供 API", dist)
        return
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="web")
    logger.info("已挂载前端静态资源：%s", dist)


app = create_app()
