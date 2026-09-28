"""七层漏斗（PRD §4.1 F1–F7）。

每一层都是**独立可测的纯函数**：输入一批观测，输出"通过的部分 + 丢弃的理由"。
最后一层才把结果变成候选。

为什么要把"丢弃的理由"一路带下来
--------------------------------
PRD A-C18 要求人审文件"不打开代码就能读懂"。但人读的时候一定会问
**"为什么少了一个指标"**——如果丢弃只发生在代码里、只写进日志，
这个问题在产物里就无解。所以每一层的丢弃都必须带 `layer` 与 `reason`，
并最终出现在人审文件里。

一条贯穿全层的原则
------------------
**宁可丢弃，不许造值。** 抓不到就少一条，绝不用旧值填充、不插值、不取平均。
空的数值比没有数值危险得多——它会被下游当成"数据就是 0"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .candidate import (
    Candidate,
    ChangeSet,
    FieldChange,
    SourceFailure,
    changed_paths_outside_whitelist,
    compute_label_change,
    compute_scalar_change,
    compute_series_change,
    is_illustrative_slot,
)
from .config import Mapping, MappingTable, Settings, Thresholds
from .contract import Caliber, Observation, Tier
from .crosscheck import (
    CheckOutcome,
    caliber_identity,
    cross_source,
    daily_monthly_mean,
    derived_recompute,
    summarize,
)
from .fingerprint import fingerprint
from .fingerprint import fingerprint
from .flags import FlagsBook
from .leveling import Level, LevelInputs, decide
from .operators import apply_operator, OperatorError
from .path import PathError, PathMissing, clone, get as path_get, set_value
from .series import (
    as_of_date,
    coerce_number,
    downsample,
    format_number,
    latest_period_of,
    match_number_type,
    period_label,
    sample_to_periods,
    thin_evenly,
)
from .store import TargetStore

LAYER_TITLES = {
    "F1": "源级准入",
    "F2": "时效筛选",
    "F3": "结构校验",
    "F4": "合理性校验",
    "F5": "交叉校验",
    "F6": "适用性白名单",
    "F7": "分级闸门",
}


@dataclass(frozen=True)
class Drop:
    """一条被丢弃的观测。**它是产物的一部分，不是日志。**"""

    layer: str
    indicator: str
    reason: str
    period: str | None = None
    count: int = 1

    def line(self) -> str:
        where = f"{self.indicator}" + (f"@{self.period}" if self.period else "")
        n = f"（{self.count} 条）" if self.count > 1 else ""
        return f"[{self.layer} {LAYER_TITLES.get(self.layer, '')}] {where}：{self.reason}{n}"


@dataclass
class FunnelContext:
    mappings: MappingTable
    thresholds: Thresholds
    settings: Settings
    flags: FlagsBook
    store: TargetStore
    today: date
    generated_at: datetime
    failures: list[SourceFailure] = field(default_factory=list)


@dataclass
class FunnelOutcome:
    changeset: ChangeSet
    drops: list[Drop]

    def drops_by_layer(self) -> dict[str, list[Drop]]:
        out: dict[str, list[Drop]] = {}
        for d in self.drops:
            out.setdefault(d.layer, []).append(d)
        return out


# --------------------------------------------------------------------------- #
# 期间工具
# --------------------------------------------------------------------------- #


def period_end(period: str) -> date:
    """期间的最后一天。用于「已发布的完整期间」判定与发布惯例。"""
    if len(period) == 10:
        return date.fromisoformat(period)
    if "Q" in period:
        year, q = period.split("-Q")
        month = int(q) * 3
        return _month_end(int(year), month)
    year, month = int(period[:4]), int(period[5:7])
    return _month_end(year, month)


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def is_period_complete(period: str, today: date) -> bool:
    """期间是否已经走完。

    只在"期间结束日早于今天"时才算完成。这意味着**当月的月度数据永远不采**——
    统计局在月末才发布，月中拿到的"当月值"只可能是一个进行中的估计。
    """
    return period_end(period) < today


# --------------------------------------------------------------------------- #
# F1 源级准入
# --------------------------------------------------------------------------- #


def f1_admit(
    observations: list[Observation], ctx: FunnelContext
) -> tuple[list[Observation], list[Drop]]:
    """未登记的源、L3 源一律丢弃。

    源级失败（网络/结构）在适配器层就变成 `SourceError` 并进入 `ctx.failures`，
    所以这一层只需处理"数据到手了但源不该用"的情形。
    """
    kept: list[Observation] = []
    dropped: list[Drop] = []
    bad: dict[str, int] = {}

    for obs in observations:
        if obs.source.tier is Tier.L3:
            bad[obs.indicator] = bad.get(obs.indicator, 0) + 1
            continue
        kept.append(obs)

    for indicator, n in sorted(bad.items()):
        dropped.append(
            Drop("F1", indicator, "来源层级为 L3（媒体线索），不得作为内容来源", count=n)
        )
    return kept, dropped


# --------------------------------------------------------------------------- #
# F2 时效筛选
# --------------------------------------------------------------------------- #


def f2_timeliness(
    observations: list[Observation], ctx: FunnelContext
) -> tuple[list[Observation], list[Drop]]:
    kept: list[Observation] = []
    dropped: list[Drop] = []
    incomplete: dict[str, int] = {}
    future: dict[str, int] = {}

    for obs in observations:
        if not is_period_complete(obs.period, ctx.today):
            incomplete[obs.indicator] = incomplete.get(obs.indicator, 0) + 1
            continue
        pub = obs.source.published_at
        if pub is not None and pub > ctx.today:
            future[obs.indicator] = future.get(obs.indicator, 0) + 1
            continue
        kept.append(obs)

    for ind, n in sorted(incomplete.items()):
        dropped.append(
            Drop("F2", ind, f"期间尚未走完（今天 {ctx.today}），不采进行中的期间", count=n)
        )
    for ind, n in sorted(future.items()):
        dropped.append(Drop("F2", ind, f"发布日晚于今天（{ctx.today}），不采未来日期", count=n))

    return kept, dropped


# --------------------------------------------------------------------------- #
# F3 结构校验
# --------------------------------------------------------------------------- #


def resolve_published_at(obs: Observation, ctx: FunnelContext) -> date | None:
    """确定「发布日」。

    优先用源给的；源没给时，按配置里**显式声明的发布惯例**推算。
    惯例必须写明（如 `period_end`），因为这是一条约定的、可核对的事实；
    工具不得自己发明一条——那属于编造（PRD §4.9 E5）。
    """
    if obs.source.published_at:
        return obs.source.published_at
    convention = ctx.thresholds.convention_for(obs.indicator)
    if convention == "period_end":
        return period_end(obs.period)
    return None


def f3_structure(
    observations: list[Observation], ctx: FunnelContext
) -> tuple[list[Observation], list[Drop]]:
    kept: list[Observation] = []
    dropped: list[Drop] = []
    buckets: dict[tuple[str, str], list[str]] = {}

    def note(layer_indicator: str, reason: str) -> None:
        buckets.setdefault((layer_indicator, reason), []).append("")

    for obs in observations:
        if obs.unit not in ctx.thresholds.allowed_units:
            note(obs.indicator, f"单位 {obs.unit!r} 不在允许表内")
            continue
        if not (obs.source.name and obs.source.url and obs.provenance.fetched_at):
            note(obs.indicator, "缺少溯源信息（来源 / URL / 抓取时间）")
            continue
        if resolve_published_at(obs, ctx) is None:
            note(
                obs.indicator,
                "源未提供发布日，且配置里没有声明该指标的发布惯例"
                "（thresholds.publication_convention）",
            )
            continue
        kept.append(obs)

    for (ind, reason), items in sorted(buckets.items()):
        dropped.append(Drop("F3", ind, reason, count=len(items)))
    return kept, dropped


# --------------------------------------------------------------------------- #
# F4 合理性校验
# --------------------------------------------------------------------------- #


@dataclass
class JumpFinding:
    exceeded: bool
    detail: str = ""


def f4_value_range(
    observations: list[Observation], ctx: FunnelContext
) -> tuple[list[Observation], list[Drop]]:
    kept: list[Observation] = []
    dropped: list[Drop] = []
    buckets: dict[tuple[str, str], int] = {}

    for obs in observations:
        rng = ctx.thresholds.range_for(obs.indicator)
        if rng is None:
            buckets[(obs.indicator, "该指标未声明值域，无法判定合理性")] = (
                buckets.get((obs.indicator, "该指标未声明值域，无法判定合理性"), 0) + 1
            )
            continue
        low, high, desc = rng
        if not (low <= obs.value <= high):
            key = (obs.indicator, f"值 {obs.value} 超出值域 [{low}, {high}]（{desc}）")
            buckets[key] = buckets.get(key, 0) + 1
            continue
        kept.append(obs)

    for (ind, reason), n in sorted(buckets.items()):
        dropped.append(Drop("F4", ind, reason, count=n))
    return kept, dropped


def f4_jump(
    old_values: list[Any],
    new_values: list[float],
    labels: list[str],
    ctx: FunnelContext,
    indicator: str,
) -> JumpFinding:
    """跳变检查：**这次更新把写入内容的值改动了多少**。

    ⚠️ 刻意**不用整条抓取序列**来判断。曾经那样做过，后果很具体：
    `cn.index.pe.000300` 一次返回 2400 个日度点，十年里必然有某一天涨跌超过 10%，
    于是**每一次运行都会命中跳变**、每条候选永久是黄级——这个级别从此不再携带信息。

    跳变的语义是"找出值得人多看一眼的地方"。对读内容的人来说，
    值得看的是**这次改动**，不是历史序列里某个早已过去的日子。

    阈值也按**写进内容的那个量**选档（见 `Thresholds.jump_for`）：
    PE 抓的是"市盈率"、写进内容是"历史分位"，两者的合理波动幅度差一个量级。
    """
    spec = ctx.thresholds.jump_for(indicator)
    if not spec:
        return JumpFinding(False)

    # 只比"两边都有的位置"。长度不同说明点数结构变了，
    # 那是结构性变更（另有规则管），不该在这里被报成一次跳变。
    pairs: list[tuple[str, float, float]] = []
    for i, new in enumerate(new_values):
        if i >= len(old_values):
            continue
        try:
            old = coerce_number(old_values[i])
        except ValueError:
            continue
        label = labels[i] if i < len(labels) else f"第 {i + 1} 个值"
        pairs.append((label, old, float(new)))
    if not pairs:
        return JumpFinding(False)

    worst_abs = 0.0
    worst_pct = 0.0
    where = ""
    for label, old, new in pairs:
        diff = abs(new - old)
        if diff > worst_abs:
            worst_abs, where = diff, label
        if old:
            worst_pct = max(worst_pct, abs((new - old) / old * 100))

    if "max_change" in spec and worst_abs > float(spec["max_change"]):
        return JumpFinding(
            True,
            f"最大变动 {worst_abs:.4f} {spec.get('unit', '')}"
            f"（阈值 {spec['max_change']}，{where}）",
        )
    if "max_pct_change" in spec and worst_pct > float(spec["max_pct_change"]):
        return JumpFinding(
            True,
            f"最大相对变动 {worst_pct:.2f}%（阈值 {spec['max_pct_change']}%，{where}）",
        )
    return JumpFinding(False)


def f4_caliber(
    series: list[Observation], mapping: Mapping
) -> list[Drop]:
    """口径标记必须与映射声明的**一致**。

    这是防"拿错序列"的最直接手段：累计值当成了当期值、同比当成了水平值，
    数值可能依然落在值域内，但含义完全变了。
    """
    if not mapping.expected_caliber:
        return []
    want = Caliber(mapping.expected_caliber)
    wrong = [o for o in series if o.caliber is not want]
    if not wrong:
        return []
    got = "、".join(sorted({o.caliber.value for o in wrong}))
    return [
        Drop(
            "F4",
            mapping.indicator,
            f"口径不符：映射声明 {want.value}，实际拿到 {got}——"
            f"这通常意味着抓错了序列",
            count=len(wrong),
        )
    ]


def effective_as_of(series: list[Observation], ctx: FunnelContext) -> date | None:
    """整条序列的「内容截至」= 最新一条观测的发布日（经发布惯例解析）。

    刻意**不用抓取日兜底**：那会把"数据很旧"掩盖成"刚刚更新过"（PRD §4.3）。
    """
    if not series:
        return None
    latest = max(series, key=lambda o: o.period)
    return resolve_published_at(latest, ctx)


def missing_periods(series: list[Observation]) -> tuple[str, ...]:
    """找出缺期。只对月度序列做（日度序列天然跳周末与节假日，不是"缺期"）。"""
    periods = sorted(o.period for o in series)
    if not periods or any(len(p) != 7 for p in periods):
        return ()
    gaps: list[str] = []
    for prev, cur in zip(periods, periods[1:]):
        y1, m1 = int(prev[:4]), int(prev[5:7])
        y2, m2 = int(cur[:4]), int(cur[5:7])
        step = (y2 - y1) * 12 + (m2 - m1)
        for k in range(1, step):
            mm = m1 + k
            yy = y1 + (mm - 1) // 12
            mm = (mm - 1) % 12 + 1
            gaps.append(f"{yy}-{mm:02d}")
    return tuple(gaps)


# --------------------------------------------------------------------------- #
# F5 交叉校验
# --------------------------------------------------------------------------- #


def f5_checks(
    mapping: Mapping,
    series: list[Observation],
    pool: dict[str, list[Observation]],
    ctx: FunnelContext,
    *,
    proposed_values: list[Any] | None = None,
) -> list[CheckOutcome]:
    outcomes: list[CheckOutcome] = []
    tol = ctx.thresholds.tolerance_for(mapping.indicator)

    for means in mapping.checks:
        if means == "C1":
            secondary = pool.get(f"{mapping.indicator}#secondary") or []
            outcomes.append(
                cross_source(
                    mapping.indicator, series, secondary, tol[0] if tol else 0.1
                )
            )

        elif means == "C2":
            spec = mapping.checks_extra.get("c2") or {}
            kind = spec.get("kind")
            tol_value = ctx.thresholds.tolerance.get(spec.get("tolerance_key", "percent"), 0.1)
            if kind == "caliber-identity":
                other = spec.get("other_caliber", "yoy")
                peer = [
                    o
                    for o in pool.get(mapping.indicator, [])
                    if o.caliber.value == other
                ] or pool.get(f"{mapping.indicator}.{other}", [])
                outcomes.append(
                    caliber_identity(mapping.indicator, series, peer, tol_value)
                )
            elif kind == "daily-monthly-mean":
                monthly_indicator = spec.get("monthly_indicator")
                monthly = pool.get(monthly_indicator or "", [])
                outcomes.append(
                    daily_monthly_mean(
                        mapping.indicator,
                        series,
                        monthly,
                        tol[0] if tol else 2.0,
                    )
                )
            else:
                outcomes.append(
                    CheckOutcome("C2", str(kind), "unavailable",
                                 f"未实现的 C2 形式 {kind!r}，本次无法执行")
                )

        elif means == "C3":
            if not mapping.transform.operator:
                outcomes.append(
                    CheckOutcome("C3", "derive", "unavailable",
                                 "该指标未声明派生算子，无可复核的派生量")
                )
                continue
            outcomes.append(
                derived_recompute(
                    mapping.indicator,
                    mapping.transform.operator,
                    series=series,
                    params=mapping.transform.params,
                    proposed=proposed_values or [],
                    tolerance=tol[0] if tol else 0.5,
                    extra_series=pool,
                )
            )

    return outcomes


# --------------------------------------------------------------------------- #
# F6 适用性白名单 + 取值
# --------------------------------------------------------------------------- #


def operator_inputs(mappings: MappingTable) -> set[str]:
    """被派生算子当作输入使用的指标（如利差的被减数）。

    它们**不进内容**，但也不是"被丢弃"——是"被用掉了"。
    这两件事在产物里必须能区分：若一律报「不在映射表里」，
    读到的人会去找为什么一个明显参与了计算的指标被丢掉了。
    """
    used: set[str] = set()
    for mapping in mappings.mappings.values():
        params = mapping.transform.params or {}
        for key in ("minus", "minuend", "monthly_indicator"):
            value = params.get(key)
            if isinstance(value, str) and value:
                used.add(value)
        for key in ("inputs", "series"):
            value = params.get(key)
            if isinstance(value, (list, tuple)):
                used.update(str(v) for v in value)
    return used


def operator_inputs(mappings: MappingTable) -> set[str]:
    """被派生算子当作输入使用的指标（如利差的被减数）。

    它们**不进内容**，但也不是"被丢弃"——是"被用掉了"。
    这两件事在产物里必须能区分：若一律报「不在映射表里」，
    读到的人会去找为什么一个明显参与了计算的指标被丢掉了。
    """
    used: set[str] = set()
    for mapping in mappings.mappings.values():
        params = mapping.transform.params or {}
        for key in ("minus", "minuend", "monthly_indicator"):
            value = params.get(key)
            if isinstance(value, str) and value:
                used.add(value)
        for key in ("inputs", "series"):
            value = params.get(key)
            if isinstance(value, (list, tuple)):
                used.update(str(v) for v in value)
    return used


def content_values(
    mapping: Mapping,
    series: list[Observation],
    ctx: FunnelContext,
    pool: dict[str, list[Observation]] | None = None,
) -> tuple[list[float], list[str]]:
    """算出要写进槽位的值，以及对应的刻度标签。

    三种情形：

    | 情形 | 取值方式 | 刻度标签 |
    |---|---|---|
    | 无算子 | **先降采样，再取最近 `periods` 期** | 期间（如 `2026-08`） |
    | 算子 → 标量列表（分位） | 直接用算子结果 | 映射声明的区间名 |
    | 算子 → 点序列（利差） | **先降采样，再取最近 `periods` 期** | 各点的期间 |

    ⚠️ **降采样必须真的执行。** 曾经只在文档和配置里写了它，代码里从未调用——
    对月度数据看不出问题（一个月本来就一个点），但日度数据会直接出事：
    收益率一次返回 400 个交易日，取"最近 12 期"就变成**最近 12 个交易日**，
    图上只有半个月，而配置里明明写着按月取样。这类"配置说做了、代码没做"
    的缺陷不会报错，只会让人对着图纳闷。

    算子也分两类，不能一律对待：
    `percentile-rank` 需要**整段历史**才能算分位（它的输出长度由窗口数决定）；
    `spread` 是逐点相减，**输出长度等于输入长度**，所以必须降采样。
    """
    params = mapping.transform.params
    n = int(params.get("periods") or ctx.settings.relookback_periods)

    def window(items: list) -> list:
        """取内容窗口：**先按期取样 → 再取最近 n 期 → 超上限才裁剪**。

        顺序不能反（见 `series.downsample` 的说明）：上限是"防 periods 写错"的兜底，
        不是选点规则。先削上限会把整段历史稀释掉，再取最近 n 期就成了稀疏窗口。
        """
        sampled = sample_to_periods(items, sample=ctx.settings.sample)
        chosen = sorted(sampled, key=lambda x: x.period)[-n:]
        if len(chosen) > ctx.settings.max_points:
            chosen = thin_evenly(
                chosen, ctx.settings.max_points, keep_latest=ctx.settings.keep_latest
            )
        return chosen

    if not mapping.transform.operator:
        chosen = window(series)
        return (
            [round(o.value, 4) for o in chosen],
            [period_label(o.period, "month") for o in chosen],
        )

    produced = apply_operator(
        mapping.transform.operator,
        series=series,
        params=params,
        extra_series=pool,
    )

    if produced and hasattr(produced[0], "period"):
        # 点序列：与原始序列同样处理
        chosen_pts = window(produced)  # type: ignore[arg-type]
        return (
            [round(float(p.value), 4) for p in chosen_pts],  # type: ignore[attr-defined]
            [period_label(p.period, "month") for p in chosen_pts],  # type: ignore[attr-defined]
        )

    # 标量列表（分位）：长度由窗口数决定，不做降采样
    labels = [str(w.get("label")) for w in params.get("windows") or []]
    return [round(float(v), 4) for v in produced], labels  # type: ignore[arg-type]



# --------------------------------------------------------------------------- #
# F7 构建候选
# --------------------------------------------------------------------------- #


def slot_paths(mapping: Mapping) -> tuple[str, str, str]:
    """从槽位路径推出 (series 路径, 块路径, 块 source 路径)。

    只做通用的"去掉最后一段"运算——core 不认识路径里的名字，
    所以这里不依赖任何具体字段名。
    """
    series_path = mapping.slot.path.rsplit(".", 1)[0]
    block_path = series_path.rsplit(".", 1)[0]
    return series_path, block_path, f"{block_path}.source"


def render_template(template: str, ctx: dict[str, str]) -> str:
    try:
        return template.format(**ctx)
    except KeyError as exc:
        raise PathError(
            f"溯源文字模板引用了未知占位符 {exc.args[0]!r}（模板：{template!r}）"
        ) from exc


def build_candidate(
    mapping: Mapping,
    series: list[Observation],
    pool: dict[str, list[Observation]],
    ctx: FunnelContext,
) -> tuple[Candidate | None, list[Drop]]:
    drops: list[Drop] = []

    if not ctx.store.exists(mapping.target):
        raise PathError(
            f"映射指向不存在的目标：{mapping.target}（指标 {mapping.indicator}）——"
            f"这是配置错误，不是数据问题"
        )

    # 注意：原始文档先留一份，用于判断"槽位当前是不是示意数据"
    original = ctx.store.read(mapping.target)
    if not _path_exists(original, mapping.slot.path):
        raise PathError(
            f"{mapping.target} 里没有 {mapping.slot.path} 这个路径——"
            f"映射与内容对不上，属配置错误（不改内容，直接报错）"
        )

    try:
        values, labels = content_values(mapping, series, ctx, pool)
    except OperatorError as exc:
        drops.append(Drop("F6", mapping.indicator, f"派生算子无法执行：{exc}"))
        return None, drops

    checks = f5_checks(
        mapping, series, pool, ctx, proposed_values=_proposed_payload(values, labels)
    )
    check_status, check_detail = summarize(checks)

    working = clone(original)
    changes: list[FieldChange] = []
    series_path, block_path, block_source_path = slot_paths(mapping)

    # ---- 1) 数值槽位 ----
    old_points = list(path_get(working, mapping.slot.path) or [])
    # 逐元素保持类型：原来的 `[88, 72]` 不该变成 `[88.0, 72.0]`——
    # 那是纯格式差异，会把 diff 弄脏，也会让下游拿到不同类型。
    typed_values = [
        match_number_type(old_points[i], v) if i < len(old_points) else float(v)
        for i, v in enumerate(values)
    ]
    changes.append(compute_series_change(mapping.slot.path, old_points, typed_values))
    set_value(working, mapping.slot.path, typed_values)

    # ---- 2) 刻度标签（期间的直接函数，属溯源文字）----
    if labels and mapping.allows(f"{block_path}.xTicks"):
        old_ticks = path_get(working, f"{block_path}.xTicks")
        if list(old_ticks) != labels:
            changes.append(compute_label_change(f"{block_path}.xTicks", list(old_ticks), labels))
            set_value(working, f"{block_path}.xTicks", labels)

    # ---- 3) 序列名（仅在映射声明了、且路径可写时）----
    name_path = f"{series_path}.name"
    if mapping.slot.series_name and mapping.allows(name_path):
        old_name = path_get(working, name_path)
        if old_name != mapping.slot.series_name:
            changes.append(compute_scalar_change(name_path, old_name, mapping.slot.series_name))
            set_value(working, name_path, mapping.slot.series_name)

    # ---- 4) 溯源文字（只由模板生成）----
    # ⚠️ 必须走 resolve_published_at，不能用 as_of_date 直接读 source.published_at。
    # 后者对"源不提供发布日"的指标会返回 None，于是产物里会出现空的「截至 」——
    # 一个空日期比没有日期更糟：它看起来像是"更新过、刚好没写"。
    as_of = effective_as_of(series, ctx)
    ctx_vars = _template_vars(series, mapping, as_of)
    # 写哪些字段**完全由映射表决定**。
    # `block_source` 是唯一一个不用声明的：它固定落在"槽位所在的那个块"的 source 上，
    # 位置由槽位路径本身推出来。其余两个（修订日期 / 条目级出处）由 text_targets 指定。
    text_writes: list[tuple[str, str]] = [(block_source_path, "block_source")]
    for template_name, field_path in mapping.text_targets.items():
        text_writes.append((field_path, template_name))

    for field_path, key in text_writes:
        if not mapping.allows(field_path) or not _path_exists(working, field_path):
            continue
        new_text = render_template(ctx.mappings.templates[key], ctx_vars)
        old_text = path_get(working, field_path)
        if str(old_text) != new_text:
            changes.append(compute_scalar_change(field_path, old_text, new_text))
            set_value(working, field_path, match_scalar_type(old_text, new_text))

    # ---- 5) 分级 ----
    jump = f4_jump(old_points, values, labels, ctx, mapping.indicator)
    gaps = missing_periods(series)
    flags_here = ctx.flags.get(mapping.target)
    illustrative = is_illustrative_slot(original, mapping)

    decision = decide(
        LevelInputs(
            touched_readonly_field=changed_paths_outside_whitelist(changes, mapping),
            flagged=bool(flags_here),
            flag_summary="；".join(f.summary for f in flags_here),
            first_real_replacement=illustrative,
            jump_exceeded=jump.exceeded,
            jump_summary=jump.detail,
            check_status=check_status,
            check_detail=check_detail,
            missing_periods=gaps,
        )
    )

    suggestions: list[str] = []
    if illustrative:
        suggestions.append(
            "该数据块当前是示意数据或槽位语义不符（现有序列名与映射声明不一致）。"
            "合入前请先确认这个槽位应当承载该指标——必要时先在内容里改好块标题与序列名。"
        )

    # 一条改动都没有 → 不进候选列表。
    # 它仍然要算级别（这样 "为什么它没事" 是可回答的），但没必要让人读一整节。
    effective_changes = [c for c in changes if not c.is_noop]
    if not effective_changes:
        return (
            Candidate(
                target=mapping.target,
                indicator=mapping.indicator,
                level=decision,
                changes=[],
                checks=checks,
                as_of=as_of,
                sources=sorted({o.source.url for o in series}),
                fetched_at=max((o.provenance.fetched_at for o in series), default=None),
                target_fingerprint=fingerprint(original),
                proposal=None,
            ),
            drops,
        )

    candidate = Candidate(
        target=mapping.target,
        indicator=mapping.indicator,
        level=decision,
        changes=effective_changes,
        checks=checks,
        as_of=as_of,
        sources=sorted({o.source.url for o in series}),
        fetched_at=max((o.provenance.fetched_at for o in series), default=None),
        target_fingerprint=fingerprint(original),
        proposal=working if decision.level is not Level.RED else None,
        suggestions=suggestions,
    )
    return candidate, drops


def match_scalar_type(old: Any, new: str) -> Any:
    """按旧值的类型决定新值怎么写。

    最典型的是 `updated_at`：内容里写的是裸 `2026-09-01`，YAML 会把它解析成**日期对象**。
    若我们写回一个字符串，ruamel 为了保持"这是字符串"会加上引号，
    于是 diff 里出现 `2026-09-01` → `'2026-09-01'` 这种与本次更新无关的差异。

    原则与数值一致：**工具只改值，不改类型。**
    """
    if isinstance(old, datetime) and not isinstance(old, date):
        try:
            return datetime.fromisoformat(new)
        except ValueError:
            return new
    if isinstance(old, date):
        try:
            return date.fromisoformat(new)
        except ValueError:
            return new
    return new


def match_scalar_type(old: Any, new: str) -> Any:
    """按旧值的类型决定新值怎么写。

    最典型的是 `updated_at`：内容里写的是裸 `2026-09-01`，YAML 会把它解析成**日期对象**。
    若我们写回一个字符串，ruamel 为了保持"这是字符串"会加上引号，
    于是 diff 里出现 `2026-09-01` → `'2026-09-01'` 这种与本次更新无关的差异。

    原则与数值一致：**工具只改值，不改类型。**
    """
    if isinstance(old, datetime) and not isinstance(old, date):
        try:
            return datetime.fromisoformat(new)
        except ValueError:
            return new
    if isinstance(old, date):
        try:
            return date.fromisoformat(new)
        except ValueError:
            return new
    return new


def _path_exists(document: Any, path: str) -> bool:
    """路径存在吗。**不存在与"存在但是空"必须区分**，所以不用 `get() is None` 判断。"""
    try:
        path_get(document, path)
    except PathMissing:
        return False
    return True


def _proposed_payload(values: list[float], labels: list[str]) -> list[dict[str, Any]]:
    """交给 C3 复核的载荷。

    **必须带期间**：点序列算子（利差）重算出来的是整条派生序列，
    候选只是其中一个窗口——按位置比对必然长度不等。带上期间才能按期间对齐。
    """
    return [
        {"period": labels[i] if i < len(labels) else "", "value": v}
        for i, v in enumerate(values)
    ]


def _template_vars(
    series: list[Observation], mapping: Mapping, as_of: date | None
) -> dict[str, str]:
    """渲染溯源文字用的变量。

    `authority` 与 `via_note` 分开，是为了让内容里那行出处既能被普通读者读懂，
    又不掩盖数据实际是从哪里取的（PRD §3.2.1 第 ② 条要求如实标注层级与上游）。
    """
    first = series[0] if series else None
    if first is None:
        return {k: "" for k in ("authority", "via_note", "tier", "as_of", "latest_period",
                                "series_label", "unit", "count")}

    authority = (first.source.upstream or first.source.name).strip()
    # 渠道名必须来自 channel，不能拿 name 顶替——name 是"谁的数据"，
    # 顶替后会写出「X（数据经X获取）」这种既啰嗦又没信息量的话。
    via = (first.source.channel or "").strip()
    return {
        "authority": authority,
        "via_note": f"（数据经{via}获取）" if via else "",
        "tier": first.source.tier.value,
        "as_of": as_of.isoformat() if as_of else "",
        "latest_period": latest_period_of(series) or "",
        "series_label": mapping.slot.series_label or mapping.target,
        "unit": first.unit,
        "count": str(len(series)),
    }


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #


def run_funnel(observations: list[Observation], ctx: FunnelContext) -> FunnelOutcome:
    drops: list[Drop] = []

    # F1 源级准入
    surviving, d = f1_admit(observations, ctx)
    drops += d

    # F2 时效筛选
    surviving, d = f2_timeliness(surviving, ctx)
    drops += d

    # F3 结构校验
    surviving, d = f3_structure(surviving, ctx)
    drops += d

    # F4 值域（逐观测）
    surviving, d = f4_value_range(surviving, ctx)
    drops += d

    # 按指标分组 —— 以下各层都以"序列"为单位
    pool: dict[str, list[Observation]] = {}
    for obs in surviving:
        pool.setdefault(obs.indicator, []).append(obs)

    # F6（前半）适用性白名单：不在映射表里的一律丢弃
    mapped_pool: dict[str, list[Observation]] = {}
    unmapped: dict[str, int] = {}
    for indicator, series in sorted(pool.items()):
        if mapping := ctx.mappings.get(indicator):
            if not mapping.is_check_only:
                mapped_pool[indicator] = series
        else:
            unmapped[indicator] = len(series)
    inputs = operator_inputs(ctx.mappings)
    for indicator, n in sorted(unmapped.items()):
        if indicator in inputs:
            consumers = sorted(
                m.indicator
                for m in ctx.mappings.mappings.values()
                if indicator
                in {
                    str(m.transform.params.get(k))
                    for k in ("minus", "minuend", "monthly_indicator")
                }
            )
            drops.append(
                Drop(
                    "F6", indicator,
                    f"作为派生算子的输入使用（被 {'、'.join(consumers)} 消费），"
                    f"不直接写入内容——所以它不该出现在候选里，但它没有被浪费",
                    count=n,
                )
            )
        else:
            drops.append(
                Drop(
                    "F6", indicator,
                    "不在映射表里（指标 → 目标 → 字段）。即使数据正确也不允许写入内容——"
                    "这张表是「工具能改哪些内容」的可审计清单",
                    count=n,
                )
            )

    # F4 值域声明检查：未声明值域的指标无法判定合理性
    for indicator in sorted(mapped_pool):
        if ctx.thresholds.range_for(indicator) is None:
            drops.append(
                Drop("F4", indicator, "未在 thresholds.value_range_of 里声明值域，无法判定合理性")
            )

    candidates: list[Candidate] = []
    up_to_date: list[str] = []
    for indicator, series in sorted(mapped_pool.items()):
        mapping = ctx.mappings.get(indicator)
        assert mapping is not None
        drops += f4_caliber(series, mapping)
        candidate, d = build_candidate(mapping, series, pool, ctx)
        drops += d
        if candidate is None:
            continue
        # 没有任何改动、且级别是绿 → 移出候选列表。
        # 但**只有绿级**才这样处理：红/黄即使无改动也要留在候选里，
        # 因为"级别"本身就是需要你知道的信息
        # （例如"这个块还自称是示意数据"——那件事跟有没有改动无关）。
        if not candidate.changes and candidate.level_value is Level.GREEN:
            up_to_date.append(candidate.target)
            continue
        candidates.append(candidate)

    changeset = ChangeSet(
        # 精确到**秒**。曾经只到分钟，后果很具体：一分钟内跑两次，
        # 第二次会写进同一目录，把第一次的 changes.md 与 run.meta.json 覆盖掉，
        # 而 proposals/ 还留着第一次的文件——一个目录里混了两次运行的数据。
        # 审计链断在这里最难发现：产物看起来是完整的。
        run_id=ctx.generated_at.strftime("%Y-%m-%d-%H%M%S"),
        generated_at=ctx.generated_at,
        candidates=candidates,
        failures=list(ctx.failures),
        flags_banner=ctx.flags.banner(),
        up_to_date=sorted(set(up_to_date)),
    )
    return FunnelOutcome(changeset=changeset, drops=drops)
