"""生成人审文件 `changes.md`（CAND-2）。

验收标准是 PRD A-C18：**不打开代码就能读懂**。这句话约束的是几个具体的东西：

1. **级别必须给理由**，而且理由要回答"为什么不是更高一级"。
2. **数值不能只列全量**。一条序列动辄几十个点，全列出来等于让人自己找差异。
   所以只显示变化的首尾各 3 点 + 变化点数。
3. **丢弃的观测必须在产物里可见**。人读的时候一定会问"为什么少了一个指标"，
   如果答案只在日志里，这句话就没法回答。
4. **"需要你决定"必须是一句可回答的问题**，而不是"请人工确认"这种空话。

排序刻意是"红在前、绿在后"：人审文件的顺序应当由严重程度决定，不由抓取顺序决定。
"""

from __future__ import annotations

from .candidate import Candidate, ChangeSet, FieldChange
from .crosscheck import CheckOutcome
from .leveling import Level
from .series import format_number

_LEVEL_MARK = {Level.RED: "🔴 红", Level.YELLOW: "🟡 黄", Level.GREEN: "🟢 绿"}


def _fmt_points(values: list[object], head: int = 3, tail: int = 3) -> str:
    nums: list[str] = []
    for v in values:
        try:
            nums.append(format_number(float(v)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            nums.append(str(v))
    if len(nums) <= head + tail:
        return "[" + ", ".join(nums) + "]"
    return (
        "[" + ", ".join(nums[:head]) + ", …（省略 " + str(len(nums) - head - tail) + " 点）…, "
        + ", ".join(nums[-tail:]) + "]"
    )


def _change_block(changes: list[FieldChange]) -> list[str]:
    lines: list[str] = []
    for c in changes:
        if c.kind == "series":
            old = list(c.old or [])
            new = list(c.new or [])
            lines.append(f"- `{c.path}`")
            lines.append(f"  - 点数：{len(old)} → {len(new)}，其中 **{c.changed_count} 点变化**")
            lines.append(f"  - 旧值：{_fmt_points(old)}")
            lines.append(f"  - 新值：{_fmt_points(new)}")
        else:
            lines.append(f"- `{c.path}`：`{c.old}` → `{c.new}`")
    return lines


def _check_block(checks: list[CheckOutcome]) -> list[str]:
    if not checks:
        return ["- （该指标未声明任何校验手段）"]
    lines: list[str] = []
    for o in checks:
        mark = {"pass": "通过", "fail": "未通过", "unavailable": "**无法执行**"}[o.status]
        tol = f"，容差 {o.tolerance}" if o.tolerance is not None else ""
        lines.append(f"- {o.means}（{o.kind}）：{mark} — {o.detail}{tol if o.status != 'pass' else ''}")
    return lines


def _decision_needed(c: Candidate) -> list[str]:
    """列出"需要你决定什么"。**只列真的需要人决定的事项，不写空话。**"""
    items: list[str] = []
    lvl = c.level_value

    if lvl is Level.RED:
        items.append(f"是否按上述建议修改内容？（命中规则 {c.level.rule}）")

    for o in c.checks:
        if o.status == "fail":
            items.append("交叉校验未通过：以哪个数为准？")
        elif o.status == "unavailable":
            items.append(f"{o.means} 本次无法执行，是否接受这次更新？")

    if any(ch.kind == "series" and ch.changed_count == 0 for ch in c.changes):
        items.append("数值未发生变化，是否仍要更新溯源日期？")

    delta = [ch for ch in c.changes if ch.kind == "series" and ch.changed_count]
    if delta:
        items.append("数值变化是否符合你的预期？（不符则可能是源口径变了）")

    if not items:
        items.append("无需决定，可直接合入。")
    return items


def render_candidate(c: Candidate) -> list[str]:
    lines: list[str] = []
    mark = _LEVEL_MARK[c.level_value]
    lines.append(f"### {mark}　`{c.target}` ← `{c.indicator}`")
    lines.append("")

    lines.append(f"**为什么是这个级别**（命中规则 {c.level.rule}）：{c.level.reason}")
    lines.append("")

    for s in c.suggestions:
        lines.append(f"> 💡 {s}")
    if c.suggestions:
        lines.append("")

    lines.append("**变了什么**")
    lines.extend(_change_block(c.changes) or ["- （无字段变化）"])
    lines.append("")

    lines.append("**依据哪个源**")
    if c.sources:
        for u in c.sources:
            lines.append(f"- {u}")
    lines.append(f"- 「内容截至」：{c.as_of.isoformat() if c.as_of else '**源未提供，也未声明发布惯例**'}")
    if c.fetched_at:
        lines.append(f"- 抓取时间：{c.fetched_at.isoformat()}")
    lines.append("")

    lines.append("**校验用了什么**")
    lines.extend(_check_block(c.checks))
    lines.append("")

    lines.append("**需要你决定什么**")
    lines.extend(f"- [ ] {q}" for q in _decision_needed(c))
    lines.append("")

    if c.applies_automatically and c.proposal is not None:
        lines.append("> 该条已产出可直接合入的片段，见 `proposals/` 目录。")
    elif c.level_value is Level.RED:
        lines.append("> 红级**不产出**可直接合入的片段——需要你手工确认并编辑内容。")
    lines.append("")
    lines.append("---")
    lines.append("")
    return lines


def render(
    changeset: ChangeSet,
    *,
    drops: list[object] | None = None,
    drafts_written: bool = True,
) -> str:
    counts = changeset.counts
    lines: list[str] = []

    # ---------------- 头部 ----------------
    lines.append(f"# 内容候选 · {changeset.run_id}")
    lines.append("")
    lines.append(f"生成时间：{changeset.generated_at.isoformat()}　·　契约版本 {changeset.contract_version}")
    lines.append("")
    lines.append(
        f"**汇总**：共 {len(changeset.candidates)} 条候选 —— "
        f"🔴 红 {counts['red']}　🟡 黄 {counts['yellow']}　🟢 绿 {counts['green']}"
    )
    lines.append("")

    if changeset.failures:
        lines.append("## ⚠️ 本次失败的数据源")
        lines.append("")
        for f in changeset.failures:
            lines.append(f"- **{f.source}**：{f.reason}")
        lines.append("")
        lines.append("> 该源本次**没有产出任何候选**。不是「数据是 0」，而是「没拿到数据」。")
        lines.append("")

    lines.append("## 待修正清单")
    lines.append("")
    lines.append(changeset.flags_banner or "（状态未知）")
    lines.append("")

    # ---------------- 候选 ----------------
    lines.append("## 候选明细")
    lines.append("")
    ordered = changeset.sorted_candidates()
    if not ordered:
        lines.append("本次没有任何候选。可能原因见下方「被丢弃的观测」。")
        lines.append("")
    for c in ordered:
        lines.extend(render_candidate(c))

    # ---------------- 已是最新 ----------------
    if changeset.up_to_date:
        lines.append("## 已是最新")
        lines.append("")
        lines.append("这些目标本次没有任何字段需要改动（级别为绿）。列出来是为了说明"
                     "「工具确实检查过它们」，而不是漏掉了。")
        lines.append("")
        for target in changeset.up_to_date:
            lines.append(f"- `{target}`")
        lines.append("")

    # ---------------- 丢弃 ----------------
    if drops:
        by_layer: dict[str, list[object]] = {}
        for d in drops:
            by_layer.setdefault(getattr(d, "layer", "?"), []).append(d)
        lines.append("## 被丢弃的观测")
        lines.append("")
        lines.append("这些数据**没有被写入内容**。列在这里，是为了让「为什么少了一条」有处可查。")
        lines.append("")
        for layer in sorted(by_layer):
            lines.append(f"### {layer}")
            lines.append("")
            for d in by_layer[layer]:
                lines.append(f"- {getattr(d, 'line', lambda: str(d))()}")
            lines.append("")

    if not drafts_written:
        lines.append("> 本次没有写出任何可直接合入的片段（全部为红级或本就没有候选）。")
        lines.append("")

    return "\n".join(lines)
