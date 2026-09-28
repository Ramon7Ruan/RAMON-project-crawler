"""人审文件的可读性门禁（PRD A-C18）。

"不打开代码就能读懂"这句话如果不落成断言，就只是一句愿望。
本文件把它拆成若干**可机械核对**的要求：

1. 每条候选必须能看出**级别 + 规则编号 + 理由**，且理由要回答"为什么不是更高一级"
2. 数值变化必须给出**旧值 → 新值**，而不是只列新值
3. "需要你决定什么"必须是**可回答的问题**，不能是"请人工确认"这种空话
4. 被丢弃的观测必须**在产物里可见**，否则"为什么少了一条"无从回答
5. 源失败必须被描述成"没拿到数据"而不是"数据是 0"
6. 待修正清单的三种状态（已读 / 缺失 / 读不懂）必须**可区分**
"""

from __future__ import annotations

import json

from beacon.core.candidate import ChangeSet, SourceFailure
from beacon.core.changes_md import render
from beacon.core.flags import parse_flags
from beacon.core.funnel import run_funnel

from datetime import date, datetime

from test_funnel import (
    MAN,
    make_obs,
    PE,
    PE_TARGET,
    PMI_TARGET,
    document_with_baseline_window,
    full_pmi_input,
    make_ctx,
    pe_daily,
    pe_document,
)

FLAGS_SAMPLE = {
    "schemaVersion": "1.0",
    "exportedAt": "2026-09-23T00:00:00Z",
    "flags": [{"targetId": PMI_TARGET, "kind": "数值有误", "note": "8 月的不对"}],
}


def render_pmi(*, delta: float = 0.4, flags=None, failures=None) -> str:
    ctx = make_ctx(
        documents={PMI_TARGET: document_with_baseline_window(), PE_TARGET: pe_document()},
        flags=flags,
        failures=failures,
    )
    outcome = run_funnel(full_pmi_input(last_value_delta=delta), ctx)
    return render(outcome.changeset, drops=list(outcome.drops))


class TestCandidateReadability:
    def test_level_rule_and_reason_are_all_present(self) -> None:
        text = render_pmi(delta=3.0)  # 跳变 → 黄

        assert "🟡 黄" in text
        assert "命中规则 Y1" in text, "必须给出规则编号，否则理由无法核对"
        assert "最大变动" in text, "理由必须说清跳了多少"

    def test_change_shows_old_and_new(self) -> None:
        text = render_pmi(delta=0.4)

        assert "旧值：" in text and "新值：" in text
        assert "点变化" in text, "必须说明有多少点发生了变化（而不是只给全量）"

    def test_decisions_are_questions_not_platitudes(self) -> None:
        text = render_pmi(delta=0.4)
        section = text.split("**需要你决定什么**")[1]

        assert "- [ ]" in section, "需要用勾选框的形式列出待决事项"
        assert "请人工确认" not in text, "「请人工确认」是一句无法回答的话"
        assert "是否符合你的预期" in section

    def test_red_level_says_no_proposal_is_produced(self) -> None:
        book = parse_flags(json.dumps(FLAGS_SAMPLE, ensure_ascii=False))
        text = render_pmi(flags=book)

        assert "🔴 红" in text
        assert "不产出" in text and "可直接合入" in text
        assert "proposals/" not in text, "红级不得提示有可合入片段"


class TestDropsAreVisible:
    def test_dropped_observations_appear_with_layer_and_reason(self) -> None:
        ctx = make_ctx(
            documents={PMI_TARGET: document_with_baseline_window(), PE_TARGET: pe_document()}
        )
        # 必须给发布日：否则会被 F3（结构校验）先拦下，测不到 F6。
        # 这条注释本身也是提醒——想在测试里直接命中某一层，得先把前面几层喂饱。
        extra = [
            make_obs(
                "us.treasury.dgs3mo", "2026-09-01", 4.1, unit="%",
                name="美国财政部（美国国债收益率曲线）",
                upstream="美国财政部 / 美联储", channel="FRED",
                published_at=date(2026, 9, 1),
            )
        ]
        outcome = run_funnel(full_pmi_input() + extra, ctx)
        text = render(outcome.changeset, drops=list(outcome.drops))

        assert "## 被丢弃的观测" in text
        assert "F6" in text and "不在映射表里" in text
        assert "为什么少了一条" in text, "要明确告诉读者这一节是干什么用的"

    def test_no_drops_means_no_empty_section(self) -> None:
        ctx = make_ctx(
            documents={PMI_TARGET: document_with_baseline_window(), PE_TARGET: pe_document()}
        )
        outcome = run_funnel(full_pmi_input(), ctx)
        text = render(outcome.changeset, drops=list(outcome.drops))
        assert "## 被丢弃的观测" not in text


class TestFailuresAreDescribedHonestly:
    def test_failure_says_no_data_not_zero(self) -> None:
        text = render_pmi(failures=[SourceFailure("fred", "ReadTimeout: 读超时")])

        assert "⚠️ 本次失败的数据源" in text
        assert "fred" in text and "ReadTimeout" in text
        assert "没拿到数据" in text, "必须说清是「没拿到」而不是「数据是 0」"


class TestFlagsBannerIsAlwaysExplicit:
    def test_three_states_are_distinguishable(self) -> None:
        missing = render_pmi()
        assert "未提供待修正清单" in missing

        broken = render_pmi(
            flags=parse_flags('{"schemaVersion": "1.0", "flags": [')
        )
        assert "无法解析" in broken
        assert "未应用任何标记" in broken

        loaded = render_pmi(flags=parse_flags(json.dumps(FLAGS_SAMPLE, ensure_ascii=False)))
        assert "已读取待修正清单" in loaded
        assert "1 条标记" in loaded

        assert len({missing, broken, loaded}) == 3, "三种状态必须产出不同的文字"


class TestOrderingAndSummary:
    def test_summary_counts_match_candidates(self) -> None:
        ctx = make_ctx(
            documents={PMI_TARGET: document_with_baseline_window(), PE_TARGET: pe_document()}
        )
        outcome = run_funnel(full_pmi_input() + pe_daily(300), ctx)
        text = render(outcome.changeset, drops=list(outcome.drops))
        counts = outcome.changeset.counts

        assert f"共 {len(outcome.changeset.candidates)} 条候选" in text
        assert f"红 {counts['red']}" in text

    def test_red_candidates_come_first(self) -> None:
        """人审文件的顺序应当由严重程度决定，不由抓取顺序决定。"""
        ctx = make_ctx(
            documents={PMI_TARGET: document_with_baseline_window(), PE_TARGET: pe_document()}
        )
        outcome = run_funnel(full_pmi_input() + pe_daily(300), ctx)
        ordered = outcome.changeset.sorted_candidates()
        ranks = [c.level_value.rank for c in ordered]
        assert ranks == sorted(ranks, reverse=True)

    def test_render_is_deterministic(self) -> None:
        assert render_pmi() == render_pmi(), "同一输入必须产出逐字节相同的文件"


class TestRenderWithEmptyChangeset:
    def test_empty_changeset_explains_itself(self) -> None:
        cs = ChangeSet(run_id="2026-09-23-1200", generated_at=datetime.now())
        text = render(cs)
        assert "共 0 条候选" in text
        assert "没有任何候选" in text


class TestNoSectionIsRenderedTwice:
    """每个小节在产物里只能出现一次。

    这条不是吹毛求疵：真发生过——插入渲染逻辑的脚本被执行了两次，
    于是 `## 已是最新` 出现了两遍，而所有"内容正确"的断言都照常通过。
    只有"段数与出现次数"这类结构性断言才拦得住它。
    """

    def test_each_section_header_appears_at_most_once(self) -> None:
        ctx = make_ctx(
            documents={PMI_TARGET: document_with_baseline_window(), PE_TARGET: pe_document()}
        )
        extra = [
            make_obs(
                "us.treasury.dgs3mo", "2026-09-01", 4.1, unit="%",
                published_at=date(2026, 9, 1),
            )
        ]
        outcome = run_funnel(full_pmi_input(last_value_delta=0.4) + extra, ctx)
        text = render(
            outcome.changeset, drops=list(outcome.drops),
            )
        for header in ("## 候选明细", "## 被丢弃的观测", "## 待修正清单", "## 失败"):
            assert text.count(header) <= 1, f"「{header}」出现了 {text.count(header)} 次"

    def test_up_to_date_section_appears_at_most_once(self) -> None:
        from beacon.core.changes_md import render as r
        from beacon.core.candidate import ChangeSet
        from datetime import datetime

        cs = ChangeSet(
            run_id="x", generated_at=datetime.now(),
            up_to_date=["a", "b"],
        )
        text = r(cs)
        assert text.count("## 已是最新") == 1
        assert text.count("- `a`") == 1


class TestNoSectionIsRenderedTwice:
    """每个小节在产物里只能出现一次。

    这条不是吹毛求疵：真发生过——插入渲染逻辑的脚本被执行了两次，
    于是 `## 已是最新` 出现了两遍，而所有"内容正确"的断言都照常通过。
    只有"段数与出现次数"这类结构性断言才拦得住它。
    """

    def test_each_section_header_appears_at_most_once(self) -> None:
        ctx = make_ctx(
            documents={PMI_TARGET: document_with_baseline_window(), PE_TARGET: pe_document()}
        )
        extra = [
            make_obs(
                "us.treasury.dgs3mo", "2026-09-01", 4.1, unit="%",
                published_at=date(2026, 9, 1),
            )
        ]
        outcome = run_funnel(full_pmi_input(last_value_delta=0.4) + extra, ctx)
        text = render(
            outcome.changeset, drops=list(outcome.drops),
            )
        for header in ("## 候选明细", "## 被丢弃的观测", "## 待修正清单", "## 失败"):
            assert text.count(header) <= 1, f"「{header}」出现了 {text.count(header)} 次"

    def test_up_to_date_section_appears_at_most_once(self) -> None:
        from beacon.core.changes_md import render as r
        from beacon.core.candidate import ChangeSet
        from datetime import datetime

        cs = ChangeSet(
            run_id="x", generated_at=datetime.now(),
            up_to_date=["a", "b"],
        )
        text = r(cs)
        assert text.count("## 已是最新") == 1
        assert text.count("- `a`") == 1
