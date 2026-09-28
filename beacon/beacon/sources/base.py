"""Adapter 接口（PRD §4.2）。

每个数据源实现同一套契约：

    observations()  →  list[Observation]    取数并归一化
    health_url()    →  str                  探活地址
    health_check()  →  HealthResult         是否能判定 ok / broken

**导入期决策：解析出 0 条观测一律视为失败。**
这是 PRD 铁律「失败时不产出候选，而不是产出空内容」在适配器层的落点。
现实里最容易骗过人的情形是"HTTP 200 但内容为空"——例如中证指数在参数写错时
返回 `200 + data: []`。若不在这里拦下，它会一路变成"这个指标这次没有值"。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, date, datetime

from pydantic import BaseModel

from ..core.contract import Caliber, Observation, Provenance, SourceRef, Tier
from ..core.fetch import FetchError, Fetcher, FetchResult


class ConfigError(RuntimeError):
    """配置层错误：源未登记、字段缺失、L1/L2 契约不满足。**必须在启动时就报出来。**"""


class SourceError(RuntimeError):
    """源层失败：解析不出内容、结构变更、返回空。**必须往上抛。**"""

    def __init__(self, source: str, reason: str) -> None:
        self.source = source
        self.reason = reason
        super().__init__(f"[{source}] {reason}")


class HealthResult(BaseModel):
    name: str
    tier: Tier
    ok: bool
    detail: str
    checked_at: datetime


class SourceAdapter(ABC):
    """所有适配器的基类。"""

    name: str = "unnamed"
    tier: Tier = Tier.L2
    upstream: str | None = None

    def __init__(self, config: dict, fetcher: Fetcher) -> None:
        self.config = config
        self.fetcher = fetcher
        # tier 与 upstream 以**配置为准**，不用类属性。
        # 理由：配置是"源是什么"的唯一来源（PRD §3.3 SR5 / §6.5），
        # 若类属性也声明一遍，两处一旦不一致就会出现"配置写 L1、运行时当 L2"的静默偏差。
        self.tier = Tier(config.get("tier", "L2"))
        self.upstream = config.get("upstream")
        # 该源偏好的 HTTP 后端。None = 用全局默认（httpx）。
        # 允许按源覆盖是因为实测存在"某站点只对特定客户端响应"的情况（见 core/curl_transport.py）。
        self.transport_name: str | None = config.get("transport")

        # L2 源必须如实标注上游一手机构（PRD §3.2.1 第 ② 条）。
        # 在构造时报错，而不是让 upstream 静默为 None——
        # 否则产出会带上"看起来有一手出处、实际是转发数据"的误导性 source。
        if self.tier is Tier.L2 and not self.upstream:
            raise ConfigError(
                f"L2 源 {self.name} 未声明 upstream——L2 必须如实标注上游一手机构"
            )

    # ------------------------------------------------------------- 子类实现

    @abstractmethod
    def fetch_raw(self) -> object:
        """取原始数据（已解析成 Python 对象，但**尚未归一化**）。"""

    @abstractmethod
    def normalize(self, raw: object) -> list[Observation]:
        """把原始数据归一化成 Observation 列表。返回空 → 抛 SourceError。"""

    @abstractmethod
    def health_url(self) -> str:
        """一个能代表"这个源还活着吗"的地址。"""

    def request_headers(self) -> dict[str, str]:
        return dict(self.config.get("headers") or {})

    def timeout(self) -> float | None:
        value = self.config.get("timeout_seconds")
        return float(value) if value else None

    # ------------------------------------------------------------- 统一入口

    def observations(self) -> list[Observation]:
        """取数 → 归一化 → 非空校验。任何一步失败都抛异常。"""
        raw = self.fetch_raw()
        result = self.normalize(raw)
        if not result:
            raise SourceError(self.name, "解析后没有任何观测（空结果不视为成功）")
        return result

    def health_check(self) -> HealthResult:
        ok, detail = self.fetcher.health(
            self.health_url(),
            headers=self.request_headers(),
            transport_name=self.transport_name,
        )
        return HealthResult(
            name=self.name,
            tier=self.tier,
            ok=ok,
            detail=detail,
            checked_at=datetime.now(UTC),
        )

    # ------------------------------------------------------------- 工具方法

    def _source_ref(self, url: str, published_at: date | None = None) -> SourceRef:
        return SourceRef(
            name=self.config.get("display_name") or self.name,
            url=url,
            tier=self.tier,
            upstream=self.upstream,
            published_at=published_at,
            channel=self.config.get("via_label"),
        )

    def _fetch_result(self, url: str) -> FetchResult:
        """取一次数据。抓取层失败收敛成 SourceError，让上层只需处理一种错误类型。

        ⚠️ `transport_name` 必须传。**曾经漏传过，症状极具迷惑性**：
        health 走的是 `fetcher.health()`（传了后端，于是探活 OK），
        而真实取数走这里（没传，退回默认 httpx，于是 FRED 全部读超时）。
        结果是"探活全绿，但一到取数就有一个源失败"，看起来像源不稳定，
        实际是自己的配置没贯通。回归测试见 tests/test_sources.py。
        """
        try:
            return self.fetcher.get(
                url,
                headers=self.request_headers(),
                timeout=self.timeout(),
                transport_name=self.transport_name,
            )
        except FetchError as exc:
            raise SourceError(self.name, exc.reason) from exc

    def _provenance(self, result: FetchResult) -> Provenance:
        """留痕直接从抓取结果传递，不做任何推断——推断出来的留痕等于没有留痕。"""
        return Provenance(
            fetched_at=result.fetched_at,
            http_status=result.status,
            from_cache=result.from_cache,
            raw_sha256=result.sha256,
        )

    def _observation(
        self,
        *,
        indicator: str,
        period: str,
        value: float,
        unit: str,
        caliber: Caliber,
        url: str,
        provenance: Provenance,
        published_at: date | None = None,
    ) -> Observation:
        """构造一条观测。所有字段显式传入，缺一个 pydantic 就会报错。"""
        return Observation(
            indicator=indicator,
            period=period,
            value=value,
            unit=unit,
            caliber=caliber,
            source=self._source_ref(url, published_at),
            provenance=provenance,
        )
