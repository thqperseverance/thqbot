"""口令哈希与会话令牌签发/校验。

- 口令：stdlib PBKDF2-HMAC-SHA256（无额外依赖，纯函数易测）。
- 会话：itsdangerous 的带时间戳签名令牌，写进 HttpOnly Cookie。

安全说明（对应 D16=A）：本模块是单应用唯一的身份来源。不存在"信任代理头"的降级
路径，因此旧网关那种"伪造 x-platform-user-id 即可冒充用户"的绕过在结构上不可能发生。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

PBKDF2_ALGORITHM = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 120_000


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    """返回 ``pbkdf2_sha256$<iterations>$<salt>$<hex-digest>``。"""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return f"{PBKDF2_ALGORITHM}${iterations}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str, *, max_iterations: int = 1_000_000) -> bool:
    """常量时间校验口令；任何格式异常都返回 False（不抛异常）。"""
    if not isinstance(password, str) or not isinstance(encoded, str):
        return False
    parts = encoded.split("$")
    if len(parts) != 4:
        return False
    algorithm, iterations_raw, salt, expected = parts
    if algorithm != PBKDF2_ALGORITHM:
        return False
    try:
        iterations = int(iterations_raw)
    except ValueError:
        return False
    if iterations <= 0 or iterations > max_iterations:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return hmac.compare_digest(digest.hex(), expected)


def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key=secret, salt="thqbot-session")


def sign_session_token(secret: str, user_id: str) -> str:
    if not user_id:
        raise ValueError("user_id is required")
    return _serializer(secret).dumps({"uid": user_id})


def load_session_token(secret: str, token: str, *, max_age: int) -> str | None:
    """校验令牌并返回 user_id；过期或伪造返回 None。"""
    if not token:
        return None
    try:
        payload = _serializer(secret).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict):
        return None
    user_id = payload.get("uid")
    return user_id if isinstance(user_id, str) and user_id else None
