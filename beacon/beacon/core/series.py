"""序列降采样与数值格式化。

两件事都是**幂等性的地基**（PRD 测试⑦：同批响应跑两次，候选逐字节一致）。

降采样为什么不是优化而是准入条件
--------------------------------
实测中证指数一次返回 **2428 条**日度数据（2016-09 至今）。而一个内容槽位的目标
是 ≤ 60 个点——68 个概念全长约 2900 行，单个概念塞 2428 个点，一条就超过全库。

取样规则为什么是「按期取最后一个」
----------------------------------
另一种做法是等距抽稀（每隔 k 个取一个）。两种都能把点数压下来，但只有前者能用：

| | 按期取最后一个 | 等距抽稀 |
|---|---|---|
| 取样点落在哪 | 每月最后一个交易日——**语义确定** | 每月 8 号、17 号……**语义随数据长度漂移** |
| 新增一个月数据 | 末尾追加一点，其余不动 | **所有取样点全部漂移** |
| 是否幂等 | 是（期间的函数） | 否（序列长度的函数） |

第二种在做 diff 时会显示"整条序列都变了"——而实际上只多了一个月。
那会让人审文件彻底失去意义。

所以规则是 `last-in-period`，只有在点数仍超上限时才退化为等距抽稀，
且**强制保留最新一点**（抽稀把"最新"丢掉，等于把这次更新的意义丢掉）。
"""

from __future__ import annotations

import math
from datetime import date

from .contract import Observation


def period_key_of(period: str) -> str:
    """把一个期间归到它的「期」——用于 last-in-period 取样。

    * 日度 `2026-09-22` → `2026-09`
    * 月度 `2026-09`    → `2026-09`
    * 季度 `2026-Q3`    → `2026-Q3`
    """
    if "Q" in period:
        return period
    if len(period) == 7:
        return period
    return period[:7]


def sample_last_in_period(series: list[Observation]) -> list[Observation]:
    """每期取最后一个观测。输入必须已按期间升序排列。"""
    out: dict[str, Observation] = {}
    for obs in series:
        out[period_key_of(obs.period)] = obs  # 升序遍历 → 后写覆盖前写 = 最后一个
    return [out[k] for k in sorted(out)]


def thin_evenly(series: list[Observation], max_points: int, *, keep_latest: bool) -> list[Observation]:
    """等距抽稀到至多 `max_points` 点。

    `keep_latest=True` 时**替换**最后一个取样点为真正的最后一点——
    否则"最新"可能正好被抽掉，而"最新"恰恰是这次更新的全部意义。
    """
    if len(series) <= max_points:
        return list(series)
    if max_points <= 0:
        return []

    step = len(series) / max_points
    picked = [series[min(int(i * step), len(series) - 1)] for i in range(max_points)]

    # 去重：步长取整后可能重复同一位置
    seen: set[str] = set()
    unique: list[Observation] = []
    for o in picked:
        if o.period not in seen:
            seen.add(o.period)
            unique.append(o)

    if keep_latest and unique and unique[-1].period != series[-1].period:
        unique[-1] = series[-1]
    return unique


def sample_to_periods(
    series: list[Observation],
    *,
    sample: str = "last-in-period",
) -> list[Observation]:
    """只做「按期取样」，不做上限裁剪。**同一输入必须给出同一输出。**"""
    if not series:
        return []
    ordered = sorted(series, key=lambda o: o.period)

    if sample == "last-in-period":
        return sample_last_in_period(ordered)
    if sample == "none":
        return ordered
    raise ValueError(f"未知的取样方式：{sample!r}（可选 last-in-period / none）")


def downsample(
    series: list[Observation],
    *,
    sample: str = "last-in-period",
    max_points: int = 60,
    keep_latest: bool = True,
) -> list[Observation]:
    """取样 + 上限裁剪。

    ⚠️ **顺序不能反。** 曾经把 `max_points` 作用在整条原始序列上，
    后果很具体：PMI 有 224 个月的历史，先裁到 60 点会被均匀稀释到 18 年跨度，
    再"取最近 6 期"得到的是一个横跨两年的稀疏窗口（实测取到了 2024-10）。
    正确的顺序是：**先按期取样 → 再取内容窗口 → 只有窗口本身超上限时才裁剪**。
    `max_points` 的定位是"防 `periods` 写错"的兜底，不是选点规则。
    """
    return thin_evenly(
        sample_to_periods(series, sample=sample),
        max_points,
        keep_latest=keep_latest,
    )


def format_number(value: float) -> str:
    """数值 → 可写进文档的字符串。**幂等性的必要条件。**

    为什么不直接用 `repr()`：浮点的 repr 在不同 Python 版本间不保证一致，
    而且真会写出 `49.79999999999999` 这种东西。统一走这里：

    * 保留最多 4 位有效小数
    * 去掉尾随零（`49.80` → `49.8`，`16.0` → `16`）
    * 整数不带小数点
    * 拒绝 NaN / Infinity（写进文档就再也解析不回来了）
    """
    if not math.isfinite(value):
        raise ValueError(f"不能把非有限数值写进文档：{value!r}")

    rounded = round(float(value), 4)
    if rounded == int(rounded):
        return str(int(rounded))
    text = f"{rounded:.4f}".rstrip("0").rstrip(".")
    return text


def coerce_number(raw: object) -> float:
    """从文档里读出来的数值统一成 float。

    文档里可能写着 `16`（int）或 `16.0`（float）或 `"16"`（字符串，人写的）。
    比较时三者必须等价，否则"旧值 16 vs 新值 16.0"会被报成一次变更。
    """
    if isinstance(raw, bool):
        raise ValueError(f"布尔值不能当数值用：{raw!r}")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError as exc:
            raise ValueError(f"无法解析为数值：{raw!r}") from exc
    raise ValueError(f"无法解析为数值：{raw!r}（类型 {type(raw).__name__}）")


def match_number_type(old: object, new: float) -> float | int:
    """按旧值的类型决定新值写 int 还是 float。

    **工具只改值，不改类型。** 文档里原本写着 `[88, 72, 54, 46]` 时，
    写成 `[88.0, 72.0, …]` 会让 diff 里多出一堆纯格式差异，
    也会让下游拿到与之前不同类型的值——两件都不是这次更新的目的。

    只在"旧值是整数、且新值恰好也是整数"时保留 int；其余一律 float。
    """
    if isinstance(old, bool):
        return float(new)
    if isinstance(old, int) and float(new).is_integer():
        return int(new)
    return float(new)


def match_number_type(old: object, new: float) -> float | int:
    """按旧值的类型决定新值写 int 还是 float。

    **工具只改值，不改类型。** 文档里原本写着 `[88, 72, 54, 46]` 时，
    写成 `[88.0, 72.0, …]` 会让 diff 里多出一堆纯格式差异，
    也会让下游拿到与之前不同类型的值——两件都不是这次更新的目的。

    只在"旧值是整数、且新值恰好也是整数"时保留 int；其余一律 float。
    """
    if isinstance(old, bool):
        return float(new)
    if isinstance(old, int) and float(new).is_integer():
        return int(new)
    return float(new)


def period_label(period: str, granularity: str) -> str:
    """期间 → 图表刻度标签。**是期间的直接函数**，不含解释。"""
    if granularity == "month" and len(period) == 10:
        return period[:7]
    return period


def latest_period_of(series: list[Observation]) -> str | None:
    return max((o.period for o in series), default=None)


def as_of_date(series: list[Observation]) -> date | None:
    """「内容截至」用哪个日期。

    PRD §4.3 明确：**是数据的实际发布日，不是抓取日**。
    这里取该序列最新一条的 `published_at`；没有就退到它的期间（日度）或 None。
    刻意**不**退到"今天"——那会把"数据很旧"掩盖成"刚刚更新过"。
    """
    if not series:
        return None
    latest = max(series, key=lambda o: o.period)
    if latest.source.published_at:
        return latest.source.published_at
    if len(latest.period) == 10:
        return date.fromisoformat(latest.period)
    return None
