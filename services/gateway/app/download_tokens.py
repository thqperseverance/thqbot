from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

_TOKEN_VERSION = "v1"
_NONCE_SIZE = 16
_SIG_SIZE = 32
_DEFAULT_DOWNLOAD_TOKEN_SECRET = "icatmsg-download-token-secret"


def _first_env(*keys: str, default: str = "") -> str:
    for key in keys:
        value = os.getenv(key, "").strip()
        if value:
            return value
    return default


def _download_token_ttl_seconds() -> int:
    raw = _first_env(
        "ICATMSG_DOWNLOAD_TOKEN_TTL_SECONDS",
        "DOWNLOAD_TOKEN_TTL_SECONDS",
        default="86400",
    )
    try:
        ttl = int(raw)
    except Exception:
        ttl = 86400
    return ttl if ttl > 0 else 86400


def _download_token_secret() -> str:
    return _first_env(
        "ICATMSG_DOWNLOAD_TOKEN_SECRET",
        "DOWNLOAD_TOKEN_SECRET",
        "ATOMIC_PLATFORM_JWT_SECRET",
        "ICATMSG_PLATFORM_JWT_SECRET",
        default=_DEFAULT_DOWNLOAD_TOKEN_SECRET,
    )


def _base64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _base64url_decode(raw: str) -> bytes:
    text = str(raw or "").strip()
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(f"{text}{padding}")


def _stream_xor(secret: bytes, nonce: bytes, data: bytes) -> bytes:
    output = bytearray(len(data))
    offset = 0
    counter = 0
    while offset < len(data):
        counter_bytes = counter.to_bytes(4, "big")
        block = hmac.new(secret, nonce + counter_bytes, hashlib.sha256).digest()
        end = min(offset + len(block), len(data))
        segment = data[offset:end]
        output[offset:end] = bytes(a ^ b for a, b in zip(segment, block[: len(segment)], strict=False))
        offset = end
        counter += 1
    return bytes(output)


def issue_download_token(path: str, *, ttl_seconds: int | None = None) -> str:
    normalized_path = str(path or "").strip()
    if not normalized_path:
        raise ValueError("path must not be empty")
    secret = _download_token_secret().encode("utf-8")
    now_s = int(time.time())
    ttl = ttl_seconds if isinstance(ttl_seconds, int) and ttl_seconds > 0 else _download_token_ttl_seconds()
    expires_at_s = now_s + ttl
    payload = {"v": _TOKEN_VERSION, "p": normalized_path, "iat": now_s, "exp": expires_at_s}
    plaintext = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    nonce = secrets.token_bytes(_NONCE_SIZE)
    ciphertext = _stream_xor(secret, nonce, plaintext)
    signed_body = nonce + ciphertext
    signature = hmac.new(secret, signed_body, hashlib.sha256).digest()
    return _base64url_encode(signed_body + signature)


def resolve_download_token(token: str) -> str | None:
    normalized_token = str(token or "").strip()
    if not normalized_token:
        return None
    secret = _download_token_secret().encode("utf-8")
    try:
        raw = _base64url_decode(normalized_token)
    except Exception:
        return None
    min_len = _NONCE_SIZE + _SIG_SIZE
    if len(raw) <= min_len:
        return None
    signed_body = raw[:-_SIG_SIZE]
    signature = raw[-_SIG_SIZE:]
    expected_signature = hmac.new(secret, signed_body, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected_signature):
        return None
    nonce = signed_body[:_NONCE_SIZE]
    ciphertext = signed_body[_NONCE_SIZE:]
    try:
        plaintext = _stream_xor(secret, nonce, ciphertext)
        payload = json.loads(plaintext.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("v") or "") != _TOKEN_VERSION:
        return None
    now_s = int(time.time())
    exp = payload.get("exp")
    path = payload.get("p")
    if not isinstance(exp, int) or exp <= now_s:
        return None
    if not isinstance(path, str) or not path.strip():
        return None
    return path.strip()


def build_gateway_download_url(path: str) -> str:
    token = issue_download_token(path)
    return f"/api/ext/icatmsg-client/files/download?p={token}"
