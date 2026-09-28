"""抓取纪律层测试（PRD §3.4 六条纪律的可验证部分）。

重点验证四件事：
* 缓存真的省掉了网络请求
* 限速真的生效
* **失败真的抛异常，而不是返回空**（这是铁律，也是 A-C3 的地基）
* 4xx 不重试、5xx 才重试
"""

from __future__ import annotations

import json

import pytest

from beacon.core.fetch import FetchError, Fetcher

URL = "https://example.test/data"


class TestCache:
    def test_second_call_hits_cache_without_network(self, stub, make_fetcher) -> None:
        stub.routes["https://example.test"] = (200, "hello")
        fetcher = make_fetcher(stub)

        first = fetcher.get(URL)
        assert first.from_cache is False
        assert first.text == "hello"
        assert len(stub.calls) == 1

        second = fetcher.get(URL)
        assert second.from_cache is True
        assert second.text == "hello"
        assert len(stub.calls) == 1, "第二次调用不应再打网络"

    def test_use_cache_false_forces_network(self, stub, make_fetcher) -> None:
        stub.routes["https://example.test"] = (200, "hello")
        fetcher = make_fetcher(stub)
        fetcher.get(URL)
        fetcher.get(URL, use_cache=False)
        assert len(stub.calls) == 2

    def test_cache_key_is_per_url(self, stub, make_fetcher) -> None:
        stub.routes["https://example.test"] = (200, "x")
        fetcher = make_fetcher(stub)
        fetcher.get(URL)
        fetcher.get(URL + "?other=1")
        assert len(stub.calls) == 2


class TestThrottle:
    def test_waits_between_same_host_requests(self, stub, make_fetcher) -> None:
        stub.routes["https://example.test"] = (200, "x")
        fetcher = make_fetcher(stub, min_interval=0.5)

        fetcher.get(URL, use_cache=False)
        assert fetcher._slept == [], "第一次请求不该等待"

        fetcher.get(URL + "?b=2", use_cache=False)
        assert fetcher._slept and fetcher._slept[0] > 0, "同域名第二次请求应等待"

    def test_different_hosts_do_not_wait(self, stub, make_fetcher) -> None:
        stub.routes["https://a.test"] = (200, "x")
        stub.routes["https://b.test"] = (200, "y")
        fetcher = make_fetcher(stub, min_interval=10.0)
        fetcher.get("https://a.test/x", use_cache=False)
        fetcher.get("https://b.test/y", use_cache=False)
        assert fetcher._slept == [], "不同域名之间不该限速"


class TestFailureNeverYieldsEmpty:
    """铁律：失败必须抛异常。返回空值会被下游当成"数据就是 0"（A-C3）。"""

    def test_network_error_raises(self, stub, make_fetcher) -> None:
        stub.routes["https://example.test"] = FetchError(URL, "连接被拒绝")
        fetcher = make_fetcher(stub)
        with pytest.raises(FetchError, match="连接被拒绝"):
            fetcher.get(URL)

    def test_404_raises_and_does_not_retry(self, stub, make_fetcher) -> None:
        stub.routes["https://example.test"] = (404, "not found")
        fetcher = make_fetcher(stub, max_retries=3)
        with pytest.raises(FetchError) as exc:
            fetcher.get(URL)
        assert exc.value.status == 404
        assert len(stub.calls) == 1, "4xx 重试不会变好，只会更像攻击"

    def test_500_retries_up_to_max(self, stub, make_fetcher) -> None:
        stub.routes["https://example.test"] = (500, "boom")
        fetcher = make_fetcher(stub, max_retries=3)
        with pytest.raises(FetchError) as exc:
            fetcher.get(URL)
        assert exc.value.status == 500
        assert len(stub.calls) == 3, "5xx 应重试到上限"

    def test_no_output_written_on_total_failure(self, stub, make_fetcher, tmp_path) -> None:
        stub.routes["https://example.test"] = (500, "boom")
        fetcher = make_fetcher(stub)
        with pytest.raises(FetchError):
            fetcher.get(URL)
        # 失败时不应留下缓存文件（否则下次会读到坏内容）
        assert not any(p.suffix == ".bin" for p in (tmp_path / "cache").glob("*"))


class TestTelemetry:
    def test_timeout_passed_to_transport(self, stub, make_fetcher) -> None:
        stub.routes["https://example.test"] = (200, "x")
        fetcher = make_fetcher(stub, timeout=42.0)
        fetcher.get(URL)
        assert stub.calls[0]["timeout"] == 42.0

    def test_per_call_timeout_overrides_default(self, stub, make_fetcher) -> None:
        stub.routes["https://example.test"] = (200, "x")
        fetcher = make_fetcher(stub)
        fetcher.get(URL, timeout=7.5)
        assert stub.calls[0]["timeout"] == 7.5

    def test_fetch_log_records_every_attempt(self, stub, make_fetcher) -> None:
        stub.routes["https://example.test"] = (200, "x")
        fetcher = make_fetcher(stub)
        fetcher.get(URL)
        fetcher.get(URL)  # 命中缓存

        lines = [json.loads(l) for l in fetcher.log_path.read_text(encoding="utf-8").splitlines()]
        assert [l["note"] for l in lines] == ["network", "cache-hit"]
        assert lines[0]["status"] == 200

    def test_result_carries_sha_and_url(self, stub, make_fetcher) -> None:
        stub.routes["https://example.test"] = (200, "hello")
        result = make_fetcher(stub).get(URL)
        assert result.url == URL
        assert len(result.sha256) == 64
        assert result.status == 200


class TestHealth:
    def test_health_reports_ok(self, stub, make_fetcher) -> None:
        stub.routes["https://example.test"] = (200, "x")
        ok, detail = make_fetcher(stub).health(URL)
        assert ok is True
        assert "200" in detail

    def test_health_reports_broken_without_raising(self, stub, make_fetcher) -> None:
        stub.routes["https://example.test"] = FetchError(URL, "超时")
        ok, detail = make_fetcher(stub).health(URL)
        assert ok is False
        assert "超时" in detail

    def test_health_does_not_write_cache(self, stub, make_fetcher, tmp_path) -> None:
        stub.routes["https://example.test"] = (200, "x")
        make_fetcher(stub).health(URL)
        assert not any(p.suffix == ".bin" for p in (tmp_path / "cache").glob("*"))
