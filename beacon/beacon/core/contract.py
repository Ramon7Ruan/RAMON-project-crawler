"""数据契约：Observation —— 唯一内部交换格式（PRD §6.4）。

设计原则
--------
1. **源无关**：本模块不认识任何具体数据源，也不认识任何具体 app。
   所有下游（归一化 / 漏斗 / 分级 / 候选）只认 Observation。
2. **不可信输入**：字段来自外部网络，必须做显式校验。
   `value` 拒绝 NaN/Infinity，`period` 必须匹配已知格式——宁可丢弃，不许放行。
3. **可溯源**：每条观测都必须带齐 `来源 / URL / 发布时间 / 抓取时间`，
   缺任一项即视为不合格（PRD §4.4 F3）。
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------- #
# 单位规范表（PRD §4.4 F3：单位必须明确且可映射）
# --------------------------------------------------------------------------- #

UNIT_TABLE: dict[str, str] = {
    "pct": "%",
    "percent": "%",
    "index": "点",
    "point": "点",
    "bp": "bp",
    "usd": "美元",
    "cny": "元",
    "cny_100m": "亿元",
    "cny_1e8": "亿元",
    "times": "倍",
    "ratio": "比值",
    "date": "日期",
}
"""规范化单位映射。**未登记的原始单位一律拒绝**，不做猜测。"""


class Caliber(str, Enum):
    """口径标记。

    为什么必须有它：`F4 序列一致性检查` 靠它防住最隐蔽的一类错误——
    **"接口通了、数字拿到了、但拿错了序列"**（累计值 vs 当期值、同比 vs 环比）。
    没有显式口径标记的数据，无法与历史序列比对，因此一律拒绝。
    """

    LEVEL = "level"            # 水平值：收益率、点位、价格
    YOY = "yoy"                # 同比
    MOM = "mom"                # 环比
    CUMULATIVE = "cumulative"  # 累计值（年初至今）
    PERIOD = "period"          # 当期值（单月/单季）
    SHARE = "share"            # 占比 / 分位


class Tier(str, Enum):
    L1 = "L1"  # 官方一手
    L2 = "L2"  # 结构化聚合（含官方数据的分发渠道）
    L3 = "L3"  # 媒体线索，**不得作为内容来源**


class SourceRef(BaseModel):
    """出处。可点击、可核对——这是"可溯源"的最小单元（PRD NF-C7）。"""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, description="机构名，如 中证指数有限公司")
    url: str = Field(min_length=1, description="可点击的原始地址")
    tier: Tier
    upstream: str | None = Field(default=None, description="L2 源的上游一手机构，必须如实标注")
    published_at: date | None = Field(default=None, description="数据发布日（非抓取日）")
    channel: str | None = Field(
        default=None,
        description=(
            "数据实际经手的渠道标签（如 东方财富 / FRED）。"
            "与 upstream 必须分开：upstream 说「这是谁的数据」，channel 说「我们通过谁拿到」。"
            "两者混在一起会写出「国家统计局（上游：国家统计局）」这种没信息量的话。"
        ),
    )


class Provenance(BaseModel):
    """抓取留痕。任一最终内容字段都要能反查到这些值。"""

    model_config = ConfigDict(frozen=True)

    fetched_at: datetime
    http_status: int
    from_cache: bool
    raw_sha256: str = Field(min_length=8)


_PERIOD_PATTERNS = (
    re.compile(r"^\d{4}-\d{2}$"),           # 月度
    re.compile(r"^\d{4}-Q[1-4]$"),          # 季度
    re.compile(r"^\d{4}-\d{2}-\d{2}$"),     # 日度
)


class Observation(BaseModel):
    """一条原子观测：某指标在某期间的值。

    这是**唯一**的内部交换格式。sources 产出它，core 消费它，consumers 翻译它。
    """

    model_config = ConfigDict(frozen=True)

    indicator: str = Field(min_length=3, description="指标 id，如 us.treasury.dgs10")
    period: str = Field(description="期间：YYYY-MM / YYYY-Qn / YYYY-MM-DD")
    value: float
    unit: str = Field(description="规范化单位，取自 UNIT_TABLE 的值")
    caliber: Caliber
    source: SourceRef
    provenance: Provenance

    @field_validator("value", mode="before")
    @classmethod
    def _finite(cls, v: object) -> float:
        """拒绝 NaN / Infinity / 布尔 / 字符串。

        两个刻意的设计：

        1. **必须是 `mode="before"`**。若在默认的 after 模式校验，pydantic 会先把
           `True` 强转成 `1.0`，那时再判 `isinstance(v, bool)` 已经晚了——布尔值会
           悄悄变成数字 1 混进序列。
        2. **不接受字符串**（如 `"4.96"`）。强制适配器显式做 `float()` 转换，
           让"从文本转数值"这件事发生在有上下文、能报行号的地方，而不是在契约层
           被动接受一个可能带了千分位或百分号的字符串。

        为什么要拦 NaN/Infinity：它们会穿过算术运算，最后在 JSON 序列化时变成
        非法的 `NaN` 字面量（JSON 标准不允许），前端解析直接失败。
        """
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"value 必须是数值，收到 {type(v).__name__}")
        if not math.isfinite(float(v)):
            raise ValueError(f"value 必须是有限数值，收到 {v!r}")
        return float(v)

    @field_validator("period")
    @classmethod
    def _period_format(cls, v: str) -> str:
        if not any(p.match(v) for p in _PERIOD_PATTERNS):
            raise ValueError(f"period 格式非法（需 YYYY-MM / YYYY-Qn / YYYY-MM-DD）：{v!r}")
        return v

    @field_validator("unit")
    @classmethod
    def _unit_known(cls, v: str) -> str:
        if v not in UNIT_TABLE.values():
            raise ValueError(f"未登记的单位：{v!r}（需先加入 UNIT_TABLE，不做猜测）")
        return v

    @property
    def period_key(self) -> tuple[str, str]:
        """排序键：(粒度标记, 期间字符串)。

        必须**先判季度**：`2026-Q3` 与月度 `2026-09` 长度都是 7、第 4 位都是连字符，
        按长度判断会把季度误判成月度。
        """
        if "Q" in self.period:
            return ("Q", self.period)
        if len(self.period) == 7:
            return ("M", self.period)
        return ("D", self.period)


def normalize_unit(raw: str) -> str | None:
    """把源里的原始单位映射成规范单位。认不出返回 None（**不猜**）。"""
    if not raw:
        return None
    key = raw.strip().lower().replace(" ", "")
    return UNIT_TABLE.get(key)


def series_sorted(observations: list[Observation]) -> list[Observation]:
    """按期间排序，并拒绝重复期间——重复说明源返回了脏数据。"""
    by_period: dict[str, Observation] = {}
    for o in observations:
        if o.period in by_period:
            raise ValueError(f"同一期间出现重复观测：{o.indicator} {o.period}")
        by_period[o.period] = o
    return [by_period[k] for k in sorted(by_period)]


def as_dict_list(observations: list[Observation]) -> list[dict[str, Any]]:
    """序列化为普通 dict 列表（供 feed.jsonl 等中性产物使用）。"""
    return [o.model_dump(mode="json") for o in observations]
