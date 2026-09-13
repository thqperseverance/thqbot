"""NO_PROXY 清理逻辑的单测（httpx 无法解析 "[::1]"）。"""

from __future__ import annotations

import os

import pytest

from ithqbot.providers.custom_provider import CustomProvider, sanitize_no_proxy_env

DIRTY = "127.0.0.1,localhost,::1,[::1]"
CLEAN = "127.0.0.1,localhost"


def test_removes_bracketed_and_bare_ipv6_loopback():
    env = {"NO_PROXY": DIRTY, "no_proxy": DIRTY}
    assert sanitize_no_proxy_env(env) is True
    assert env["NO_PROXY"] == CLEAN
    assert env["no_proxy"] == CLEAN


def test_is_idempotent():
    env = {"NO_PROXY": CLEAN}
    assert sanitize_no_proxy_env(env) is False
    assert env["NO_PROXY"] == CLEAN
    assert sanitize_no_proxy_env(env) is False


def test_handles_missing_and_empty_values():
    # 完全不存在 -> 不修改
    assert sanitize_no_proxy_env({}) is False

    # 空字符串 -> 不修改
    assert sanitize_no_proxy_env({"NO_PROXY": ""}) is False

    # 纯空白 -> 规范化为空字符串（属于修改，语义等价于"没有 bypass 列表"）
    blank = {"NO_PROXY": "", "no_proxy": "   "}
    assert sanitize_no_proxy_env(blank) is True
    assert blank["NO_PROXY"] == ""
    assert blank["no_proxy"] == ""


def test_preserves_unrelated_entries():
    env = {"NO_PROXY": "a.example.com, 10.0.0.1 ,[::1],b.example.com"}
    assert sanitize_no_proxy_env(env) is True
    assert env["NO_PROXY"] == "a.example.com,10.0.0.1,b.example.com"


def test_handles_single_loophole_entry():
    env = {"no_proxy": "[::1]"}
    sanitize_no_proxy_env(env)
    assert env["no_proxy"] == ""


def test_provider_builds_client_with_dirty_no_proxy(monkeypatch):
    """核心回归：脏 NO_PROXY 不应让客户端构造抛 InvalidURL。"""
    monkeypatch.setenv("NO_PROXY", DIRTY)
    monkeypatch.setenv("no_proxy", DIRTY)

    provider = CustomProvider(api_key="test-key", api_base="https://api.deepseek.com")
    client = provider._client_for_api_base("https://api.deepseek.com")  # 不应抛异常
    assert client is not None
    # 环境已被就地清理
    assert os.environ["NO_PROXY"] == CLEAN
    assert os.environ["no_proxy"] == CLEAN
    # 同一 base 复用缓存客户端
    assert provider._client_for_api_base("https://api.deepseek.com") is client


def test_client_cache_is_per_api_base(monkeypatch):
    monkeypatch.setenv("NO_PROXY", CLEAN)
    provider = CustomProvider(api_key="k", api_base="https://a.example.com")
    first = provider._client_for_api_base("https://a.example.com")
    second = provider._client_for_api_base("https://b.example.com")
    assert first is not second


def test_resolve_api_base_prefers_model_mapping():
    provider = CustomProvider(
        api_key="k",
        api_base="https://default.example.com",
        model_api_bases={"special-model": "https://special.example.com"},
    )
    assert provider._resolve_api_base("special-model") == "https://special.example.com"
    assert provider._resolve_api_base("other-model") == "https://default.example.com"


@pytest.mark.parametrize(
    "raw,expected",
    [
        (DIRTY, CLEAN),
        ("[::1]", ""),
        ("::1", ""),
        (CLEAN, CLEAN),
    ],
)
def test_matrix(raw: str, expected: str):
    env = {"NO_PROXY": raw}
    sanitize_no_proxy_env(env)
    assert env["NO_PROXY"] == expected
