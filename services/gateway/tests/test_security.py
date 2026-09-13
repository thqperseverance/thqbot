"""口令哈希与会话令牌的单元测试。"""

from __future__ import annotations

import time

import pytest

from app.security import (
    DEFAULT_ITERATIONS,
    hash_password,
    load_session_token,
    sign_session_token,
    verify_password,
)

SECRET = "unit-test-secret"


def test_hash_password_format_and_salt_uniqueness():
    first = hash_password("correct horse", iterations=1000)
    second = hash_password("correct horse", iterations=1000)
    algorithm, iterations, salt, digest = first.split("$")
    assert algorithm == "pbkdf2_sha256"
    assert int(iterations) == 1000
    assert len(salt) == 32
    assert len(digest) == 64
    # 相同口令必须产生不同摘要（每次新 salt）
    assert first != second


def test_verify_password_accepts_correct_and_rejects_wrong():
    encoded = hash_password("s3cret-pass", iterations=1000)
    assert verify_password("s3cret-pass", encoded) is True
    assert verify_password("s3cret-pas", encoded) is False
    assert verify_password("", encoded) is False


def test_verify_password_handles_malformed_hashes():
    for broken in ("", "not-a-hash", "pbkdf2_sha256$abc$x$y", "md5$1000$salt$digest", "pbkdf2_sha256$-1$a$b"):
        assert verify_password("whatever", broken) is False


def test_verify_password_rejects_absurd_iterations():
    """防止用超大 iterations 的哈希做 DoS。"""
    encoded = hash_password("x", iterations=1000)
    _, _, salt, digest = encoded.split("$")
    hostile = f"pbkdf2_sha256$99999999${salt}${digest}"
    assert verify_password("x", hostile) is False


def test_hash_password_requires_non_empty_password():
    with pytest.raises(ValueError):
        hash_password("")


def test_default_iterations_is_reasonable():
    assert DEFAULT_ITERATIONS >= 100_000


def test_session_token_roundtrip():
    token = sign_session_token(SECRET, "user-123")
    assert load_session_token(SECRET, token, max_age=60) == "user-123"


def test_session_token_rejects_other_secret():
    token = sign_session_token(SECRET, "user-123")
    assert load_session_token("another-secret", token, max_age=60) is None


def test_session_token_rejects_tampering():
    token = sign_session_token(SECRET, "user-123")
    assert load_session_token(SECRET, token + "x", max_age=60) is None
    assert load_session_token(SECRET, "", max_age=60) is None


def test_session_token_expires():
    token = sign_session_token(SECRET, "user-123")
    time.sleep(1.1)
    assert load_session_token(SECRET, token, max_age=0) is None
    assert load_session_token(SECRET, token, max_age=60) == "user-123"


def test_sign_session_token_requires_user_id():
    with pytest.raises(ValueError):
        sign_session_token(SECRET, "")
