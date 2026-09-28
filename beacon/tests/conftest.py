"""测试公共夹具。

核心是 `StubTransport`：它让所有测试**不打网络**（NF-C9），
同时记录"被请求过哪些 URL、带了什么 header、超时设成多少"——
后两者正是"限速生效""超时生效"这类断言需要的证据。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from beacon.core.fetch import FetchError, Fetcher

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class StubTransport:
    """按 URL 前缀匹配的假传输。

    routes 的值可以是：
      * `(status, body)` —— 正常响应（body 为 str 时自动编码）
      * `Exception` 实例 —— 直接抛（模拟网络层故障）
    """

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        for prefix, value in self.routes.items():
            if url.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                status, body = value
                return status, body.encode("utf-8") if isinstance(body, str) else body
        raise FetchError(url, "未配置的桩（测试用例漏配了这个 URL）")

    @property
    def urls(self) -> list[str]:
        return [c["url"] for c in self.calls]


@pytest.fixture
def stub() -> StubTransport:
    return StubTransport()


@pytest.fixture
def make_fetcher(tmp_path: Path):
    """构造一个用假传输、且不会真的 sleep 的 Fetcher。"""

    def _make(transport: StubTransport, **overrides: Any) -> Fetcher:
        slept: list[float] = []
        kwargs: dict[str, Any] = {
            "cache_dir": tmp_path / "cache",
            "transport": transport,
            "timeout": 30.0,
            "min_interval": 0.0,
            "max_retries": 1,
            "cache_ttl_hours": 12.0,
            "sleeper": slept.append,
        }
        kwargs.update(overrides)
        fetcher = Fetcher(**kwargs)
        fetcher._slept = slept  # type: ignore[attr-defined]  # 供断言使用
        return fetcher

    return _make


@pytest.fixture
def sources_config() -> dict:
    """最小可用的源配置（字段与 beacon/config/sources.yaml 保持一致）。"""
    return {
        "defaults": {"timeout_seconds": 30, "cache_ttl_hours": 12},
        "sources": {
            "fred": {
                "tier": "L2",
                "upstream": "美国财政部 / 美联储",
                "base_url": "https://fred.stlouisfed.org",
                "headers": {"User-Agent": "test-agent"},
            },
            "eastmoney": {
                "tier": "L2",
                "upstream": "国家统计局 / 中国物流与采购联合会",
                "base_url": "https://datacenter-web.eastmoney.com",
                "headers": {"Referer": "https://data.eastmoney.com/"},
            },
            "csindex": {
                "tier": "L1",
                "upstream": "中证指数有限公司（一手）",
                "base_url": "https://www.csindex.com.cn",
                "headers": {},
            },
        },
    }
