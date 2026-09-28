"""交叉校验三手段（PRD §3.5 / §4.6 F5）。

一条贯穿全文的原则
------------------
**检查跑不了，不等于检查通过了。**

如果声明的校验因为"数据还没攒够"而无法执行，结果必须是 `unavailable`，
并且**在分级里落成黄级**——绝不静默跳过。这条原则是"不放宽标准"最容易被侵蚀的地方：
一个 `except: pass` 就能让所有校验变成装饰品，而且没有任何测试会发现。

首版实况（详见 `docs/漏斗与分级设计.md` §4）
------------------------------------------
| 手段 | 首版是否可用 | 说明 |
|---|---|---|
| C1 跨源对照 | ❌ **无任何指标可用** | 首版三个指标各只有一个可达源（`docs/数据源清单.md` §6 已确认无第二源） |
| C2 同源多口径互校 | ✅ 两个指标可用，**均已实测** | PMI 的水平值×同比；收益率的日度月均值×月度值 |
| C3 派生公式复核 | ✅ 全部可用 | 利差、分位 |
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from .contract import Caliber, Observation
from .operators import OperatorError, SeriesPoint, to_points

Status = Literal["pass", "fail", "unavailable"]


@dataclass(frozen=True)
class CheckOutcome:
    """一次校验的结论。**必须能回答"为什么没通过"以及"差多少"。**"""

    means: str
    kind: str
    status: Status
    detail: str
    deviation: float | None = None
    tolerance: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == "pass"


def _month_of(period: str) -> str:
    return period[:7]


def _as_date(period: str) -> date:
    if len(period) == 10:
        return date.fromisoformat(period)
    if "Q" in period:
        year, q = period.split("-Q")
        return date(int(year), (int(q) - 1) * 3 + 1, 1)
    return date(int(period[:4]), int(period[5:7]), 1)


# --------------------------------------------------------------------------- #
# C1 跨源对照
# --------------------------------------------------------------------------- #


def cross_source(
    indicator: str,
    primary: list[Observation],
    secondary: list[Observation],
    tolerance: float,
) -> CheckOutcome:
    """两个**独立发布**的源逐点比对。取交集期间，超出容差即 fail。

    首版没有指标能走这条路（没有真独立第二源）。功能照做，是为了让
    "以后拿到第二个源"变成一次配置改动，而不是一次开发。
    """
    sec = {o.period: o.value for o in secondary}
    common = [(o.period, o.value, sec[o.period]) for o in primary if o.period in sec]
    if not common:
        return CheckOutcome("C1", "cross-source", "unavailable",
                            f"{indicator} 的两个源没有共同期间，无法对照")

    worst = max(abs(a - b) for _, a, b in common)
    period_worst = max(common, key=lambda t: abs(t[1] - t[2]))
    if worst <= tolerance:
        return CheckOutcome("C1", "cross-source", "pass",
                            f"两源在 {len(common)} 个共同期间内一致，最大偏差 {worst:.4f}",
                            worst, tolerance)
    return CheckOutcome(
        "C1", "cross-source", "fail",
        f"{period_worst[0]}：一源 {period_worst[1]}，另一源 {period_worst[2]}，"
        f"差 {abs(period_worst[1] - period_worst[2]):.4f}（容差 {tolerance}）",
        worst, tolerance,
    )


# --------------------------------------------------------------------------- #
# C2 同源多口径互校
# --------------------------------------------------------------------------- #


def caliber_identity(
    indicator: str,
    level_series: list[Observation],
    yoy_series: list[Observation],
    tolerance: float,
) -> CheckOutcome:
    """水平值 × 同比百分比 自洽。

    恒等式：

        同比%  ==  (本期水平 − 去年同期水平) / 去年同期水平 × 100

    **实测（东方财富 224 条真实记录）**：可检验 212 条（前 12 个月没有去年同期），
    全部零偏差，最大偏差 0.0000。

    它能抓住什么：源自身前后矛盾。例如回改了历史水平值却漏改同比字段——
    那会让"本期与去年同期之差"与源自己给出的同比对不上。
    这类错误在任何单口径检查里都看不出来。
    """
    levels = {o.period: o.value for o in level_series}
    yoys = {o.period: o.value for o in yoy_series}

    checked: list[tuple[str, float, float]] = []
    for period, yoy in sorted(yoys.items()):
        if len(period) != 7:
            continue
        year, month = int(period[:4]), int(period[5:7])
        prev = f"{year - 1}-{month:02d}"
        cur, base = levels.get(period), levels.get(prev)
        if cur is None or base in (None, 0):
            continue
        recomputed = (cur - base) / base * 100
        checked.append((period, recomputed, yoy))

    if not checked:
        return CheckOutcome(
            "C2", "caliber-identity", "unavailable",
            f"{indicator} 缺少可配对的水平值/同比观测（需要至少 13 个月），本次无法执行",
        )

    worst = max(abs(rec - given) for _, rec, given in checked)
    bad = max(checked, key=lambda t: abs(t[1] - t[2]))
    if worst <= tolerance:
        return CheckOutcome("C2", "caliber-identity", "pass",
                            f"{len(checked)} 个期间自洽，最大偏差 {worst:.4f}",
                            worst, tolerance)
    return CheckOutcome(
        "C2", "caliber-identity", "fail",
        f"{bad[0]}：按水平值重算同比为 {bad[1]:.4f}，源自报 {bad[2]:.4f}，"
        f"差 {abs(bad[1] - bad[2]):.4f}（容差 {tolerance}）——源的自身数据不一致",
        worst, tolerance,
    )


def daily_monthly_mean(
    indicator: str,
    daily: list[Observation],
    monthly: list[Observation],
    tolerance_bp: float,
) -> CheckOutcome:
    """日度序列的月均值 ↔ 该月的月度值。

    FRED 同时提供日度序列（如 `DGS10`）与月度序列（如 `GS10`，即前者的月平均），
    这构成一个真正的同源多口径。

    **实测（2025-01 ~ 2026-08，20 个月）**：最大偏差 **0.48 bp**、平均 0.24 bp，
    容差 2 bp 内通过。残余偏差来自月度序列只保留两位小数。
    """
    by_month: dict[str, list[float]] = {}
    for o in daily:
        by_month.setdefault(_month_of(o.period), []).append(o.value)

    monthly_map = {o.period: o.value for o in monthly}
    compared: list[tuple[str, float, float]] = []
    for month in sorted(by_month):
        if month not in monthly_map:
            continue
        mean = sum(by_month[month]) / len(by_month[month])
        compared.append((month, mean, monthly_map[month]))

    if not compared:
        return CheckOutcome(
            "C2", "daily-monthly-mean", "unavailable",
            f"{indicator} 的日度与月度序列没有重叠月份，本次无法执行",
        )

    worst_bp = max(abs(m - v) * 100 for _, m, v in compared)
    bad = max(compared, key=lambda t: abs(t[1] - t[2]))
    if worst_bp <= tolerance_bp:
        return CheckOutcome(
            "C2", "daily-monthly-mean", "pass",
            f"{len(compared)} 个月一致，最大偏差 {worst_bp:.2f} bp",
            worst_bp, tolerance_bp,
        )
    return CheckOutcome(
        "C2", "daily-monthly-mean", "fail",
        f"{bad[0]}：日度月均 {bad[1]:.4f}，月度值 {bad[2]:.2f}，"
        f"差 {abs(bad[1] - bad[2]) * 100:.2f} bp（容差 {tolerance_bp}）",
        worst_bp, tolerance_bp,
    )


# --------------------------------------------------------------------------- #
# C3 派生公式复核
# --------------------------------------------------------------------------- #


def derived_recompute(
    indicator: str,
    operator: str,
    *,
    series: list[Observation],
    params: dict[str, Any],
    proposed: list[dict[str, Any]],
    tolerance: float,
    extra_series: dict[str, list[Observation]] | None = None,
) -> CheckOutcome:
    """用算子重算一遍，与候选里的值逐点比对。

    这是"派生可复现"的机器化落点：**同一条公式、同一份输入，必须得到同一个结果**。
    若不一致，说明要么算子变了、要么候选里的值是手写的——两种都必须人看。
    """
    if not proposed:
        return CheckOutcome("C3", operator, "unavailable",
                            f"{indicator} 本次没有派生值可复核")

    try:
        if operator == "percentile-rank":
            from .operators import percentile_rank  # noqa: PLC0415

            recomputed: Any = percentile_rank(to_points(series), params.get("windows") or [])
        elif operator == "spread":
            from .operators import apply_operator  # noqa: PLC0415

            recomputed = apply_operator(
                "spread", series=series, params=params, extra_series=extra_series
            )
        else:
            return CheckOutcome("C3", operator, "unavailable",
                                f"算子 {operator!r} 没有复核分支，本次无法执行")
    except OperatorError as exc:
        return CheckOutcome("C3", operator, "unavailable", f"算子无法执行：{exc}")

    # ⚠️ 点序列算子（spread）**必须按期间比对，不能按位置**。
    # 算子重算的是整条派生序列（实测 273 个交易日），而候选只是其中一个窗口（12 个月）。
    # 按位置比会得到"点数不一致：候选 12 点，重算 273 点"——把一次正确的计算报成失败，
    # 而且这条 fail 会降黄，于是这个指标**永久不可能变绿**。
    if recomputed and isinstance(recomputed[0], SeriesPoint):
        by_month = {p.period[:7]: p.value for p in recomputed}
        pairs: list[tuple[str, float, float]] = []
        for item in proposed:
            if not isinstance(item, dict):
                continue
            period = str(item.get("period") or "")
            if period[:7] in by_month:
                pairs.append((period, float(item["value"]), by_month[period[:7]]))
        if not pairs:
            return CheckOutcome(
                "C3", operator, "unavailable",
                f"{indicator} 候选里的期间在重算结果里一个都对不上，本次无法复核",
            )
        worst = max(abs(a - b) for _, a, b in pairs)
        bad = max(pairs, key=lambda t: abs(t[1] - t[2]))
        if worst <= tolerance:
            return CheckOutcome("C3", operator, "pass",
                                f"{len(pairs)} 个派生值可复现（按期间比对），最大偏差 {worst:.4f}",
                                worst, tolerance)
        return CheckOutcome(
            "C3", operator, "fail",
            f"{indicator} {bad[0]} 对不上：候选 {bad[1]}，按 {params} 重算 {bad[2]:.4f}"
            f"（容差 {tolerance}）",
            worst, tolerance,
        )

    given = [float(p["value"]) if isinstance(p, dict) else float(p) for p in proposed]
    if len(given) != len(recomputed):
        return CheckOutcome(
            "C3", operator, "fail",
            f"{indicator} 点数不一致：候选 {len(given)} 点，重算 {len(recomputed)} 点",
            None, tolerance,
        )

    worst = max((abs(a - b) for a, b in zip(given, recomputed)), default=0.0)
    if worst <= tolerance:
        return CheckOutcome("C3", operator, "pass",
                            f"{len(given)} 个派生值可复现，最大偏差 {worst:.4f}",
                            worst, tolerance)
    idx = max(range(len(given)), key=lambda i: abs(given[i] - recomputed[i]))
    return CheckOutcome(
        "C3", operator, "fail",
        f"{indicator} 第 {idx + 1} 个派生值对不上：候选 {given[idx]}，重算 {recomputed[idx]}"
        f"（容差 {tolerance}）",
        worst, tolerance,
    )


def summarize(outcomes: list[CheckOutcome]) -> tuple[Status, str]:
    """把多条校验汇成一个结论。

    取"最坏"：只要有 fail 就是 fail；全是 unavailable 也算 unavailable。
    **不能因为"有一条通过了"就当成整体通过** —— 那正是"用一条好消息掩盖两条坏消息"。
    """
    if not outcomes:
        return "unavailable", "该指标未声明任何校验手段"
    if any(o.status == "fail" for o in outcomes):
        worst = next(o for o in outcomes if o.status == "fail")
        return "fail", worst.detail
    if any(o.status == "unavailable" for o in outcomes):
        un = next(o for o in outcomes if o.status == "unavailable")
        return "unavailable", un.detail
    return "pass", "；".join(o.detail for o in outcomes)
