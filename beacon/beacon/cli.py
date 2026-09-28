"""命令行入口（IND-3）。

只暴露两个命令，够用就好：

    beacon health   各源探活，回答"现在哪些源还活着"
    beacon export   取数并产出中性产物 Feed（feed.jsonl + feed.meta.json）

三条贯穿全命令的纪律
--------------------
1. **路径全部由参数传入**（I5）——不硬编码任何父项目相对路径。
   本工具可以整体搬到别处、或被别的 app 调用。
2. **失败隔离**（NF-C10）——单个源失败不影响其他源，但必须被明确报出来。
3. **绝不产出部分可信的产物**：若所有源都失败，**不写任何文件**并返回非 0，
   而不是写一个空的 feed 让人以为"这次没有更新"。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from . import CONTRACT_VERSION, __version__
from .core.candidate import SourceFailure
from .core.contract import as_dict_list
from .core.path import PathError, PathMissing
from .sources.base import SourceAdapter, SourceError
from .sources.registry import (
    ADAPTERS,
    ConfigError,
    build_adapters,
    build_fetcher,
    load_config,
)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default=None,
        help="源配置文件路径（默认：包内 config/sources.yaml）",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="缓存与抓取日志目录（默认：./.beacon-cache）",
    )
    parser.add_argument(
        "--sources",
        default=None,
        help="只处理这些源，逗号分隔（默认：配置里登记的全部）",
    )
    parser.add_argument(
        "--transport",
        choices=("httpx", "curl"),
        default="httpx",
        help="HTTP 后端（默认 httpx）。某些站点对 Python 客户端不响应时改用 curl",
    )
    parser.add_argument("--proxy", default=None, help="显式指定 HTTP 代理，如 http://127.0.0.1:7890")
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="忽略环境变量里的代理设置，直连",
    )


def _prepare(args: argparse.Namespace) -> tuple[list[SourceAdapter], Path]:
    config = load_config(args.config)
    cache_dir = Path(args.cache_dir) if args.cache_dir else Path.cwd() / ".beacon-cache"
    fetcher = build_fetcher(
        config,
        cache_dir,
        transport_name=getattr(args, "transport", "httpx"),
        proxy=getattr(args, "proxy", None),
        trust_env=not getattr(args, "no_proxy", False),
    )
    names = [s.strip() for s in args.sources.split(",")] if args.sources else None
    return build_adapters(config, fetcher, names), cache_dir


# --------------------------------------------------------------------------- #
# beacon health
# --------------------------------------------------------------------------- #


def cmd_health(args: argparse.Namespace) -> int:
    adapters, _ = _prepare(args)
    print(f"beacon {__version__} · 源健康检查（{len(adapters)} 个）\n")
    broken = 0
    for adapter in adapters:
        result = adapter.health_check()
        mark = "OK  " if result.ok else "坏  "
        if not result.ok:
            broken += 1
        print(f"  [{mark}] {result.name:<10} {result.tier.value}  {result.detail}")
    print()
    if broken:
        print(f"  {broken} 个源不可用。不可用的源在 export 时会被跳过并记入 failures。")
    else:
        print("  全部可用。")
    return 0 if broken < len(adapters) else 1


# --------------------------------------------------------------------------- #
# beacon export
# --------------------------------------------------------------------------- #


def cmd_export(args: argparse.Namespace) -> int:
    adapters, _ = _prepare(args)
    out_dir = Path(args.out).resolve() if args.out else Path.cwd() / "content-candidates"
    out_dir.mkdir(parents=True, exist_ok=True)

    observations = []
    failures: list[dict[str, str]] = []
    used: list[str] = []

    for adapter in adapters:
        try:
            got = adapter.observations()
        except SourceError as exc:
            # 失败隔离：一个源倒下不影响其他源，但必须留痕
            failures.append({"source": adapter.name, "reason": exc.reason})
            print(f"  [跳过] {adapter.name}：{exc.reason}", file=sys.stderr)
            continue
        observations.extend(got)
        used.append(adapter.name)
        print(f"  [取到] {adapter.name}：{len(got)} 条观测")

    if not observations:
        # 铁律：全部失败时不写任何产物，避免下游把空文件当成"这次没有更新"
        print("\n所有源都没有取到数据，不产出任何文件。", file=sys.stderr)
        return 1

    observations.sort(key=lambda o: (o.indicator, o.period))

    feed_path = out_dir / "feed.jsonl"
    with feed_path.open("w", encoding="utf-8") as fh:
        for row in as_dict_list(observations):
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    meta = {
        "contractVersion": CONTRACT_VERSION,
        "beaconVersion": __version__,
        "generatedAt": datetime.now(UTC).isoformat(),
        "sources": used,
        "failures": failures,
        "indicators": sorted({o.indicator for o in observations}),
        "counts": {
            "observations": len(observations),
            "byIndicator": _count_by_indicator(observations),
        },
    }
    (out_dir / "feed.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"\n  产出：{feed_path}")
    print(f"        {out_dir / 'feed.meta.json'}")
    print(f"  观测 {len(observations)} 条 · 指标 {len(meta['indicators'])} 个 · 失败源 {len(failures)} 个")
    return 0


def _count_by_indicator(observations: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for o in observations:
        counts[o.indicator] = counts.get(o.indicator, 0) + 1
    return dict(sorted(counts.items()))


# --------------------------------------------------------------------------- #
# beacon run —— 七层漏斗 → 候选 + 人审文件
# --------------------------------------------------------------------------- #


def cmd_run(args: argparse.Namespace) -> int:
    from datetime import date

    from .core.changes_md import render as render_changes
    from .core.config import load_mapping, load_settings, load_thresholds
    from .core.flags import load_flags
    from .core.funnel import FunnelContext, run_funnel
    from .core.store import TargetError

    if not args.content_dir:
        print(
            "缺少 --content-dir：需要指定内容库目录（工具不猜父项目路径）",
            file=sys.stderr,
        )
        return 2

    adapters, _ = _prepare(args)
    mapping_table = load_mapping(args.mapping)
    thresholds = load_thresholds(args.thresholds)
    settings = load_settings(args.settings)

    # 内容库
    from .consumers.recall import RecallContentStore

    try:
        store = RecallContentStore(args.content_dir)
    except TargetError as exc:
        print(f"内容库错误：{exc}", file=sys.stderr)
        return 2

    # 待修正清单：缺失不算错，但状态必须带进产物
    flags = load_flags(args.flags if args.flags is not None else settings.flags_path)

    # 取数（失败隔离）
    observations = []
    failures: list[SourceFailure] = []
    used: list[str] = []
    for adapter in adapters:
        try:
            got = adapter.observations()
        except SourceError as exc:
            failures.append(SourceFailure(adapter.name, exc.reason))
            print(f"  [跳过] {adapter.name}：{exc.reason}", file=sys.stderr)
            continue
        observations.extend(got)
        used.append(adapter.name)
        print(f"  [取到] {adapter.name}：{len(got)} 条观测")

    if not observations:
        # 铁律：全源失败时不产出任何文件（PRD A-C3）
        print("\n所有源都没有取到数据，不产出任何候选文件。", file=sys.stderr)
        return 1

    ctx = FunnelContext(
        mappings=mapping_table,
        thresholds=thresholds,
        settings=settings,
        flags=flags,
        store=store,
        today=date.today(),
        generated_at=datetime.now(UTC),
        failures=failures,
    )

    try:
        outcome = run_funnel(observations, ctx)
    except (PathError, PathMissing) as exc:
        print(f"配置与内容对不上：{exc}", file=sys.stderr)
        return 2

    changeset = outcome.changeset
    out_root = Path(args.out).resolve() if args.out else Path.cwd() / "content-candidates"
    out_dir = out_root / changeset.run_id
    # 一次运行 = 一个目录，**永不覆盖**。
    # 若允许复用目录，第二次运行会覆盖第一次的 changes.md 与 run.meta.json，
    # 而 proposals/ 仍留着第一次的文件——产物看起来完整，实际是两次运行的混合，
    # 事后根本无法重建"当时到底提了什么、依据哪个指纹写回的"。
    if out_dir.exists():
        print(
            f"运行目录已存在：{out_dir}\n"
            f"  一次运行对应一个目录，不覆盖。请稍后重试（运行号精确到秒）。",
            file=sys.stderr,
        )
        return 2
    out_dir.mkdir(parents=True)

    # ---- 人审文件（唯一必须读的产物）----
    (out_dir / "changes.md").write_text(
        render_changes(changeset, drops=list(outcome.drops)), encoding="utf-8"
    )

    # ---- 可直接合入的片段：只有绿/黄才有 ----
    #
    # ⚠️ **按目标合并**，不能一条候选写一个文件。
    # 一个目标可能被多条映射写入（PMI 的制造业与非制造业就在同一个数据块的两个 series 里），
    # 而文件名只能是目标 id —— 后写的会**覆盖**先写的，于是先写那条的改动整条丢失。
    # 实测踩过：跑完之后「非制造业」更新了、「制造业」还是旧的示意值，
    # 而产物看起来完全正常（一个 proposal 文件、applied.json 里也只有一条记录）。
    proposals = out_dir / "proposals"
    merged, conflicts = _merge_proposals(changeset, store)
    written = 0
    for target, document in sorted(merged.items()):
        proposals.mkdir(parents=True, exist_ok=True)
        (proposals / f"{target}.yaml").write_text(_dump_yaml(document), encoding="utf-8")
        written += 1
    for msg in conflicts:
        print(f"  [冲突] {msg}", file=sys.stderr)

    # ---- 留档 ----
    (out_dir / "observations.json").write_text(
        json.dumps(as_dict_list(observations), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "run.meta.json").write_text(
        json.dumps(
            {
                "contractVersion": CONTRACT_VERSION,
                "beaconVersion": __version__,
                "runId": changeset.run_id,
                "generatedAt": changeset.generated_at.isoformat(),
                "sources": used,
                "failures": [{"source": f.source, "reason": f.reason} for f in failures],
                "counts": {
                    "observations": len(observations),
                    "candidates": len(changeset.candidates),
                    **changeset.counts,
                    "proposalsWritten": written,
                    "drops": len(outcome.drops),
                },
                "flags": {"source": flags.source, "banner": changeset.flags_banner},
                # 每个目标在**本次运行开始时**的语义指纹。
                # apply 会拿它判断"内容在我审核之后有没有被人改过"。
                "targetFingerprints": {
                    c.target: c.target_fingerprint
                    for c in changeset.candidates
                    if c.target_fingerprint
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    counts = changeset.counts
    print(f"\n  产出目录：{out_dir}")
    print(f"    changes.md           ← 人只看这一个文件")
    if written:
        print(f"    proposals/           ← {written} 个可直接合入的片段")
    print(f"    observations.json / run.meta.json")
    print(
        f"\n  候选 {len(changeset.candidates)} 条 —— 红 {counts['red']}　黄 {counts['yellow']}　绿 {counts['green']}"
    )
    if outcome.drops:
        print(f"  被丢弃的观测 {len(outcome.drops)} 组（原因见 changes.md）")
    if failures:
        print(f"  ⚠️ 失败源 {len(failures)} 个：{', '.join(f.source for f in failures)}")
    return 0


def _merge_proposals(changeset: Any, store: Any) -> tuple[dict[str, Any], list[str]]:
    """把落在同一个目标上的多条候选合成一份片段。

    同一条路径被两条候选写了**不同的值**时不算"合并"，而是配置冲突：
    此时无法判断该用哪个，所以拒绝该目标并明确报出来，而不是静默取最后一个。
    """
    from .core.path import clone, get as path_get, set_value

    groups: dict[str, list[Any]] = {}
    for cand in changeset.candidates:
        if cand.applies_automatically and cand.proposal is not None:
            groups.setdefault(cand.target, []).append(cand)

    merged: dict[str, Any] = {}
    conflicts: list[str] = []
    for target, cands in groups.items():
        if len(cands) == 1:
            merged[target] = cands[0].proposal
            continue

        document = clone(cands[0].proposal)
        claimed: dict[str, tuple[Any, str]] = {
            ch.path: (ch.new, cands[0].indicator) for ch in cands[0].changes
        }
        bad = False
        for cand in cands[1:]:
            for change in cand.changes:
                if change.path in claimed:
                    previous, who = claimed[change.path]
                    if previous != change.new:
                        conflicts.append(
                            f"{target} 的 {change.path} 被 {who} 与 {cand.indicator} "
                            f"写入了不同的值（{previous!r} vs {change.new!r}）——"
                            f"该目标本次不产出片段"
                        )
                        bad = True
                    continue
                set_value(document, change.path, change.new)
                claimed[change.path] = (change.new, cand.indicator)
        if not bad:
            merged[target] = document
    return merged, conflicts


def _dump_yaml(document: object) -> str:
    from ruamel.yaml import YAML

    from .core.style import stamp_flow_style

    # ⚠️ 必须在 dump **之前**钉住风格。
    # 一旦这份文档被写成块状落盘，再加载回来时风格已经是"块状（已定）"，
    # 那时任何"只处理未定风格"的修补都不会再触发——问题会被固化下来。
    stamp_flow_style(document)

    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=2, offset=0)   # 与 consumers.recall 保持一致
    import io

    buf = io.StringIO()
    y.dump(document, buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# beacon apply —— 把采纳的候选写回内容
# --------------------------------------------------------------------------- #


def cmd_apply(args: argparse.Namespace) -> int:
    """写回候选。

    **刻意不做的事**：不调用内容门禁、不调用任何外部命令。
    理由：门禁是 App 侧的工具（Node/tsx），而这个包必须不依赖 Node 工具链（IND-7）。
    写回之后由人（或外层脚本）去跑门禁——工具只负责把文件改对，并说清楚下一步。
    """
    from .core.fingerprint import fingerprint
    from .consumers.recall import RecallContentStore
    from .core.store import TargetError

    if not args.content_dir:
        print("缺少 --content-dir", file=sys.stderr)
        return 2
    if not args.candidates:
        print("缺少 --candidates（`beacon run` 产出的那次运行目录）", file=sys.stderr)
        return 2

    run_dir = Path(args.candidates).resolve()
    meta_path = run_dir / "run.meta.json"
    if not meta_path.exists():
        print(f"不是一次运行的产物目录（缺 run.meta.json）：{run_dir}", file=sys.stderr)
        return 2

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    recorded = dict(meta.get("targetFingerprints") or {})
    proposals_dir = run_dir / "proposals"
    files = sorted(proposals_dir.glob("*.yaml")) if proposals_dir.exists() else []

    print(f"运行：{meta.get('runId')}　（{len(files)} 个可合入片段）")

    # 内容库**先校验**，再去判断有没有片段。
    # 反过来写会让"路径写错"在恰好没有片段时被报成"本次没有可合入的片段"——
    # 那是把参数错误伪装成一次正常运行。
    try:
        store = RecallContentStore(args.content_dir)
    except TargetError as exc:
        print(f"内容库错误：{exc}", file=sys.stderr)
        return 2

    if not files:
        print("  本次没有可合入的片段——红级不产出片段，这是设计如此。")
        return 0

    pre_apply = run_dir / "pre-apply"
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for path in files:
        target = path.stem
        if not store.exists(target):
            skipped.append({"target": target, "reason": "内容库里找不到这个目标"})
            continue

        current = store.read(target)
        expected = recorded.get(target)
        if expected is None:
            skipped.append({"target": target, "reason": "运行记录里没有这个目标的指纹"})
            continue
        if fingerprint(current) != expected:
            # 这是本命令最重要的一条守卫
            skipped.append({
                "target": target,
                "reason": (
                    "内容在生成候选之后被改动过——无条件写回会**静默覆盖那次改动**。"
                    "请重新跑一次 run 并基于新内容审核"
                ),
            })
            continue

        try:
            proposal = _load_yaml(path)
        except Exception as exc:  # noqa: BLE001
            skipped.append({"target": target, "reason": f"片段无法解析：{exc}"})
            continue

        if args.dry_run:
            applied.append({"target": target, "status": "would-apply", "before": expected})
            continue

        # 先留一份原文快照：出问题时有一条明确的还原路径。
        # 不用"应该不会出错"来替代还原手段。
        pre_apply.mkdir(parents=True, exist_ok=True)
        (pre_apply / path.name).write_text(store.source_text(target), encoding="utf-8")

        before = fingerprint(current)
        store.write(target, proposal)

        after = fingerprint(store.read(target))
        if after != fingerprint(proposal):
            skipped.append({"target": target, "reason": "写回后复验不一致"})
            continue
        applied.append({"target": target, "status": "applied", "before": before, "after": after})

    (run_dir / "applied.json").write_text(
        json.dumps(
            {"runId": meta.get("runId"), "dryRun": bool(args.dry_run),
             "applied": applied, "skipped": skipped},
            ensure_ascii=False, indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    if args.dry_run:
        print("\n（预演，未写任何文件）")
    for item in applied:
        print(f"  [{'预演' if args.dry_run else '已写回'}] {item['target']}")
    for item in skipped:
        print(f"  [跳过] {item['target']}：{item['reason']}", file=sys.stderr)

    print(f"\n  已写回 {len(applied)} / 跳过 {len(skipped)}　→ {run_dir / 'applied.json'}")
    if applied and not args.dry_run:
        print(f"  原文快照：{pre_apply}")
        print("\n  下一步：在项目根目录运行**内容门禁**，确认没有破坏既有约束。")
        # 这里刻意不写出那条命令。
        # `tests/test_independence.py` 会扫描本包源码里是否出现 Node 工具链的词
        # （npm / npx / node_modules / electron），出现即失败——因为「不依赖 Node 工具链」
        # 这条性质只有做成机械的词扫描才拦得住，靠"这条只是提示不是依赖"的判断拦不住。
        # 代价是提示文字里不能写出具体命令，只能指向项目文档。
        print("  命令见项目根目录的 README（gate 脚本）。本工具不代跑，也不认识那条命令。")
    return 0 if not skipped else 1


def _load_yaml(path: Path) -> object:
    from ruamel.yaml import YAML

    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=2, offset=0)
    with path.open("r", encoding="utf-8") as fh:
        return y.load(fh)


# --------------------------------------------------------------------------- #
# beacon status —— 手动模式的配套入口（U3）
# --------------------------------------------------------------------------- #


def cmd_status(args: argparse.Namespace) -> int:
    """一眼看清：源活着吗、上次跑是什么时候、谁的「内容截至」超期、有没有待修正。"""
    from .core.config import load_mapping
    from .core.flags import load_flags
    from .consumers.recall import RecallContentStore
    from .core.store import TargetError

    print(f"beacon {__version__} · 状态\n")

    # ---- 1) 源健康（可选，因为要打网络）----
    if args.check_sources:
        adapters, _ = _prepare(args)
        for adapter in adapters:
            r = adapter.health_check()
            print(f"  [{'OK  ' if r.ok else '坏  '}] {r.name:<10} {r.tier.value}  {r.detail}")
    else:
        print("  （未检查源健康；加 --check-sources 会打网络）")
    print()

    if not args.content_dir:
        print("  未提供 --content-dir，无法检查内容状态。", file=sys.stderr)
        return 0

    # ---- 2) 上次运行 ----
    out_root = Path(args.out).resolve() if args.out else Path.cwd() / "content-candidates"
    runs = sorted([p for p in out_root.iterdir() if p.is_dir()]) if out_root.exists() else []
    if runs:
        last = runs[-1]
        m = last / "run.meta.json"
        info = json.loads(m.read_text(encoding="utf-8")) if m.exists() else {}
        c = info.get("counts") or {}
        print(f"  上次运行：{info.get('runId', last.name)}")
        print(f"    候选 {c.get('candidates', '?')} 条（红 {c.get('red', '?')}／"
              f"黄 {c.get('yellow', '?')}／绿 {c.get('green', '?')}），"
              f"待合入片段 {c.get('proposalsWritten', '?')} 个")
        flags_info = info.get("flags") or {}
        print(f"    待修正清单：{flags_info.get('banner', '未知')}")
    else:
        print(f"  还没有任何运行记录（{out_root} 为空）")
    print()

    # ---- 3) 内容截至 ----
    try:
        store = RecallContentStore(args.content_dir)
    except TargetError as exc:
        print(f"  内容库错误：{exc}", file=sys.stderr)
        return 2

    flags = load_flags(args.flags)
    today = date.today()
    stale_after = int(args.stale_days)
    print(f"  各目标的「内容截至」（超过 {stale_after} 天标 ⚠️）：")
    worst = 0
    # 按目标去重：一个目标可能被多条映射引用（如 PMI 的制造业与非制造业
    # 写在同一个数据块的两个 series 里）。按映射逐条列出会把同一份内容报两次，
    # 让人以为有两处需要处理。
    seen: set[str] = set()
    for mapping in sorted(load_mapping().mappings.values(), key=lambda m: m.target):
        if mapping.is_check_only or mapping.target in seen:
            continue
        seen.add(mapping.target)
        if not store.exists(mapping.target):
            print(f"    {mapping.target:<40} —　⚠️ 内容库里找不到这个目标")
            continue
        doc = store.read(mapping.target)
        as_of = _as_of_from_source(doc)
        marks = []
        if as_of is None:
            marks.append("没有「截至」日期")
        else:
            age = (today - as_of).days
            worst = max(worst, age)
            if age > stale_after:
                marks.append(f"⚠️ 已 {age} 天")
        if flags.has(mapping.target):
            marks.append("有待修正标记")
        tag = "　".join(marks) if marks else "正常"
        shown = as_of.isoformat() if as_of else "—"
        print(f"    {mapping.target:<40} {shown}　{tag}")
    print()
    if worst:
        print(f"  最旧的数据已 {worst} 天。")
    return 0


def _as_of_from_source(document: Any) -> date | None:
    """从内容的 `source` 文字里抽「截至 YYYY-MM-DD」。抽不到返回 None。"""
    raw = str(document.get("source") or "")
    m = re.search(r"截至\s*(\d{4})-(\d{2})-(\d{2})", raw)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="beacon",
        description="内容采集工具：把官方公开数据变成可审核的候选变更",
    )
    parser.add_argument("--version", action="version", version=f"beacon {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_health = sub.add_parser("health", help="各源探活")
    _add_common(p_health)
    p_health.set_defaults(func=cmd_health)

    p_export = sub.add_parser("export", help="取数并产出 Feed（中性产物）")
    _add_common(p_export)
    p_export.add_argument("--out", default=None, help="产物目录（默认：./content-candidates）")
    p_export.set_defaults(func=cmd_export)

    p_run = sub.add_parser("run", help="跑七层漏斗，产出候选与人审文件")
    _add_common(p_run)
    p_run.add_argument(
        "--content-dir",
        default=None,
        help="内容库目录（必需）。工具不猜父项目路径——这是一个独立的包",
    )
    p_run.add_argument("--mapping", default=None, help="指标映射表（默认：包内 config/mapping.yaml）")
    p_run.add_argument("--thresholds", default=None, help="阈值配置（默认：包内 config/thresholds.yaml）")
    p_run.add_argument("--settings", default=None, help="运行设置（默认：包内 config/settings.yaml）")
    p_run.add_argument(
        "--flags",
        default=None,
        help="「待修正」清单 JSON。缺失不算错，但状态会写进产物",
    )
    p_run.add_argument("--out", default=None, help="产物根目录（默认：./content-candidates）")
    p_run.set_defaults(func=cmd_run)

    p_apply = sub.add_parser("apply", help="把某次运行的可合入片段写回内容")
    p_apply.add_argument("--candidates", required=True, help="`beacon run` 产出的运行目录")
    p_apply.add_argument("--content-dir", required=True, help="内容库目录")
    p_apply.add_argument("--dry-run", action="store_true", help="只预演，不写任何文件")
    p_apply.set_defaults(func=cmd_apply)

    p_status = sub.add_parser("status", help="源健康 / 上次运行 / 内容是否超期 / 待修正标记")
    _add_common(p_status)
    p_status.add_argument("--content-dir", default=None, help="内容库目录")
    p_status.add_argument("--flags", default=None, help="「待修正」清单 JSON")
    p_status.add_argument("--out", default=None, help="运行产物根目录（默认：./content-candidates）")
    p_status.add_argument(
        "--check-sources", action="store_true", help="同时检查源健康（会打网络）"
    )
    p_status.add_argument("--stale-days", default=30, help="超过多少天算过时（默认 30）")
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
