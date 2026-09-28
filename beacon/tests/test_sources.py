"""三个适配器的解析测试 + 断网硬门禁。

夹具用的是**实测拿到的真实响应结构**（见 docs/数据源清单.md §2），
所以这些测试同时也在守护"源结构变了会被发现"这件事。
"""

from __future__ import annotations

import pytest
from conftest import StubTransport, load_fixture

from beacon.core.contract import Caliber
from beacon.core.contract import Caliber
from beacon.core.fetch import FetchError
from beacon.sources.base import SourceError
from beacon.sources.csindex import CsindexAdapter
from beacon.sources.eastmoney import EastmoneyAdapter
from beacon.sources.fred import FredAdapter
from beacon.sources.registry import ADAPTERS

FRED = "https://fred.stlouisfed.org"
EM = "https://datacenter-web.eastmoney.com"
CSI = "https://www.csindex.com.cn"


def build(name: str, stub, make_fetcher, config, **kwargs):
    cls = ADAPTERS[name]
    return cls(config["sources"][name], make_fetcher(stub), **kwargs)


# --------------------------------------------------------------------------- #
# FRED
# --------------------------------------------------------------------------- #


class TestFred:
    def test_skips_missing_value_instead_of_zero(self, stub, make_fetcher, sources_config) -> None:
        """FRED 用 `.` 表示缺失。**必须跳过，绝不能当 0**（宁缺勿造）。"""
        stub.routes[FRED] = (200, load_fixture("fred_dgs10.csv"))
        adapter = build("fred", stub, make_fetcher, sources_config, series=["DGS10"])

        obs = adapter.observations()
        # 夹具 6 行，其中 2026-09-16 是 "."，应被跳过 → 5 条
        assert len(obs) == 5
        assert "2026-09-16" not in {o.period for o in obs}
        assert all(o.value != 0 for o in obs), "缺失值不得被填成 0"

    def test_parses_real_values(self, stub, make_fetcher, sources_config) -> None:
        stub.routes[FRED] = (200, load_fixture("fred_dgs10.csv"))
        adapter = build("fred", stub, make_fetcher, sources_config, series=["DGS10"])
        by_period = {o.period: o for o in adapter.observations()}

        assert by_period["2026-09-21"].value == 4.96
        assert by_period["2026-09-18"].value == 5.01
        assert by_period["2026-09-21"].unit == "%"
        assert by_period["2026-09-21"].source.tier.value == "L2"
        assert by_period["2026-09-21"].source.upstream, "L2 源必须标注上游"

    def test_sidecar_has_provenance(self, stub, make_fetcher, sources_config) -> None:
        stub.routes[FRED] = (200, load_fixture("fred_dgs10.csv"))
        adapter = build("fred", stub, make_fetcher, sources_config, series=["DGS10"])
        prov = adapter.observations()[0].provenance
        assert prov.http_status == 200
        assert len(prov.raw_sha256) == 64
        assert prov.fetched_at is not None

    def test_all_values_missing_raises(self, stub, make_fetcher, sources_config) -> None:
        stub.routes[FRED] = (200, "observation_date,DGS10\n2026-09-21,.\n")
        adapter = build("fred", stub, make_fetcher, sources_config, series=["DGS10"])
        with pytest.raises(SourceError, match="没有任何有效观测"):
            adapter.observations()

    def test_bad_header_raises(self, stub, make_fetcher, sources_config) -> None:
        stub.routes[FRED] = (200, "onlyonecolumn\n")
        adapter = build("fred", stub, make_fetcher, sources_config, series=["DGS10"])
        with pytest.raises(SourceError, match="表头异常"):
            adapter.observations()

    def test_unregistered_series_raises(self, stub, make_fetcher, sources_config) -> None:
        stub.routes[FRED] = (200, load_fixture("fred_dgs10.csv"))
        adapter = build("fred", stub, make_fetcher, sources_config, series=["NOPE"])
        with pytest.raises(SourceError, match="未登记的序列"):
            adapter.fetch_raw()


# --------------------------------------------------------------------------- #
# 东方财富
# --------------------------------------------------------------------------- #


class TestEastmoney:
    def test_parses_two_calibers_and_skips_null(self, stub, make_fetcher, sources_config) -> None:
        """夹具 3 期 × 2 指标 × 2 口径，其中 6 月的非制造业为 null。

        水平值 5 条（6 月非制造业为 null）+ 同比 5 条 = 10 条。

        为什么同比也要产出：它是这个指标**唯一可用的 C2 手段**
        （同源多口径互校，实测 212/212 零偏差）。漏掉它，PMI 就没有任何交叉校验了。
        """
        stub.routes[EM] = (200, load_fixture("eastmoney_pmi.json"))
        obs = build("eastmoney", stub, make_fetcher, sources_config).observations()

        assert len(obs) == 10
        levels = [o for o in obs if o.caliber is Caliber.LEVEL]
        yoys = [o for o in obs if o.caliber is Caliber.YOY]
        assert len(levels) == 5 and len(yoys) == 5
        assert {o.indicator for o in levels} == {
            "cn.pmi.manufacturing",
            "cn.pmi.non-manufacturing",
        }
        assert {o.indicator for o in yoys} == {
            "cn.pmi.manufacturing.yoy",
            "cn.pmi.non-manufacturing.yoy",
        }
        assert {o.unit for o in yoys} == {"%"}, "同比是百分比，不是点"

    def test_period_is_monthly(self, stub, make_fetcher, sources_config) -> None:
        stub.routes[EM] = (200, load_fixture("eastmoney_pmi.json"))
        obs = build("eastmoney", stub, make_fetcher, sources_config).observations()
        by_key = {(o.indicator, o.period): o.value for o in obs if o.caliber is Caliber.LEVEL}

        assert by_key[("cn.pmi.manufacturing", "2026-08")] == 49.8
        assert by_key[("cn.pmi.non-manufacturing", "2026-08")] == 49.0
        assert by_key[("cn.pmi.manufacturing", "2026-06")] == 50.3
        assert ("cn.pmi.non-manufacturing", "2026-06") not in by_key, "null 应被跳过"

    def test_business_failure_raises_even_on_http_200(self, stub, make_fetcher, sources_config) -> None:
        """东财用 success 字段表示业务成败 —— HTTP 200 不代表成功。"""
        stub.routes[EM] = (200, load_fixture("eastmoney_error.json"))
        adapter = build("eastmoney", stub, make_fetcher, sources_config)
        with pytest.raises(SourceError, match="业务层失败"):
            adapter.observations()

    def test_missing_result_list_raises(self, stub, make_fetcher, sources_config) -> None:
        stub.routes[EM] = (200, '{"success":true,"result":{}}')
        adapter = build("eastmoney", stub, make_fetcher, sources_config)
        with pytest.raises(SourceError, match="缺少 result.data"):
            adapter.observations()

    def test_non_json_raises(self, stub, make_fetcher, sources_config) -> None:
        stub.routes[EM] = (200, "<html>被拦了</html>")
        adapter = build("eastmoney", stub, make_fetcher, sources_config)
        with pytest.raises(SourceError, match="不是合法 JSON"):
            adapter.observations()

    def test_referer_header_is_sent(self, stub, make_fetcher, sources_config) -> None:
        """缺 Referer 会被拒（实测）——这条断言防的是"配置被改掉"。"""
        stub.routes[EM] = (200, load_fixture("eastmoney_pmi.json"))
        build("eastmoney", stub, make_fetcher, sources_config).observations()
        assert "Referer" in stub.calls[0]["headers"]


# --------------------------------------------------------------------------- #
# 中证指数
# --------------------------------------------------------------------------- #


class TestCsindex:
    def test_parses_pe_from_peg_field(self, stub, make_fetcher, sources_config) -> None:
        """字段名叫 peg，含义是 PE —— 这条断言把"名字与含义的错配"钉住。"""
        stub.routes[CSI] = (200, load_fixture("csindex_perf.json"))
        obs = build("csindex", stub, make_fetcher, sources_config).observations()

        assert len(obs) == 3
        latest = [o for o in obs if o.period == "2026-09-22"][0]
        assert latest.value == 13.53
        assert latest.unit == "倍"
        assert latest.indicator == "cn.index.pe.000300"

    def test_source_is_l1(self, stub, make_fetcher, sources_config) -> None:
        stub.routes[CSI] = (200, load_fixture("csindex_perf.json"))
        obs = build("csindex", stub, make_fetcher, sources_config).observations()
        assert obs[0].source.tier.value == "L1"

    def test_empty_array_is_treated_as_failure(self, stub, make_fetcher, sources_config) -> None:
        """**伪成功拦截**：HTTP 200 + code 200 + data 为空，这是最容易被误判的假象。"""
        stub.routes[CSI] = (200, load_fixture("csindex_empty.json"))
        adapter = build("csindex", stub, make_fetcher, sources_config)
        with pytest.raises(SourceError, match="data 为空"):
            adapter.observations()

    def test_date_format_is_compact(self, stub, make_fetcher, sources_config) -> None:
        """日期必须 YYYYMMDD 无连字符，否则服务端返回空数组。"""
        adapter = build("csindex", stub, make_fetcher, sources_config)
        url = adapter.endpoint()
        assert "startDate=" in url and "endDate=" in url
        for token in url.split("&"):
            if token.startswith(("startDate=", "endDate=")):
                value = token.split("=", 1)[1]
                assert len(value) == 8 and value.isdigit(), f"日期格式不对：{value}"

    def test_negative_pe_skipped(self, stub, make_fetcher, sources_config) -> None:
        payload = load_fixture("csindex_perf.json").replace('"peg":13.53', '"peg":-1.2')
        stub.routes[CSI] = (200, payload)
        obs = build("csindex", stub, make_fetcher, sources_config).observations()
        assert all(o.value > 0 for o in obs)
        assert len(obs) == 2, "负 PE 在指数层面不成立，应跳过而不是当成便宜"

    def test_missing_data_key_raises(self, stub, make_fetcher, sources_config) -> None:
        stub.routes[CSI] = (200, '{"code":"200","msg":"Success"}')
        adapter = build("csindex", stub, make_fetcher, sources_config)
        with pytest.raises(SourceError, match="缺少 data 列表"):
            adapter.observations()


# --------------------------------------------------------------------------- #
# 硬门禁：失败不产假数据（PRD A-C3）
# --------------------------------------------------------------------------- #


class TestFailureIsolation:
    """P-C2 的硬门禁：断网时全部 adapter 干净失败，产出 0 条候选。"""

    def test_all_sources_offline_yield_zero_observations(
        self, stub, make_fetcher, sources_config
    ) -> None:
        for host in (FRED, EM, CSI):
            stub.routes[host] = FetchError(host, "网络不可达")

        total, errors = 0, []
        for name in ADAPTERS:
            adapter = build(name, stub, make_fetcher, sources_config)
            try:
                total += len(adapter.observations())
            except SourceError as exc:
                errors.append(exc.reason)

        assert total == 0, "断网时不得产出任何观测"
        assert len(errors) == len(ADAPTERS), "每个源都应干净失败，而不是静默返回空"

    def test_one_source_down_does_not_affect_others(
        self, stub, make_fetcher, sources_config
    ) -> None:
        """单源失败不影响其他源（NF-C10）。"""
        stub.routes[FRED] = FetchError(FRED, "超时")
        stub.routes[EM] = (200, load_fixture("eastmoney_pmi.json"))
        stub.routes[CSI] = (200, load_fixture("csindex_perf.json"))

        results: dict[str, int] = {}
        for name in ADAPTERS:
            adapter = build(name, stub, make_fetcher, sources_config, **(
                {"series": ["DGS10"]} if name == "fred" else {}
            ))
            try:
                results[name] = len(adapter.observations())
            except SourceError:
                results[name] = -1

        assert results["fred"] == -1, "FRED 应失败"
        assert results["eastmoney"] == 10
        assert results["csindex"] == 3

    def test_health_check_reports_broken_without_raising(
        self, stub, make_fetcher, sources_config
    ) -> None:
        for host in (FRED, EM, CSI):
            stub.routes[host] = FetchError(host, "连接超时")
        for name in ADAPTERS:
            result = build(name, stub, make_fetcher, sources_config).health_check()
            assert result.ok is False
            assert result.detail


# --------------------------------------------------------------------------- #
# 配置声明的 HTTP 后端必须贯通到「真实取数」路径
# --------------------------------------------------------------------------- #


class TestTransportPropagation:
    """回归测试：`transport` 配置不能只在探活时生效。

    曾经踩过的坑：`health_check()` 传了 `transport_name`，而 `_fetch_result()` 漏传。
    症状极具迷惑性——**探活三源全绿，一到 export 就发现 FRED 全部读超时**，
    看起来像"这个源不稳定"，实际是自己的配置根本没走到取数路径上。
    """  # noqa: D210

    def test_declared_transport_is_used_on_the_fetch_path(
        self, make_fetcher, sources_config
    ) -> None:
        # 默认后端（httpx）与声明后端（curl）各一个桩，都配好合法的响应，
        # 这样一旦走错也能正常解析、只会表现为"数据来自错误的源"，
        # 断言失败信息比抛异常更直白。
        default_stub = StubTransport({FRED: (200, load_fixture("fred_dgs10.csv"))})
        declared_stub = StubTransport({FRED: (200, load_fixture("fred_dgs10.csv"))})

        fetcher = make_fetcher(default_stub, transport_name="httpx")
        fetcher._transports["curl"] = declared_stub  # type: ignore[attr-defined]

        config = dict(sources_config["sources"]["fred"], transport="curl")
        adapter = FredAdapter(config, fetcher, series=["DGS10"])
        obs = adapter.observations()

        assert declared_stub.calls, "取数必须走配置里声明的 curl 后端"
        assert not default_stub.calls, "不得悄悄退回默认后端——这正是那个 bug 的形态"
        assert len(obs) == 5

    def test_source_without_transport_uses_the_default_backend(
        self, stub, make_fetcher, sources_config
    ) -> None:
        """没声明 transport 的源照常走默认后端，不受这条机制影响。"""
        stub.routes[CSI] = (200, load_fixture("csindex_perf.json"))
        adapter = build("csindex", stub, make_fetcher, sources_config)

        assert adapter.transport_name is None
        assert len(adapter.observations()) == 3
        assert stub.calls, "未声明后端的源必须走默认传输"
