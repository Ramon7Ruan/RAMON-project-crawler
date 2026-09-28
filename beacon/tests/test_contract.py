"""契约层测试：Observation 必须挡住的那些输入。

这一层的价值在于**把坏数据挡在入口**。校验失败的观测应该在构造时就被拒绝，
而不是带着 NaN 一路走到渲染层才崩。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from beacon.core.contract import (
    Caliber,
    Observation,
    Provenance,
    SourceRef,
    Tier,
    as_dict_list,
    normalize_unit,
    series_sorted,
)


def make_obs(**overrides) -> Observation:
    base = dict(
        indicator="us.treasury.dgs10",
        period="2026-09-21",
        value=4.96,
        unit="%",
        caliber=Caliber.LEVEL,
        source=SourceRef(name="FRED", url="https://fred.stlouisfed.org/", tier=Tier.L2),
        provenance=Provenance(
            fetched_at=datetime(2026, 9, 23, 3, 0, tzinfo=UTC),
            http_status=200,
            from_cache=False,
            raw_sha256="a" * 16,
        ),
    )
    base.update(overrides)
    return Observation(**base)


class TestValueMustBeFinite:
    """NaN / Infinity 必须在入口被拦下。

    它们会穿过算术运算，最后在 JSON 序列化时变成非法的 `NaN` 字面量，
    让前端解析直接失败——那时已经离数据源很远了，排查成本极高。
    """

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_non_finite(self, bad: float) -> None:
        with pytest.raises(ValidationError, match="有限数值"):
            make_obs(value=bad)

    def test_rejects_bool(self) -> None:
        with pytest.raises(ValidationError, match="必须是数值"):
            make_obs(value=True)

    def test_accepts_zero_and_negative(self) -> None:
        # 0 与负数都是合法值（如净流入为负），不该被误伤
        assert make_obs(value=0.0).value == 0.0
        assert make_obs(value=-3.5).value == -3.5


class TestPeriodFormat:
    @pytest.mark.parametrize("ok", ["2026-09", "2026-Q3", "2026-09-21"])
    def test_accepts_known_formats(self, ok: str) -> None:
        assert make_obs(period=ok).period == ok

    @pytest.mark.parametrize("bad", ["2026/09", "20260921", "2026-9", "26-09", "", "Q3"])
    def test_rejects_unknown_formats(self, bad: str) -> None:
        with pytest.raises(ValidationError, match="period 格式非法"):
            make_obs(period=bad)

    def test_period_key_sorts_by_granularity(self) -> None:
        assert make_obs(period="2026-09").period_key == ("M", "2026-09")
        assert make_obs(period="2026-Q3").period_key == ("Q", "2026-Q3")
        assert make_obs(period="2026-09-21").period_key == ("D", "2026-09-21")


class TestUnitWhitelist:
    def test_unknown_unit_rejected(self) -> None:
        with pytest.raises(ValidationError, match="未登记的单位"):
            make_obs(unit="斤")

    def test_normalize_unit_maps_known(self) -> None:
        assert normalize_unit("pct") == "%"
        assert normalize_unit(" CNY_100M ") == "亿元"
        assert normalize_unit("percent") == "%"

    def test_normalize_unit_returns_none_for_unknown(self) -> None:
        # 认不出就返回 None，交给调用方丢弃——**不猜**
        assert normalize_unit("万亿") is None
        assert normalize_unit("") is None


class TestProvenanceRequired:
    def test_source_ref_requires_url(self) -> None:
        with pytest.raises(ValidationError):
            SourceRef(name="某源", url="", tier=Tier.L2)

    def test_provenance_requires_sha(self) -> None:
        with pytest.raises(ValidationError):
            Provenance(fetched_at=datetime.now(UTC), http_status=200, from_cache=False, raw_sha256="ab")


class TestSeriesHelpers:
    def test_duplicate_period_rejected(self) -> None:
        # 同一期间出现两次说明源返回了脏数据，必须报错而不是悄悄去重
        dup = [make_obs(period="2026-09-21", value=4.96), make_obs(period="2026-09-21", value=4.90)]
        with pytest.raises(ValueError, match="重复观测"):
            series_sorted(dup)

    def test_sorted_by_period(self) -> None:
        items = [make_obs(period="2026-09-21"), make_obs(period="2026-09-17"), make_obs(period="2026-09-18")]
        assert [o.period for o in series_sorted(items)] == ["2026-09-17", "2026-09-18", "2026-09-21"]

    def test_as_dict_list_is_json_ready(self) -> None:
        rows = as_dict_list([make_obs()])
        assert rows[0]["indicator"] == "us.treasury.dgs10"
        assert isinstance(rows[0]["source"], dict)
        assert isinstance(rows[0]["caliber"], str)
