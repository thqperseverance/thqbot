"""S3 兼容客户端构造函数测试。

回归点：旧实现把 endpoint 的 hostname 当作 AWS region，导致
``botocore.exceptions.InvalidRegionError: Provided region_name '127.0.0.1' ...``
—— 自建 MinIO 的 endpoint 几乎都是 IP:端口 形式，所以这条路径必然踩到。
"""

from __future__ import annotations

import pytest

from app.s3_compat import DEFAULT_REGION, S3CompatClient, normalize_endpoint_url


@pytest.mark.parametrize(
    "endpoint",
    ["127.0.0.1:9000", "minio:9000", "host.docker.internal:9000", "http://127.0.0.1:9000"],
)
def test_client_accepts_ip_or_hostname_endpoints(endpoint: str):
    client = S3CompatClient(endpoint, access_key="minioadmin", secret_key="minioadmin")
    assert client is not None


def test_client_accepts_explicit_region():
    client = S3CompatClient(
        "127.0.0.1:9000", access_key="a", secret_key="b", region="cn-north-1"
    )
    assert client is not None


def test_default_region_is_valid_aws_style_name():
    assert DEFAULT_REGION == "us-east-1"


@pytest.mark.parametrize(
    "raw,secure,expected",
    [
        ("127.0.0.1:9000", False, "http://127.0.0.1:9000"),
        ("127.0.0.1:9000", True, "https://127.0.0.1:9000"),
        ("http://minio:9000", False, "http://minio:9000"),
        ("https://minio:9000", True, "https://minio:9000"),
    ],
)
def test_normalize_endpoint_url(raw: str, secure: bool, expected: str):
    assert normalize_endpoint_url(raw, secure=secure) == expected


def test_normalize_endpoint_url_requires_value():
    with pytest.raises(ValueError):
        normalize_endpoint_url("", secure=False)
