"""漏斗与分级测试（P-C3 的门禁）。

门禁原文（`开发计划.md` §3 P-C3）：
    手工构造 **5 组变更**，分级（绿/黄/红）**全部正确**；`changes.md` 不打开代码就能读懂。

所以本文件的结构是"先构造变更，再断言级别"，而不是"跑一遍看输出对不对"。
构造必须显式、可读——否则"分级正确"这句话就无从核对。

所有测试都不打网络：观测是直接构造的 `Observation` 对象（NF-C9）。

⚠️ 一个容易踩的坑：**水平值序列必须够 13 个月**。
C2 的恒等式要拿"本期 vs 去年同期"比，只有 6 个月时 C2 一律 `unavailable`，
于是所有本该是绿级的用例都会掉到黄级（Y4）。测试构造史里真踩过这一步。
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from beacon.core.candidate import ChangeSet, SourceFailure
from beacon.core.config import load_mapping, load_settings, load_thresholds
from beacon.core.contract import Caliber, Observation, Provenance, SourceRef, Tier
from beacon.core.flags import FlagsBook, load_flags, parse_flags
from beacon.core.funnel import FunnelContext, run_funnel
from beacon.core.leveling import Level
from beacon.core.store import MemoryStore

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
TODAY = date(2026, 9, 23)

PMI_TARGET = "hotspot.data.pmi-reading"
PE_TARGET = "hotspot.data.valuation-percentile"
MAN = "cn.pmi.manufacturing"
NON = "cn.pmi.non-manufacturing"
PE = "cn.index.pe.000300"

HISTORY_MONTHS = 24
"""构造多久的历史。必须 ≥ 13（C2 需要去年同期）。"""


# --------------------------------------------------------------------------- #
# 期间与序列构造
# --------------------------------------------------------------------------- #


def months_back(end: str, n: int) -> list[str]:
    y, m = int(end[:4]), int(end[5:7])
    out: list[str] = []
    for i in range(n - 1, -1, -1):
        mm, yy = m - i, y
        while mm <= 0:
            mm += 12
            yy -= 1
        out.append(f"{yy}-{mm:02d}")
    return out


def baseline_values(n: int = HISTORY_MONTHS) -> list[float]:
    """确定性的基线水平值。刻意有小幅波动（不是常数），才测得出跳变逻辑。"""
    return [round(49.5 + ((i * 7) % 9 - 4) * 0.2, 2) for i in range(n)]


def make_obs(
    indicator: str,
    period: str,
    value: float,
    *,
    unit: str = "点",
    caliber: Caliber = Caliber.LEVEL,
    tier: Tier = Tier.L2,
    name: str = "国家统计局与中国物流与采购联合会",
    upstream: str | None = "国家统计局 / 中国物流与采购联合会",
    channel: str | None = "东方财富",
    published_at: date | None = None,
) -> Observation:
    return Observation(
        indicator=indicator,
        period=period,
        value=value,
        unit=unit,
        caliber=caliber,
        source=SourceRef(
            name=name, url=f"https://example.test/{indicator}", tier=tier,
            upstream=upstream, channel=channel, published_at=published_at,
        ),
        provenance=Provenance(
            fetched_at=NOW, http_status=200, from_cache=False, raw_sha256="a" * 64
        ),
    )


def pmi_levels(
    values: list[float] | None = None,
    *,
    indicator: str = MAN,
    end: str = "2026-08",
) -> list[Observation]:
    vals = values if values is not None else baseline_values()
    return [
        make_obs(indicator, p, v) for p, v in zip(months_back(end, len(vals)), vals)
    ]


def yoy_of(levels: list[Observation], *, indicator: str | None = None) -> list[Observation]:
    """由水平值序列**真的算一遍**同比。

    不填常数：填常数的话，C2 是否真的在工作就测不出来了
    （常数会让"重算值 ≠ 自报值"永远成立，测试反而会假通过或假失败）。
    """
    by_period = {o.period: o.value for o in levels}
    out: list[Observation] = []
    for o in levels:
        y, m = int(o.period[:4]), int(o.period[5:7])
        base = by_period.get(f"{y - 1}-{m:02d}")
        if base is None:
            continue  # 去年同期不在序列里 → 无法配对（与真实源的行为一致）
        out.append(
            make_obs(
                indicator or f"{o.indicator}.yoy",
                o.period,
                round((o.value - base) / base * 100, 4),
                unit="%",
                caliber=Caliber.YOY,
            )
        )
    return out


def pe_daily(days: int = 1200, *, end: str = "2026-09-22") -> list[Observation]:
    end_date = date.fromisoformat(end)
    out: list[Observation] = []
    for i in range(days - 1, -1, -1):
        d = end_date - timedelta(days=i)
        value = round(12.0 + (i % 97) * 0.05, 4)   # 确定性锯齿，非单调
        out.append(
            make_obs(
                PE, d.isoformat(), value, unit="倍", tier=Tier.L1,
                name="中证指数有限公司", upstream="中证指数有限公司（指数编制机构，一手）",
                channel=None, published_at=d,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# 目标文档构造
# --------------------------------------------------------------------------- #


def pmi_document(
    *,
    man: list[float] | None = None,
    non: list[float] | None = None,
    man_name: str = "制造业 PMI",
    non_name: str = "非制造业 PMI",
    ticks: list[str] | None = None,
    block_source: str = "制造业 / 非制造业 PMI；数据来源：国家统计局，截至 2026-08-31",
    item_source: str = "国家统计局与中国物流与采购联合会，截至 2026-08-31",
    updated_at: str = "2026-08-31",
) -> dict:
    """造一份 PMI 目标文档。

    默认值刻意是"**已经是真实数据**"的状态（无示意标记、槽位名匹配、刻度是期间），
    这样各用例只需覆盖自己关心的那一项，不会因为"首次替换"而意外落在红级。
    """
    ticks = ticks or ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]
    return {
        "id": PMI_TARGET,
        "title": "PMI 怎么读",
        "source": item_source,
        "updated_at": updated_at,
        "blocks": [
            {"type": "prose", "label": "定义", "body": "PMI 是环比扩散指数。"},
            {"type": "timeline", "label": "周期", "events": []},
            {
                "type": "dataviz",
                "label": "制造业与非制造业",
                "chart": "line",
                "xLabel": "月份",
                "yLabel": "PMI",
                "xTicks": list(ticks),
                "series": [
                    {"name": man_name, "points": list(man if man is not None else [49.3] * 6)},
                    {"name": non_name, "points": list(non if non is not None else [50.1] * 6)},
                ],
                "source": block_source,
            },
        ],
    }


def pe_document(*, points: list[float] | None = None, block_source: str | None = None) -> dict:
    return {
        "id": PE_TARGET,
        "title": "估值分位",
        "source": "中证指数有限公司，截至 2026-09-22",
        "updated_at": "2026-09-22",
        "blocks": [
            {"type": "prose", "label": "定义", "body": "分位是区间内的相对位置。"},
            {
                "type": "dataviz",
                "label": "不同区间下的分位",
                "chart": "bar",
                "xTicks": ["近 3 年", "近 5 年", "近 10 年", "全历史"],
                "series": [{"name": "分位", "points": list(points or [64.69, 70.3, 55.15, 55.15])}],
                "source": block_source
                or "沪深300 估值分位；数据来源：中证指数有限公司，截至 2026-09-22",
            },
        ],
    }


def yield_document(
    *,
    points: list[float] | None = None,
    series_name: str = "利差",
    block_source: str = "数据待首次填充",
) -> dict:
    """收益率曲线概念：blocks[1] 是利差的折线块。"""
    return {
        "id": "hotspot.data.yield-curve-inversion",
        "title": "收益率曲线倒挂",
        "source": "美国财政部，截至 2026-09-01",
        "updated_at": "2026-09-01",
        "blocks": [
            {"type": "prose", "label": "定义", "body": "长端低于短端就是倒挂。"},
            {
                "type": "dataviz",
                "label": "10 年期减 2 年期利差（跌破 0 即倒挂）",
                "chart": "line",
                "xLabel": "月份",
                "yLabel": "利差（百分点）",
                "xTicks": list(points or []) and ["2025-10"],
                "series": [{"name": series_name, "points": list(points or [])}],
                "source": block_source,
            },
        ],
    }


def yield_document(
    *,
    points: list[float] | None = None,
    series_name: str = "利差",
    block_source: str = "数据待首次填充",
) -> dict:
    """收益率曲线概念：blocks[1] 是利差的折线块。"""
    return {
        "id": "hotspot.data.yield-curve-inversion",
        "title": "收益率曲线倒挂",
        "source": "美国财政部，截至 2026-09-01",
        "updated_at": "2026-09-01",
        "blocks": [
            {"type": "prose", "label": "定义", "body": "长端低于短端就是倒挂。"},
            {
                "type": "dataviz",
                "label": "10 年期减 2 年期利差（跌破 0 即倒挂）",
                "chart": "line",
                "xLabel": "月份",
                "yLabel": "利差（百分点）",
                "xTicks": list(points or []) and ["2025-10"],
                "series": [{"name": series_name, "points": list(points or [])}],
                "source": block_source,
            },
        ],
    }


def make_ctx(
    *,
    documents: dict[str, dict] | None = None,
    flags: FlagsBook | None = None,
    failures: list[SourceFailure] | None = None,
) -> FunnelContext:
    return FunnelContext(
        mappings=load_mapping(),
        thresholds=load_thresholds(),
        settings=load_settings(),
        flags=flags or FlagsBook(source="missing"),
        store=MemoryStore(documents or {PMI_TARGET: pmi_document(), PE_TARGET: pe_document()}),
        today=TODAY,
        generated_at=NOW,
        failures=failures or [],
    )


def find(changeset: ChangeSet, target: str, indicator: str | None = None):
    for c in changeset.candidates:
        if c.target == target and (indicator is None or c.indicator == indicator):
            return c
    return None


def full_pmi_input(*, last_value_delta: float = 0.0) -> list[Observation]:
    """一套完整的 PMI 输入：24 个月水平值 + 与之自洽的同比。

    `last_value_delta` 用来制造变化（末月加一点）。
    """
    values = baseline_values()
    incoming = list(values)
    incoming[-1] = round(incoming[-1] + last_value_delta, 2)
    levels = pmi_levels(incoming)
    return levels + yoy_of(levels, indicator=f"{MAN}.yoy")


def document_with_baseline_window() -> dict:
    """文档里的旧值 = 基线序列的最后 6 点。

    这样 `full_pmi_input(last_value_delta=0)` 会产出"零变化"的候选，
    而 `delta != 0` 时恰好只有最后一点变化——便于精确断言。
    """
    return pmi_document(man=baseline_values()[-6:])


# --------------------------------------------------------------------------- #
# 门禁：5 组变更，分级全部正确
# --------------------------------------------------------------------------- #


class TestLevelingGate:
    """P-C3 门禁：手工构造的变更，分级必须全部正确。"""

    def test_group1_green_plain_numeric_refresh(self) -> None:
        """第 1 组 · 绿 —— 常规数值刷新。

        条件齐备：槽位语义匹配、无示意标记、校验通过、无跳变、无缺期、无待修正标记，
        且改动只落在白名单字段上。
        """
        ctx = make_ctx(
            documents={
                PMI_TARGET: document_with_baseline_window(),
                PE_TARGET: pe_document(),
            }
        )
        outcome = run_funnel(full_pmi_input(last_value_delta=0.4), ctx)
        cand = find(outcome.changeset, PMI_TARGET, MAN)

        assert cand is not None
        assert cand.level_value is Level.GREEN, (
            f"应为绿级，实际命中 {cand.level.rule}：{cand.level.reason}"
        )
        assert cand.applies_automatically, "绿级必须产出可直接合入的片段"
        assert cand.proposal is not None
        series_change = next(c for c in cand.changes if c.path.endswith("series[0].points"))
        assert series_change.changed_count == 1, "只应有一点变化"

    def test_group2_yellow_jump_exceeds_threshold(self) -> None:
        """第 2 组 · 黄 —— 触发跳变阈值（扩散指数单月变动 > 2.0 点）。"""
        ctx = make_ctx(
            documents={
                PMI_TARGET: document_with_baseline_window(),
                PE_TARGET: pe_document(),
            }
        )
        outcome = run_funnel(full_pmi_input(last_value_delta=3.0), ctx)
        cand = find(outcome.changeset, PMI_TARGET, MAN)

        assert cand is not None
        assert cand.level_value is Level.YELLOW
        assert cand.level.rule == "Y1", "应当命中跳变规则，而不是别的规则"
        assert "最大变动" in cand.level.reason, "理由必须说清跳了多少"
        assert cand.applies_automatically, "黄级仍产出可合入片段（只是需人确认）"

    def test_group3_yellow_check_unavailable(self) -> None:
        """第 3 组 · 黄 —— 声明的校验本次无法执行。

        这是"不放宽标准"最容易被侵蚀的地方：只要一个 `except: pass`，
        所有校验都会慢慢变成装饰品，而且没有任何测试会发现。
        """
        ctx = make_ctx(
            documents={
                PMI_TARGET: document_with_baseline_window(),
                PE_TARGET: pe_document(),
            }
        )
        levels = pmi_levels(
            [*baseline_values()[:-1], round(baseline_values()[-1] + 0.4, 2)]
        )
        outcome = run_funnel(levels, ctx)   # 刻意不给同比 → C2 无法执行
        cand = find(outcome.changeset, PMI_TARGET, MAN)

        assert cand is not None
        assert cand.level_value is Level.YELLOW
        assert cand.level.rule == "Y4"
        assert "无法执行" in cand.level.reason

    def test_group4_red_first_real_replacement(self) -> None:
        """第 4 组 · 红 —— 首次以真实数据替换示意数据（结构性变更）。"""
        ctx = make_ctx(
            documents={
                PMI_TARGET: pmi_document(
                    man=[48.5, 49.2, 50.6, 51.2, 50.4, 49.8],
                    man_name="新订单",                     # 槽位语义不符
                    ticks=["1", "2", "3", "4", "5", "6"],  # 刻度还是序号
                    block_source="示意数据，仅用于说明剪刀差的用法",
                ),
                PE_TARGET: pe_document(),
            }
        )
        outcome = run_funnel(full_pmi_input(last_value_delta=0.4), ctx)
        cand = find(outcome.changeset, PMI_TARGET, MAN)

        assert cand is not None
        assert cand.level_value is Level.RED
        assert cand.level.rule == "R3"
        assert cand.proposal is None, "红级**不得**产出可直接合入的片段"
        assert not cand.applies_automatically
        assert cand.suggestions, "红级必须给出该怎么改的建议"

    def test_group5_red_flag_hit(self) -> None:
        """第 5 组 · 红 —— 该概念有未处理的「待修正」标记。"""
        book = parse_flags(
            json.dumps(
                {
                    "schemaVersion": "1.0",
                    "exportedAt": "2026-09-23T00:00:00Z",
                    "flags": [
                        {"targetId": PMI_TARGET, "kind": "数值有误", "note": "8 月的不对"}
                    ],
                },
                ensure_ascii=False,
            )
        )
        ctx = make_ctx(
            documents={
                PMI_TARGET: document_with_baseline_window(),
                PE_TARGET: pe_document(),
            },
            flags=book,
        )
        outcome = run_funnel(full_pmi_input(last_value_delta=0.4), ctx)
        cand = find(outcome.changeset, PMI_TARGET, MAN)

        assert cand is not None
        assert cand.level_value is Level.RED
        assert cand.level.rule == "R2"
        assert "数值有误" in cand.level.reason, "理由里必须带上标记的内容"

    def test_red_when_change_falls_outside_the_whitelist(self) -> None:
        """补充用例 · 红 —— 改动落在可写白名单之外。

        正常路径下这永远不会发生（只往白名单写），所以要**故意**篡改映射来验证
        这条守卫真的会拦住——否则它只是一句声明。
        """
        ctx = make_ctx(
            documents={
                PMI_TARGET: document_with_baseline_window(),
                PE_TARGET: pe_document(),
            }
        )
        # 把白名单缩到只剩一个无关字段：于是真实改动全部落在白名单外
        broken = [
            m for m in ctx.mappings.mappings.values() if m.indicator == MAN
        ][0]
        object.__setattr__(broken, "writable_fields", ("blocks[99].nope",))

        outcome = run_funnel(full_pmi_input(last_value_delta=0.4), ctx)
        cand = find(outcome.changeset, PMI_TARGET, MAN)

        assert cand is not None
        assert cand.level_value is Level.RED
        assert cand.level.rule == "R1"
        assert cand.proposal is None


# --------------------------------------------------------------------------- #
# 丢弃纪律
# --------------------------------------------------------------------------- #


class TestDropDiscipline:
    def test_unmapped_indicator_is_dropped_even_when_correct(self) -> None:
        """不在映射表里的观测，**即使数据完全正确也丢弃**（PRD §4.7 F6）。

        收益率曲线在首版正是这种情况——它的数据完全正确，但没有语义匹配的槽位。
        """
        # 用 dgs3mo 而不是 dgs10 —— dgs10 现在**已经有映射**（利差），
        # 它不再是"未映射"的例子。3 个月期既没映射、也不是任何算子的输入。
        yields = [
            make_obs(
                "us.treasury.dgs3mo", f"2026-09-{d:02d}", 4.1, unit="%",
                name="美国财政部（美国国债收益率曲线）",
                upstream="美国财政部 / 美联储", channel="FRED",
                published_at=date(2026, 9, d),
            )
            for d in range(1, 11)
        ]
        ctx = make_ctx(
            documents={
                PMI_TARGET: document_with_baseline_window(),
                PE_TARGET: pe_document(),
            }
        )
        outcome = run_funnel(full_pmi_input() + yields, ctx)

        assert not any(c.indicator == "us.treasury.dgs3mo" for c in outcome.changeset.candidates)
        f6 = [d for d in outcome.drops if d.layer == "F6"]
        assert any("us.treasury.dgs3mo" in d.indicator for d in f6)
        assert any("不在映射表里" in d.reason for d in f6), "必须说明丢弃原因"

    def test_operator_inputs_are_distinguished_from_dropped(self) -> None:
        """作为算子输入的指标是「被用掉」，不是「被丢弃」。

        两者在产物里必须能区分：一律报「不在映射表里」会让人去找
        "为什么一个明显参与了计算的指标被丢掉了"。
        """
        yields = [
            make_obs(
                "us.treasury.dgs2", f"2026-09-{d:02d}", 4.7, unit="%",
                name="美国财政部（美国国债收益率曲线）",
                upstream="美国财政部 / 美联储", channel="FRED",
                published_at=date(2026, 9, d),
            )
            for d in range(1, 11)
        ] + [
            make_obs(
                "us.treasury.dgs10", f"2026-09-{d:02d}", 4.9, unit="%",
                name="美国财政部（美国国债收益率曲线）",
                upstream="美国财政部 / 美联储", channel="FRED",
                published_at=date(2026, 9, d),
            )
            for d in range(1, 11)
        ]
        ctx = make_ctx(
            documents={
                PMI_TARGET: document_with_baseline_window(),
                PE_TARGET: pe_document(),
                "hotspot.data.yield-curve-inversion": yield_document(),
            }
        )
        outcome = run_funnel(full_pmi_input() + yields, ctx)

        dgs2 = next(
            (d for d in outcome.drops if d.layer == "F6" and "dgs2" in d.indicator), None
        )
        assert dgs2 is not None
        assert "作为派生算子的输入使用" in dgs2.reason
        assert "不在映射表里" not in dgs2.reason

    def test_value_out_of_range_is_dropped_not_clamped(self) -> None:
        """值域超标 → **丢弃**，绝不截断到边界（截断就是造值）。"""
        values = baseline_values()
        values[-1] = 999.0
        ctx = make_ctx(
            documents={
                PMI_TARGET: document_with_baseline_window(),
                PE_TARGET: pe_document(),
            }
        )
        levels = pmi_levels(values)
        outcome = run_funnel(levels + yoy_of(levels, indicator=f"{MAN}.yoy"), ctx)

        f4 = [d for d in outcome.drops if d.layer == "F4"]
        assert any("超出值域" in d.reason for d in f4)

        cand = find(outcome.changeset, PMI_TARGET, MAN)
        assert cand is not None
        new_points = next(
            c.new for c in cand.changes if c.path.endswith("series[0].points")
        )
        assert max(new_points) < 100, "被丢弃的值绝不能出现在内容里"

    def test_incomplete_period_is_dropped(self) -> None:
        """进行中 / 未来的期间不采。"""
        extra = [make_obs(MAN, "2026-09", 50.9)]
        ctx = make_ctx(
            documents={
                PMI_TARGET: document_with_baseline_window(),
                PE_TARGET: pe_document(),
            }
        )
        outcome = run_funnel(full_pmi_input() + extra, ctx)

        f2 = [d for d in outcome.drops if d.layer == "F2"]
        assert any("尚未走完" in d.reason for d in f2)

    def test_wrong_caliber_is_dropped(self) -> None:
        """口径不符 → 丢弃。这是防「拿错序列」最直接的一层。"""
        wrong = [
            make_obs(MAN, p, 49.0, caliber=Caliber.CUMULATIVE)
            for p in months_back("2026-08", 6)
        ]
        ctx = make_ctx(
            documents={
                PMI_TARGET: document_with_baseline_window(),
                PE_TARGET: pe_document(),
            }
        )
        outcome = run_funnel(wrong, ctx)

        f4 = [d for d in outcome.drops if d.layer == "F4"]
        assert any("口径不符" in d.reason for d in f4)

    def test_empty_input_yields_empty_changeset(self) -> None:
        ctx = make_ctx()
        outcome = run_funnel([], ctx)
        assert outcome.changeset.candidates == []
        assert outcome.changeset.counts == {"green": 0, "yellow": 0, "red": 0}

    def test_source_failure_is_carried_into_the_changeset(self) -> None:
        """源失败要带进产物——"少了两个指标"和"这两个指标没拿到"必须可区分。"""
        ctx = make_ctx(failures=[SourceFailure("fred", "ReadTimeout")])
        outcome = run_funnel(full_pmi_input(), ctx)
        assert [f.source for f in outcome.changeset.failures] == ["fred"]


# --------------------------------------------------------------------------- #
# 幂等性
# --------------------------------------------------------------------------- #


class TestIdempotency:
    """同一份输入跑两次，候选必须**逐字段一致**（PRD 测试⑦）。"""

    def test_two_runs_produce_identical_candidates(self) -> None:
        series = pe_daily(days=1200)
        doc = pe_document()

        def run() -> ChangeSet:
            ctx = make_ctx(
                documents={
                    PE_TARGET: copy.deepcopy(doc),
                    PMI_TARGET: document_with_baseline_window(),
                }
            )
            return run_funnel(series, ctx).changeset

        first, second = run(), run()
        assert len(first.candidates) == len(second.candidates) == 1

        a, b = first.candidates[0], second.candidates[0]
        assert a.indicator == b.indicator
        assert a.level.rule == b.level.rule
        assert [c.path for c in a.changes] == [c.path for c in b.changes]
        assert [c.new for c in a.changes] == [c.new for c in b.changes], "数值必须逐点一致"
        assert a.proposal == b.proposal, "产出的文档必须完全一致（含派生文字）"

    def test_sampling_does_not_drift_when_new_data_arrives(self) -> None:
        """序列尾部多几条时，已取样的点**不得漂移**。

        这是"按期取样"相对于"等距抽稀"的核心优势。若这条挂了，
        说明取样被改成了等距抽稀——那时每次更新都会显示"整条序列都变了"，
        人审文件会彻底失去意义。
        """
        from beacon.core.series import downsample

        short = pe_daily(days=400)
        longer = pe_daily(days=430)

        a = downsample(short)
        b = {o.period: o.value for o in downsample(longer)}
        assert len(a) > 10, "取样后点数太少，这条断言就没有意义了"
        for o in a[:-1]:
            assert b.get(o.period) == o.value, f"{o.period} 的取样值发生了漂移"


# --------------------------------------------------------------------------- #
# flags 输入的健壮性
# --------------------------------------------------------------------------- #


class TestFlagsInput:
    def test_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        book = load_flags(tmp_path / "nope.json")
        assert book.source == "missing"
        assert book.banner(), "即使是缺失状态，也必须产出可读的说明行"

    def test_broken_file_is_marked_not_silently_ignored(self, tmp_path: Path) -> None:
        """损坏的标记文件**不算错**，但产物里必须能看出"这次没读到"。"""
        p = tmp_path / "flags.json"
        p.write_text('{"schemaVersion": "1.0", "flags": [', encoding="utf-8")
        book = load_flags(p)

        assert book.source == "unreadable"
        assert not book.usable
        assert "无法解析" in book.banner()

    def test_wrong_schema_version_is_refused_explicitly(self) -> None:
        book = parse_flags(json.dumps({"schemaVersion": "9.9", "flags": []}))
        assert book.source == "unreadable"
        assert "9.9" in book.banner()

    def test_entries_without_target_are_skipped(self) -> None:
        book = parse_flags(
            json.dumps({"schemaVersion": "1.0", "flags": [{"kind": "x"}, {"targetId": "ok"}]})
        )
        assert book.source == "file"
        assert list(book.by_target) == ["ok"]

    def test_non_list_flags_is_unreadable(self) -> None:
        book = parse_flags(json.dumps({"schemaVersion": "1.0", "flags": {"a": 1}}))
        assert book.source == "unreadable"


class TestJumpLooksAtTheChangeNotTheHistory:
    """跳变检查必须看「这次改了什么」，而不是「历史序列里有没有大波动」。

    这是一个会**永久**污染所有候选、却不会让任何测试变红的缺陷：
    原先的实现拿整条抓取序列判断跳变，而 `cn.index.pe.000300` 一次返回 2400 个日度点，
    十年里必然有某一天涨跌超过阈值 → 每次运行都命中跳变 → 每条候选永久是黄级。
    级别从此不再携带信息，但代码没有任何异常。
    """

    def test_historical_swing_does_not_flag_a_quiet_update(self) -> None:
        from beacon.core.funnel import f4_jump, FunnelContext  # noqa: F401

        # 造一条 1200 天的序列，中间插一次 50% 的历史暴涨
        series = pe_daily(days=1200)
        mid = len(series) // 2
        object.__setattr__(series[mid], "value", series[mid].value * 1.5)

        ctx = make_ctx()
        # 「内容里的旧值」与「即将写入的新值」几乎相同 —— 这次更新什么都没改
        old_values = [20.0, 30.0, 40.0, 50.0]
        new_values = [20.0, 30.0, 40.0, 50.4]

        finding = f4_jump(old_values, new_values, ["a", "b", "c", "d"], ctx, "cn.index.pe.000300")

        assert not finding.exceeded, (
            f"历史序列里的大波动不该影响本次更新，却报了：{finding.detail}"
        )

    def test_a_real_change_in_the_content_is_flagged(self) -> None:
        from beacon.core.funnel import f4_jump

        ctx = make_ctx()
        # 分位变动 23 个百分点 > 阈值 20
        finding = f4_jump(
            [88.0, 72.0, 54.0, 46.0],
            [64.69, 70.3, 55.15, 55.15],
            ["近 3 年", "近 5 年", "近 10 年", "全历史"],
            ctx,
            "cn.index.pe.000300",
        )
        assert finding.exceeded
        assert "近 3 年" in finding.detail, "理由里要指明是哪个位置变了"

    def test_percentile_and_pe_use_different_thresholds(self) -> None:
        """阈值必须按「写进内容的量」选档，不是按原始指标。"""
        from beacon.core.config import load_thresholds

        t = load_thresholds()
        pe_jump = t.jump_for("cn.index.pe.000300")
        pmi_jump = t.jump_for("cn.pmi.manufacturing")

        assert "max_change" in pe_jump, "分位应当按「改变多少个百分点」衡量"
        assert pe_jump["max_change"] != pmi_jump["max_change"]


class TestJumpLooksAtTheChangeNotTheHistory:
    """跳变检查必须看「这次改了什么」，而不是「历史序列里有没有大波动」。

    这是一个会**永久**污染所有候选、却不会让任何测试变红的缺陷：
    原先的实现拿整条抓取序列判断跳变，而 `cn.index.pe.000300` 一次返回 2400 个日度点，
    十年里必然有某一天涨跌超过阈值 → 每次运行都命中跳变 → 每条候选永久是黄级。
    级别从此不再携带信息，但代码没有任何异常。
    """

    def test_historical_swing_does_not_flag_a_quiet_update(self) -> None:
        from beacon.core.funnel import f4_jump, FunnelContext  # noqa: F401

        # 造一条 1200 天的序列，中间插一次 50% 的历史暴涨
        series = pe_daily(days=1200)
        mid = len(series) // 2
        object.__setattr__(series[mid], "value", series[mid].value * 1.5)

        ctx = make_ctx()
        # 「内容里的旧值」与「即将写入的新值」几乎相同 —— 这次更新什么都没改
        old_values = [20.0, 30.0, 40.0, 50.0]
        new_values = [20.0, 30.0, 40.0, 50.4]

        finding = f4_jump(old_values, new_values, ["a", "b", "c", "d"], ctx, "cn.index.pe.000300")

        assert not finding.exceeded, (
            f"历史序列里的大波动不该影响本次更新，却报了：{finding.detail}"
        )

    def test_a_real_change_in_the_content_is_flagged(self) -> None:
        from beacon.core.funnel import f4_jump

        ctx = make_ctx()
        # 分位变动 23 个百分点 > 阈值 20
        finding = f4_jump(
            [88.0, 72.0, 54.0, 46.0],
            [64.69, 70.3, 55.15, 55.15],
            ["近 3 年", "近 5 年", "近 10 年", "全历史"],
            ctx,
            "cn.index.pe.000300",
        )
        assert finding.exceeded
        assert "近 3 年" in finding.detail, "理由里要指明是哪个位置变了"

    def test_percentile_and_pe_use_different_thresholds(self) -> None:
        """阈值必须按「写进内容的量」选档，不是按原始指标。"""
        from beacon.core.config import load_thresholds

        t = load_thresholds()
        pe_jump = t.jump_for("cn.index.pe.000300")
        pmi_jump = t.jump_for("cn.pmi.manufacturing")

        assert "max_change" in pe_jump, "分位应当按「改变多少个百分点」衡量"
        assert pe_jump["max_change"] != pmi_jump["max_change"]


class TestUpToDateTargets:
    """已经最新的目标不该占用人审文件的篇幅。

    一个"什么都没变"的候选进了候选列表，人审文件就要为它写一整节，
    而你只能读到"（无字段变化）"——纯噪音，还会稀释真正需要看的那几条。
    但它也不能凭空消失：必须能看出"工具确实检查过它"。
    """

    def _run_second_time(self):
        """第一次跑完并写回，再跑第二次——此时一切已是最新。"""
        documents = {
            PMI_TARGET: document_with_baseline_window(),
            PE_TARGET: pe_document(),
        }
        # 必须把 PE 的观测也喂进去——只喂 PMI 是造不出 PE 候选的（这里踩过一次）
        first = run_funnel(full_pmi_input() + pe_daily(1200), make_ctx(documents=documents))
        # 用手工构造"已写回"的状态：把提案里的值当作新文档
        cand = find(first.changeset, PE_TARGET, PE)
        assert cand is not None and cand.proposal is not None
        documents[PE_TARGET] = cand.proposal
        return run_funnel(pe_daily(1200), make_ctx(documents=documents))

    def test_no_change_green_goes_to_up_to_date(self) -> None:
        second = self._run_second_time()

        assert find(second.changeset, PE_TARGET, PE) is None, "无改动的绿级不该进候选列表"
        assert PE_TARGET in second.changeset.up_to_date

    def test_it_is_still_reported_somewhere(self) -> None:
        """不能凭空消失——要能看出工具检查过它。"""
        from beacon.core.changes_md import render

        second = self._run_second_time()
        text = render(second.changeset, drops=list(second.drops))

        assert "## 已是最新" in text
        assert PE_TARGET in text
        assert "而不是漏掉了" in text

    def test_red_without_changes_still_stays_a_candidate(self) -> None:
        """红级即使没有改动也要留在候选里——"级别"本身就是需要你知道的信息。

        典型情形：一个数据块还自称是「示意数据」，而它的值恰好已经等于真实值。
        此时没有字段要改，但"这个块还没被正式承认为真实数据"必须被说出来。
        """
        documents = {
            PMI_TARGET: pmi_document(
                man=baseline_values()[-6:],
                man_name="制造业 PMI",
                block_source="示意数据，仅用于说明剪刀差的用法",   # 仍自称示意
            ),
            PE_TARGET: pe_document(),
        }
        outcome = run_funnel(full_pmi_input(), make_ctx(documents=documents))
        cand = find(outcome.changeset, PMI_TARGET, MAN)

        assert cand is not None, "红级不得被 up_to_date 吞掉"
        assert cand.level_value is Level.RED
        assert cand.level.rule == "R3"


class TestUpToDateTargets:
    """已经最新的目标不该占用人审文件的篇幅。

    一个"什么都没变"的候选进了候选列表，人审文件就要为它写一整节，
    而你只能读到"（无字段变化）"——纯噪音，还会稀释真正需要看的那几条。
    但它也不能凭空消失：必须能看出"工具确实检查过它"。
    """

    def _run_second_time(self):
        """第一次跑完并写回，再跑第二次——此时一切已是最新。"""
        documents = {
            PMI_TARGET: document_with_baseline_window(),
            PE_TARGET: pe_document(),
        }
        # 必须把 PE 的观测也喂进去——只喂 PMI 是造不出 PE 候选的（这里踩过一次）
        first = run_funnel(full_pmi_input() + pe_daily(1200), make_ctx(documents=documents))
        # 用手工构造"已写回"的状态：把提案里的值当作新文档
        cand = find(first.changeset, PE_TARGET, PE)
        assert cand is not None and cand.proposal is not None
        documents[PE_TARGET] = cand.proposal
        return run_funnel(pe_daily(1200), make_ctx(documents=documents))

    def test_no_change_green_goes_to_up_to_date(self) -> None:
        second = self._run_second_time()

        assert find(second.changeset, PE_TARGET, PE) is None, "无改动的绿级不该进候选列表"
        assert PE_TARGET in second.changeset.up_to_date

    def test_it_is_still_reported_somewhere(self) -> None:
        """不能凭空消失——要能看出工具检查过它。"""
        from beacon.core.changes_md import render

        second = self._run_second_time()
        text = render(second.changeset, drops=list(second.drops))

        assert "## 已是最新" in text
        assert PE_TARGET in text
        assert "而不是漏掉了" in text

    def test_red_without_changes_still_stays_a_candidate(self) -> None:
        """红级即使没有改动也要留在候选里——"级别"本身就是需要你知道的信息。

        典型情形：一个数据块还自称是「示意数据」，而它的值恰好已经等于真实值。
        此时没有字段要改，但"这个块还没被正式承认为真实数据"必须被说出来。
        """
        documents = {
            PMI_TARGET: pmi_document(
                man=baseline_values()[-6:],
                man_name="制造业 PMI",
                block_source="示意数据，仅用于说明剪刀差的用法",   # 仍自称示意
            ),
            PE_TARGET: pe_document(),
        }
        outcome = run_funnel(full_pmi_input(), make_ctx(documents=documents))
        cand = find(outcome.changeset, PMI_TARGET, MAN)

        assert cand is not None, "红级不得被 up_to_date 吞掉"
        assert cand.level_value is Level.RED
        assert cand.level.rule == "R3"


class TestDownsamplingIsActuallyApplied:
    """降采样必须**真的执行**，不能只写在配置里。

    这是一个"配置说做了、代码没做"的缺陷：日期实例里，`settings.funnel.series`
    写着"按月取样、上限 60 点"，但漏斗从未调用过它。对月度数据看不出来
    （一个月本来就一个点），对日度数据则直接出事——
    收益率一次返回 400 个交易日，"取最近 12 期"变成**最近 12 个交易日**，
    图上只有半个月。没有异常、没有测试变红，只有一张不对的图。
    """

    def _ctx_for(self, document: dict, mapping_path=None):
        return make_ctx(documents=document)

    def test_content_values_are_monthly_not_daily(self) -> None:
        from beacon.core.config import load_mapping
        from beacon.core.funnel import content_values

        mapping = load_mapping().mappings["cn.pmi.manufacturing"]
        ctx = make_ctx()
        levels = pmi_levels()          # 24 个月
        values, labels = content_values(mapping, levels, ctx)

        assert len(values) == 6, "映射声明 periods=6，应当正好 6 个点"
        assert all(len(lbl) == 7 for lbl in labels), f"刻度应当是月份：{labels}"
        assert labels == sorted(labels)

    def test_a_daily_series_is_sampled_to_months(self) -> None:
        """日度序列必须被压成月频——否则"最近 12 期"就是最近 12 天。"""
        from beacon.core.config import load_mapping, load_settings
        from beacon.core.funnel import content_values
        from beacon.core.series import downsample

        mapping = load_mapping().mappings["cn.pmi.manufacturing"]
        ctx = make_ctx()
        # 把日度序列伪装成某一指标的观测：只关心取样行为
        daily = pe_daily(days=400)
        daily = [o.model_copy(update={"indicator": "cn.pmi.manufacturing"}) for o in daily]

        sampled = downsample(daily, sample="last-in-period", max_points=60, keep_latest=True)
        assert len(sampled) < 40, f"400 个交易日应当被压到月频（约 13 点），实际 {len(sampled)}"

        values, labels = content_values(mapping, daily, ctx)
        assert all(len(lbl) == 7 for lbl in labels), f"刻度必须是月：{labels}"
        # 6 个点跨越的应当是多个月，而不是 6 天
        assert labels[0] < labels[-1]

    def test_percentile_operator_still_uses_the_full_history(self) -> None:
        """分位需要整段历史，不能被降采样砍掉。

        这是"两个算子不能一律对待"的落点：`percentile-rank` 的输出长度由窗口数决定，
        砍掉历史会直接改变分位值。
        """
        from beacon.core.config import load_mapping
        from beacon.core.funnel import content_values

        mapping = load_mapping().mappings["cn.index.pe.000300"]
        ctx = make_ctx()
        series = pe_daily(days=1200)

        full, _ = content_values(mapping, series, ctx)
        halved, _ = content_values(mapping, series[:600], ctx)

        assert len(full) == 4, "分位是 4 个值（4 个区间）"
        assert full != halved, "历史长度变了，分位就该变——说明它确实用了整段历史"


class TestDownsamplingIsActuallyApplied:
    """降采样必须**真的执行**，不能只写在配置里。

    这是一个"配置说做了、代码没做"的缺陷：日期实例里，`settings.funnel.series`
    写着"按月取样、上限 60 点"，但漏斗从未调用过它。对月度数据看不出来
    （一个月本来就一个点），对日度数据则直接出事——
    收益率一次返回 400 个交易日，"取最近 12 期"变成**最近 12 个交易日**，
    图上只有半个月。没有异常、没有测试变红，只有一张不对的图。
    """

    def _ctx_for(self, document: dict, mapping_path=None):
        return make_ctx(documents=document)

    def test_content_values_are_monthly_not_daily(self) -> None:
        from beacon.core.config import load_mapping
        from beacon.core.funnel import content_values

        mapping = load_mapping().mappings["cn.pmi.manufacturing"]
        ctx = make_ctx()
        levels = pmi_levels()          # 24 个月
        values, labels = content_values(mapping, levels, ctx)

        assert len(values) == 6, "映射声明 periods=6，应当正好 6 个点"
        assert all(len(lbl) == 7 for lbl in labels), f"刻度应当是月份：{labels}"
        assert labels == sorted(labels)

    def test_a_daily_series_is_sampled_to_months(self) -> None:
        """日度序列必须被压成月频——否则"最近 12 期"就是最近 12 天。"""
        from beacon.core.config import load_mapping, load_settings
        from beacon.core.funnel import content_values
        from beacon.core.series import downsample

        mapping = load_mapping().mappings["cn.pmi.manufacturing"]
        ctx = make_ctx()
        # 把日度序列伪装成某一指标的观测：只关心取样行为
        daily = pe_daily(days=400)
        daily = [o.model_copy(update={"indicator": "cn.pmi.manufacturing"}) for o in daily]

        sampled = downsample(daily, sample="last-in-period", max_points=60, keep_latest=True)
        assert len(sampled) < 40, f"400 个交易日应当被压到月频（约 13 点），实际 {len(sampled)}"

        values, labels = content_values(mapping, daily, ctx)
        assert all(len(lbl) == 7 for lbl in labels), f"刻度必须是月：{labels}"
        # 6 个点跨越的应当是多个月，而不是 6 天
        assert labels[0] < labels[-1]

    def test_percentile_operator_still_uses_the_full_history(self) -> None:
        """分位需要整段历史，不能被降采样砍掉。

        这是"两个算子不能一律对待"的落点：`percentile-rank` 的输出长度由窗口数决定，
        砍掉历史会直接改变分位值。
        """
        from beacon.core.config import load_mapping
        from beacon.core.funnel import content_values

        mapping = load_mapping().mappings["cn.index.pe.000300"]
        ctx = make_ctx()
        series = pe_daily(days=1200)

        full, _ = content_values(mapping, series, ctx)
        halved, _ = content_values(mapping, series[:600], ctx)

        assert len(full) == 4, "分位是 4 个值（4 个区间）"
        assert full != halved, "历史长度变了，分位就该变——说明它确实用了整段历史"


class TestContentWindowIsTheLastPeriods:
    """内容窗口必须是**最近 N 期**，不能被上限裁剪稀释。

    这是一个顺序错误：`max_points` 曾经作用在整条原始序列上。
    PMI 有 224 个月历史，先裁到 60 点会被均匀稀释到 18 年跨度，
    再"取最近 6 期"得到的是横跨两年的稀疏窗口——实测取到了 2024-10。
    代码没报错，只是图上的月份不对。
    """

    def test_monthly_window_is_consecutive(self) -> None:
        from beacon.core.config import load_mapping
        from beacon.core.funnel import content_values

        mapping = load_mapping().mappings[MAN]
        ctx = make_ctx()
        series = pmi_levels()          # 24 个月，远超 max_points 之外的稀释效应
        _, labels = content_values(mapping, series, ctx)

        assert labels == ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"], (
            f"应当是连续的最近 6 个月，实际 {labels}"
        )

    def test_long_history_does_not_widen_the_window(self) -> None:
        """历史越长，窗口也不该变宽——它只取决于 `periods`。"""
        from beacon.core.config import load_mapping
        from beacon.core.funnel import content_values

        mapping = load_mapping().mappings[MAN]
        ctx = make_ctx()

        short = pmi_levels(end="2026-08")
        long = pmi_levels([49.0] * 120 + [49.5] * 24, end="2026-08")  # 144 个月

        _, labels_short = content_values(mapping, short, ctx)
        _, labels_long = content_values(mapping, long, ctx)
        assert labels_short == labels_long


class TestC3ComparesPointSeriesByPeriod:
    """点序列算子必须**按期间**复核，不能按位置。

    算子重算的是整条派生序列（实测 273 个交易日），而候选只是其中一个窗口（12 个月）。
    按位置比会报"点数不一致：候选 12 点，重算 273 点"——把一次正确的计算报成失败，
    而且这条 fail 会降黄，于是这个指标**永久不可能变绿**。
    """

    def _daily_yields(self, days: int = 400) -> list[Observation]:
        from datetime import timedelta

        end = date(2026, 9, 21)
        out: list[Observation] = []
        for i in range(days - 1, -1, -1):
            d = end - timedelta(days=i)
            out.append(
                make_obs(
                    "us.treasury.dgs10", d.isoformat(), round(4.9 + (i % 11) * 0.01, 4),
                    unit="%", name="美国财政部（美国国债收益率曲线）",
                    upstream="美国财政部 / 美联储", channel="FRED", published_at=d,
                )
            )
            out.append(
                make_obs(
                    "us.treasury.dgs2", d.isoformat(), round(4.7 + (i % 7) * 0.01, 4),
                    unit="%", name="美国财政部（美国国债收益率曲线）",
                    upstream="美国财政部 / 美联储", channel="FRED", published_at=d,
                )
            )
        return out

    def test_spread_is_verified_against_the_widest_series(self) -> None:
        from beacon.core.crosscheck import derived_recompute
        from beacon.core.funnel import content_values, _proposed_payload
        from beacon.core.config import load_mapping

        mapping = load_mapping().mappings["us.treasury.dgs10"]
        ctx = make_ctx()
        series = self._daily_yields()
        pool = {
            "us.treasury.dgs10": [o for o in series if o.indicator.endswith("dgs10")],
            "us.treasury.dgs2": [o for o in series if o.indicator.endswith("dgs2")],
        }

        values, labels = content_values(mapping, pool["us.treasury.dgs10"], ctx, pool)
        assert len(values) == 12, f"应当取最近 12 个月，实际 {len(values)}"

        outcome = derived_recompute(
            "us.treasury.dgs10",
            "spread",
            series=pool["us.treasury.dgs10"],
            params=mapping.transform.params,
            proposed=_proposed_payload(values, labels),
            tolerance=2.0,
            extra_series=pool,
        )
        assert outcome.status == "pass", f"本该通过，却报了：{outcome.detail}"

    def test_a_wrong_value_is_still_caught(self) -> None:
        """按期间比对**不能变成一律通过**——值被改过要能抓住。"""
        from beacon.core.crosscheck import derived_recompute
        from beacon.core.funnel import content_values, _proposed_payload
        from beacon.core.config import load_mapping

        mapping = load_mapping().mappings["us.treasury.dgs10"]
        ctx = make_ctx()
        series = self._daily_yields()
        pool = {
            "us.treasury.dgs10": [o for o in series if o.indicator.endswith("dgs10")],
            "us.treasury.dgs2": [o for o in series if o.indicator.endswith("dgs2")],
        }
        values, labels = content_values(mapping, pool["us.treasury.dgs10"], ctx, pool)
        tampered = list(values)
        tampered[-1] += 0.5          # 手改一个点

        outcome = derived_recompute(
            "us.treasury.dgs10",
            "spread",
            series=pool["us.treasury.dgs10"],
            params=mapping.transform.params,
            proposed=_proposed_payload(tampered, labels),
            tolerance=0.02,
            extra_series=pool,
        )
        assert outcome.status == "fail"
        assert labels[-1] in outcome.detail, "失败信息要指明是哪个期间"


class TestContentWindowIsTheLastPeriods:
    """内容窗口必须是**最近 N 期**，不能被上限裁剪稀释。

    这是一个顺序错误：`max_points` 曾经作用在整条原始序列上。
    PMI 有 224 个月历史，先裁到 60 点会被均匀稀释到 18 年跨度，
    再"取最近 6 期"得到的是横跨两年的稀疏窗口——实测取到了 2024-10。
    代码没报错，只是图上的月份不对。
    """

    def test_monthly_window_is_consecutive(self) -> None:
        from beacon.core.config import load_mapping
        from beacon.core.funnel import content_values

        mapping = load_mapping().mappings[MAN]
        ctx = make_ctx()
        series = pmi_levels()          # 24 个月，远超 max_points 之外的稀释效应
        _, labels = content_values(mapping, series, ctx)

        assert labels == ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"], (
            f"应当是连续的最近 6 个月，实际 {labels}"
        )

    def test_long_history_does_not_widen_the_window(self) -> None:
        """历史越长，窗口也不该变宽——它只取决于 `periods`。"""
        from beacon.core.config import load_mapping
        from beacon.core.funnel import content_values

        mapping = load_mapping().mappings[MAN]
        ctx = make_ctx()

        short = pmi_levels(end="2026-08")
        long = pmi_levels([49.0] * 120 + [49.5] * 24, end="2026-08")  # 144 个月

        _, labels_short = content_values(mapping, short, ctx)
        _, labels_long = content_values(mapping, long, ctx)
        assert labels_short == labels_long


class TestC3ComparesPointSeriesByPeriod:
    """点序列算子必须**按期间**复核，不能按位置。

    算子重算的是整条派生序列（实测 273 个交易日），而候选只是其中一个窗口（12 个月）。
    按位置比会报"点数不一致：候选 12 点，重算 273 点"——把一次正确的计算报成失败，
    而且这条 fail 会降黄，于是这个指标**永久不可能变绿**。
    """

    def _daily_yields(self, days: int = 400) -> list[Observation]:
        from datetime import timedelta

        end = date(2026, 9, 21)
        out: list[Observation] = []
        for i in range(days - 1, -1, -1):
            d = end - timedelta(days=i)
            out.append(
                make_obs(
                    "us.treasury.dgs10", d.isoformat(), round(4.9 + (i % 11) * 0.01, 4),
                    unit="%", name="美国财政部（美国国债收益率曲线）",
                    upstream="美国财政部 / 美联储", channel="FRED", published_at=d,
                )
            )
            out.append(
                make_obs(
                    "us.treasury.dgs2", d.isoformat(), round(4.7 + (i % 7) * 0.01, 4),
                    unit="%", name="美国财政部（美国国债收益率曲线）",
                    upstream="美国财政部 / 美联储", channel="FRED", published_at=d,
                )
            )
        return out

    def test_spread_is_verified_against_the_widest_series(self) -> None:
        from beacon.core.crosscheck import derived_recompute
        from beacon.core.funnel import content_values, _proposed_payload
        from beacon.core.config import load_mapping

        mapping = load_mapping().mappings["us.treasury.dgs10"]
        ctx = make_ctx()
        series = self._daily_yields()
        pool = {
            "us.treasury.dgs10": [o for o in series if o.indicator.endswith("dgs10")],
            "us.treasury.dgs2": [o for o in series if o.indicator.endswith("dgs2")],
        }

        values, labels = content_values(mapping, pool["us.treasury.dgs10"], ctx, pool)
        assert len(values) == 12, f"应当取最近 12 个月，实际 {len(values)}"

        outcome = derived_recompute(
            "us.treasury.dgs10",
            "spread",
            series=pool["us.treasury.dgs10"],
            params=mapping.transform.params,
            proposed=_proposed_payload(values, labels),
            tolerance=2.0,
            extra_series=pool,
        )
        assert outcome.status == "pass", f"本该通过，却报了：{outcome.detail}"

    def test_a_wrong_value_is_still_caught(self) -> None:
        """按期间比对**不能变成一律通过**——值被改过要能抓住。"""
        from beacon.core.crosscheck import derived_recompute
        from beacon.core.funnel import content_values, _proposed_payload
        from beacon.core.config import load_mapping

        mapping = load_mapping().mappings["us.treasury.dgs10"]
        ctx = make_ctx()
        series = self._daily_yields()
        pool = {
            "us.treasury.dgs10": [o for o in series if o.indicator.endswith("dgs10")],
            "us.treasury.dgs2": [o for o in series if o.indicator.endswith("dgs2")],
        }
        values, labels = content_values(mapping, pool["us.treasury.dgs10"], ctx, pool)
        tampered = list(values)
        tampered[-1] += 0.5          # 手改一个点

        outcome = derived_recompute(
            "us.treasury.dgs10",
            "spread",
            series=pool["us.treasury.dgs10"],
            params=mapping.transform.params,
            proposed=_proposed_payload(tampered, labels),
            tolerance=0.02,
            extra_series=pool,
        )
        assert outcome.status == "fail"
        assert labels[-1] in outcome.detail, "失败信息要指明是哪个期间"
