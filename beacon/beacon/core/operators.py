"""已注册的派生算子（C3 派生公式复核的基础）。

**为什么派生公式必须登记在案**
----------------------------
PRD §4.5 要求「派生指标必须携带可复现的公式与输入」。如果公式内联在漏斗代码里，
"可复现"就只是一句口头承诺——没有任何机制能拦住有人改了一行而没人知道。

所以：算子在这里集中实现、在 `config/mapping.yaml` 里按名字引用，
映射表里出现未注册的算子名会在**加载配置时**直接报错。

一个反直觉但重要的细节：**算子的参考时点必须是数据自身的时点，不是"今天"**。
用"今天"会让同一份输入在不同日期产出不同的值，幂等性（PRD 测试⑦）立刻失效。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable

from .contract import Observation


class OperatorError(ValueError):
    """算子无法执行（输入不足、参数缺失）。**必须显式报出，不得静默跳过。**"""


@dataclass(frozen=True)
class SeriesPoint:
    period: str
    value: float


def to_points(series: list[Observation]) -> list[SeriesPoint]:
    return [SeriesPoint(o.period, o.value) for o in sorted(series, key=lambda x: x.period)]


def _as_date(period: str) -> date:
    """把期间转成可比较的日期。月/季取该期第一天（用于区间起点计算）。"""
    if len(period) == 10:
        return date.fromisoformat(period)
    if "Q" in period:
        year, q = period.split("-Q")
        return date(int(year), (int(q) - 1) * 3 + 1, 1)
    return date(int(period[:4]), int(period[5:7]), 1)


# --------------------------------------------------------------------------- #
# 算子 1：秩占比（估值分位）
# --------------------------------------------------------------------------- #


def percentile_rank(
    points: list[SeriesPoint],
    windows: list[dict[str, Any]],
) -> list[float]:
    """当前值在若干回溯区间内的秩占比（%）。

    公式（PRD 里那个 `\\text{分位}`）：

        #{t ∈ T : PE_t ≤ PE_now} / #{t ∈ T} × 100

    两个刻意的选择：

    1. **参考时点 = 最新观测的期间，而不是"今天"**。理由见模块说明（幂等）。
    2. **当前值计入分子**。若不计，满区间内最高估值也永远拿不到 100%，
       分位的上界就不再是 100，而这是读者默认会假设的。
    """
    if not points:
        raise OperatorError("序列为空，无法计算分位")

    ordered = sorted(points, key=lambda p: p.period)
    latest = ordered[-1]
    now_date = _as_date(latest.period)

    out: list[float] = []
    for w in windows:
        if "days" not in w:
            raise OperatorError(f"区间缺少 days 参数：{w!r}")
        days = w["days"]
        if days is None:
            subset = ordered
        else:
            start = now_date - timedelta(days=int(days))
            subset = [p for p in ordered if _as_date(p.period) >= start]
        if not subset:
            raise OperatorError(f"区间 {w.get('label')!r} 内没有任何观测，无法计算分位")
        hits = sum(1 for p in subset if p.value <= latest.value)
        out.append(round(hits / len(subset) * 100, 2))
    return out


# --------------------------------------------------------------------------- #
# 算子 2：利差（长端 − 短端）
# --------------------------------------------------------------------------- #


def spread(long_series: list[SeriesPoint], short_series: list[SeriesPoint]) -> list[SeriesPoint]:
    """两条序列逐点相减。**只取两者都有的期间**。

    为什么取交集而不是并集：并集会在缺的一侧填入某个值——那就是插值，
    而 PRD 的 E5 明确禁止「抓不到时的替代值」。宁可少几点，不许造一点。
    """
    short_map = {p.period: p.value for p in short_series}
    out = [
        SeriesPoint(p.period, round(p.value - short_map[p.period], 4))
        for p in sorted(long_series, key=lambda x: x.period)
        if p.period in short_map
    ]
    if not out:
        raise OperatorError("两条序列没有共同期间，无法计算利差")
    return out


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #

OPERATORS: dict[str, Callable[..., Any]] = {
    "percentile-rank": percentile_rank,
    "spread": spread,
}
"""算子名 → 实现。映射表只能引用这里存在的名字。"""


def apply_operator(
    name: str,
    *,
    series: list[Observation],
    params: dict[str, Any],
    extra_series: dict[str, list[Observation]] | None = None,
) -> list[float] | list[SeriesPoint]:
    if name not in OPERATORS:
        raise OperatorError(f"未注册的算子：{name!r}（可选 {'/'.join(sorted(OPERATORS))}）")

    if name == "percentile-rank":
        return percentile_rank(to_points(series), params.get("windows") or [])

    if name == "spread":
        minus = params.get("minus")
        if not minus:
            raise OperatorError("spread 算子必须给出 minus（被减数指标 id）")
        pool = extra_series or {}
        if minus not in pool:
            raise OperatorError(f"spread 需要 {minus} 的序列，但本次运行没有取到它")
        return spread(to_points(pool[minus]), to_points(series))

    raise OperatorError(f"算子 {name!r} 已注册但没有实现分支——注册表与实现不同步")
