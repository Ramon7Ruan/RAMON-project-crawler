"""写回（`beacon apply`）测试。

写回是整个工具唯一**会改动已有内容**的动作，所以它的每条守卫都必须被测到。
最重要的三条：

1. **指纹守卫**：从生成候选到确认之间内容若被人改过，必须拒绝写回。
   没有它，无条件写回会静默覆盖那次手工修改——写回"成功"、diff 也"对"，
   只是丢掉了一个不属于本次流程的改动。
2. **写后复验**：写完要重新读一遍并比对，而不是相信"写成功"这件事本身。
3. **原文快照**：出问题时有一条明确的还原路径，不靠"应该不会出错"。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from beacon.cli import main
from beacon.core.candidate import ChangeSet
from beacon.core.fingerprint import fingerprint
from beacon.consumers.recall import RecallContentStore

# 规范形式的夹具（缩进必须与 consumers/recall.py 一致，否则往返会重排）
CONTENT = """\
- id: hotspot.data.valuation-percentile
  domain: hotspot
  track: data
  title: 估值分位
  source: 中证指数有限公司，截至 2026-09-01
  updated_at: 2026-09-01
  recipe: [prose, formula, dataviz]
  blocks:
  - {type: prose, label: 定义, body: "分位把当前估值放进历史区间里排序。"}
  - type: dataviz
    label: 不同区间下的分位
    chart: bar
    xTicks: ["近 3 年", "近 5 年", "近 10 年", "全历史"]
    series:
    - {name: 分位, points: [88, 72, 54, 46]}
    source: 沪深300 估值分位；数据来源：中证指数有限公司，截至 2026-09-01

- id: econ.macro.other
  domain: econ
  track: macro
  title: 另一个概念
  source: 国家统计局，截至 2026-09-01
  updated_at: 2026-09-01
  recipe: [prose]
  blocks:
  - {type: prose, label: 定义, body: "不该被碰到。"}
"""

TARGET = "hotspot.data.valuation-percentile"


@pytest.fixture
def content_dir(tmp_path: Path) -> Path:
    d = tmp_path / "content" / "hotspot"
    d.mkdir(parents=True)
    (d / "hotspot.yaml").write_text(CONTENT, encoding="utf-8")
    return tmp_path / "content"


def make_run_dir(tmp_path: Path, content_dir: Path, *, tamper_proposal: bool = False) -> Path:
    """造一次运行的产物：run.meta.json（含指纹）+ proposals/。"""
    store = RecallContentStore(content_dir)
    original = store.read(TARGET)

    updated = store.read(TARGET)
    updated["blocks"][1]["series"][0]["points"] = [64.69, 70.3, 55.15, 55.15]
    updated["updated_at"] = "2026-09-22"
    if tamper_proposal:
        updated["title"] = "被篡改的标题"      # 模拟一份不该被写回的片段

    run_dir = tmp_path / "content-candidates" / "2026-09-23-1200"
    (run_dir / "proposals").mkdir(parents=True)
    # 必须用 ruamel 写：store.read() 返回的是 round-trip 类型，
    # PyYAML 的 safe_dump 不认（会抛 RepresenterError）。
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=2, offset=0)
    buf = io.StringIO()
    y.dump(updated, buf)
    (run_dir / "proposals" / f"{TARGET}.yaml").write_text(buf.getvalue(), encoding="utf-8")
    (run_dir / "run.meta.json").write_text(
        json.dumps(
            {
                "runId": "2026-09-23-1200",
                "counts": {"candidates": 1, "green": 1, "yellow": 0, "red": 0},
                "targetFingerprints": {TARGET: fingerprint(original)},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return run_dir


def run_apply(run_dir: Path, content_dir: Path, *extra: str) -> int:
    return int(
        main(
            [
                "apply",
                "--candidates", str(run_dir),
                "--content-dir", str(content_dir),
                *extra,
            ]
        )
    )


class TestApplyHappyPath:
    def test_writes_the_proposal_and_records_evidence(self, tmp_path: Path, content_dir: Path) -> None:
        run_dir = make_run_dir(tmp_path, content_dir)
        assert run_apply(run_dir, content_dir) == 0

        after = RecallContentStore(content_dir).read(TARGET)
        assert after["blocks"][1]["series"][0]["points"] == [64.69, 70.3, 55.15, 55.15]
        assert after["updated_at"] == "2026-09-22"

        record = json.loads((run_dir / "applied.json").read_text(encoding="utf-8"))
        assert [a["target"] for a in record["applied"]] == [TARGET]
        assert record["skipped"] == []
        assert record["applied"][0]["before"] != record["applied"][0]["after"]

    def test_only_the_target_record_changes(self, tmp_path: Path, content_dir: Path) -> None:
        store = RecallContentStore(content_dir)
        other_before = store.read("econ.macro.other")

        run_dir = make_run_dir(tmp_path, content_dir)
        run_apply(run_dir, content_dir)

        assert RecallContentStore(content_dir).read("econ.macro.other") == other_before

    def test_snapshot_of_the_original_is_kept(self, tmp_path: Path, content_dir: Path) -> None:
        """出问题时要有一条明确的还原路径。"""
        run_dir = make_run_dir(tmp_path, content_dir)
        before_text = RecallContentStore(content_dir).source_text(TARGET)

        run_apply(run_dir, content_dir)

        snapshot = run_dir / "pre-apply" / f"{TARGET}.yaml"
        assert snapshot.exists(), "写回前必须留下原文快照"
        assert snapshot.read_text(encoding="utf-8") == before_text

    def test_dry_run_writes_nothing(self, tmp_path: Path, content_dir: Path) -> None:
        run_dir = make_run_dir(tmp_path, content_dir)
        store = RecallContentStore(content_dir)
        before = store.source_text(TARGET)

        run_apply(run_dir, content_dir, "--dry-run")

        assert RecallContentStore(content_dir).source_text(TARGET) == before
        assert not (run_dir / "pre-apply").exists()
        record = json.loads((run_dir / "applied.json").read_text(encoding="utf-8"))
        assert record["dryRun"] is True

    def test_no_proposals_is_not_an_error(self, tmp_path: Path, content_dir: Path) -> None:
        """全红级 → 没有片段 → 不是错误（这正是设计如此）。"""
        run_dir = make_run_dir(tmp_path, content_dir)
        for f in (run_dir / "proposals").glob("*.yaml"):
            f.unlink()
        assert run_apply(run_dir, content_dir) == 0


class TestFingerprintGuard:
    """本命令最重要的一条守卫。"""

    def test_refuses_when_content_changed_since_the_run(
        self, tmp_path: Path, content_dir: Path, capsys
    ) -> None:
        run_dir = make_run_dir(tmp_path, content_dir)

        # 运行之后、写回之前，内容被人手工改过
        store = RecallContentStore(content_dir)
        edited = store.read(TARGET)
        edited["source"] = "有人手工改了这行"
        store.write(TARGET, edited)

        code = run_apply(run_dir, content_dir)

        assert code == 1, "指纹不符时必须返回非 0"
        assert "被改动过" in capsys.readouterr().err
        after = RecallContentStore(content_dir).read(TARGET)
        assert after["source"] == "有人手工改了这行", "被改动的内容不得被覆盖"
        assert after["blocks"][1]["series"][0]["points"] == [88, 72, 54, 46], (
            "数值也不得被写回——整条候选都应当被拒绝，而不是部分应用"
        )

    def test_fingerprint_ignores_formatting_only_changes(
        self, tmp_path: Path, content_dir: Path
    ) -> None:
        """指纹取的是**语义**：只重新排版（不改变值）不得导致写回被拒。

        否则"有人用编辑器保存过一次"就会让写回全部失败——那时守卫会从保护变成障碍。
        """
        from beacon.core.path import clone

        store = RecallContentStore(content_dir)
        a = store.read(TARGET)

        # 同一份语义，但 dict 的键顺序不同
        b = {k: clone(v) for k, v in reversed(list(a.items()))}
        assert fingerprint(a) == fingerprint(b)

    def test_fingerprint_changes_when_a_value_changes(self, content_dir: Path) -> None:
        store = RecallContentStore(content_dir)
        a = store.read(TARGET)
        b = store.read(TARGET)
        b["blocks"][1]["series"][0]["points"] = [1, 2, 3, 4]
        assert fingerprint(a) != fingerprint(b)

    def test_missing_fingerprint_is_refused(self, tmp_path: Path, content_dir: Path) -> None:
        run_dir = make_run_dir(tmp_path, content_dir)
        meta = json.loads((run_dir / "run.meta.json").read_text(encoding="utf-8"))
        meta["targetFingerprints"] = {}
        (run_dir / "run.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
        assert run_apply(run_dir, content_dir) == 1


class TestApplyGuards:
    def test_unknown_target_is_skipped(self, tmp_path: Path, content_dir: Path) -> None:
        run_dir = make_run_dir(tmp_path, content_dir)
        (run_dir / "proposals" / "no.such.target.yaml").write_text("a: 1\n", encoding="utf-8")
        assert run_apply(run_dir, content_dir) == 1

    def test_broken_proposal_is_skipped_not_crashed(
        self, tmp_path: Path, content_dir: Path
    ) -> None:
        run_dir = make_run_dir(tmp_path, content_dir)
        (run_dir / "proposals" / "hotspot.data.broken.yaml").write_text(
            "a: [1, 2\n", encoding="utf-8"
        )
        assert run_apply(run_dir, content_dir) == 1

    def test_missing_meta_is_refused(self, tmp_path: Path, content_dir: Path) -> None:
        run_dir = tmp_path / "not-a-run"
        run_dir.mkdir()
        assert run_apply(run_dir, content_dir) == 2

    def test_missing_content_dir_is_refused(self, tmp_path: Path, capsys) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "run.meta.json").write_text("{}", encoding="utf-8")
        code = int(main(["apply", "--candidates", str(run_dir), "--content-dir", str(tmp_path / "nope")]))
        assert code == 2
        assert "内容库错误" in capsys.readouterr().err

    def test_apply_does_not_shell_out(self) -> None:
        """写回**不得**调用外部命令（尤其不得调用 Node 工具链）。

        内容门禁是 App 侧的工具，而这个包必须不依赖 Node（IND-7）。
        所以 apply 只把文件改对，然后告诉人下一步该跑什么。
        """
        src = (Path(__file__).resolve().parent.parent / "beacon" / "cli.py").read_text(
            encoding="utf-8"
        )
        apply_src = src.split("def cmd_apply")[1].split("def _load_yaml")[0]
        # 检查的是**调用形态**而不是词本身：`npm run gate` 会作为"下一步该跑什么"
        # 出现在提示文字里，那是给人看的，不是工具去执行。
        for token in ("import subprocess", "os.system(", "Popen(", "check_output(", "os.exec"):
            assert token not in apply_src, f"cmd_apply 里出现了对外部命令的调用：{token}"


class TestProposalsAreMergedPerTarget:
    """落在同一目标上的多条候选必须**合并成一份片段**。

    文件名只能是目标 id，所以"一条候选一个文件"时后写的会覆盖先写的——
    先写那条的改动整条丢失。实测踩过：PMI 的制造业与非制造业在同一个数据块的两个
    series 里，跑完之后**非制造业更新了、制造业还是旧值**，
    而产物看起来完全正常（一个 proposal 文件、applied.json 里也只有一条记录）。
    """

    def _changeset(self) -> ChangeSet:
        from datetime import datetime

        from beacon.core.candidate import Candidate, FieldChange
        from beacon.core.leveling import Level, LevelDecision

        def doc(man: list, non: list) -> dict:
            return {
                "id": TARGET,
                "source": "旧的出处",
                "updated_at": "2026-09-01",
                "blocks": [
                    {"type": "prose", "label": "定义", "body": "…"},
                    {
                        "type": "dataviz",
                        "label": "制造业与非制造业 PMI",
                        "xTicks": ["2026-07", "2026-08"],
                        "series": [
                            {"name": "制造业 PMI", "points": man},
                            {"name": "非制造业 PMI", "points": non},
                        ],
                        "source": "旧的块级出处",
                    },
                ],
            }

        green = LevelDecision(Level.GREEN, "G1", "仅涉及可写字段")
        original = doc([49.2, 49.8], [49.0, 49.0])

        a = Candidate(
            target=TARGET, indicator="ind.manufacturing", level=green,
            changes=[
                FieldChange("blocks[1].series[0].points", "series", [49.2, 49.8], [50.0, 50.5],
                            changed_count=2),
                FieldChange("updated_at", "scalar", "2026-09-01", "2026-08-31"),
            ],
            checks=[], as_of=None, sources=[], fetched_at=None,
            proposal=doc([50.0, 50.5], [49.0, 49.0]),
        )
        b = Candidate(
            target=TARGET, indicator="ind.non-manufacturing", level=green,
            changes=[
                FieldChange("blocks[1].series[1].points", "series", [49.0, 49.0], [50.1, 49.4],
                            changed_count=2),
                FieldChange("updated_at", "scalar", "2026-09-01", "2026-08-31"),
            ],
            checks=[], as_of=None, sources=[], fetched_at=None,
            proposal=doc([49.2, 49.8], [50.1, 49.4]),
        )
        return ChangeSet(
            run_id="r", generated_at=datetime(2026, 9, 23),
            candidates=[a, b],
        )

    def test_both_series_end_up_in_one_document(self) -> None:
        from beacon.cli import _merge_proposals
        from beacon.core.path import get as path_get

        merged, conflicts = _merge_proposals(self._changeset(), store=None)

        assert conflicts == []
        assert list(merged) == [TARGET]
        doc = merged[TARGET]
        assert path_get(doc, "blocks[1].series[0].points") == [50.0, 50.5], "第一条的改动不能丢"
        assert path_get(doc, "blocks[1].series[1].points") == [50.1, 49.4], "第二条的改动也要在"

    def test_a_single_candidate_is_passed_through_unchanged(self) -> None:
        from beacon.cli import _merge_proposals

        cs = self._changeset()
        cs.candidates = cs.candidates[:1]
        merged, conflicts = _merge_proposals(cs, store=None)
        assert conflicts == []
        assert merged[TARGET] is cs.candidates[0].proposal

    def test_conflicting_values_are_reported_not_silently_picked(self) -> None:
        """两条候选给同一路径写了不同的值 → 报冲突，而不是静默选最后一个。"""
        from beacon.cli import _merge_proposals
        from beacon.core.candidate import FieldChange

        cs = self._changeset()
        cs.candidates[1].changes.append(
            FieldChange("updated_at", "scalar", "2026-09-01", "2026-09-15")  # 与第一条不同
        )
        cs.candidates[1].proposal["updated_at"] = "2026-09-15"

        merged, conflicts = _merge_proposals(cs, store=None)

        assert conflicts, "必须报出冲突"
        assert "updated_at" in conflicts[0]
        assert TARGET not in merged, "有冲突的目标本次不产出片段"


class TestProposalsAreMergedPerTarget:
    """落在同一目标上的多条候选必须**合并成一份片段**。

    文件名只能是目标 id，所以"一条候选一个文件"时后写的会覆盖先写的——
    先写那条的改动整条丢失。实测踩过：PMI 的制造业与非制造业在同一个数据块的两个
    series 里，跑完之后**非制造业更新了、制造业还是旧值**，
    而产物看起来完全正常（一个 proposal 文件、applied.json 里也只有一条记录）。
    """

    def _changeset(self) -> ChangeSet:
        from datetime import datetime

        from beacon.core.candidate import Candidate, FieldChange
        from beacon.core.leveling import Level, LevelDecision

        def doc(man: list, non: list) -> dict:
            return {
                "id": TARGET,
                "source": "旧的出处",
                "updated_at": "2026-09-01",
                "blocks": [
                    {"type": "prose", "label": "定义", "body": "…"},
                    {
                        "type": "dataviz",
                        "label": "制造业与非制造业 PMI",
                        "xTicks": ["2026-07", "2026-08"],
                        "series": [
                            {"name": "制造业 PMI", "points": man},
                            {"name": "非制造业 PMI", "points": non},
                        ],
                        "source": "旧的块级出处",
                    },
                ],
            }

        green = LevelDecision(Level.GREEN, "G1", "仅涉及可写字段")
        original = doc([49.2, 49.8], [49.0, 49.0])

        a = Candidate(
            target=TARGET, indicator="ind.manufacturing", level=green,
            changes=[
                FieldChange("blocks[1].series[0].points", "series", [49.2, 49.8], [50.0, 50.5],
                            changed_count=2),
                FieldChange("updated_at", "scalar", "2026-09-01", "2026-08-31"),
            ],
            checks=[], as_of=None, sources=[], fetched_at=None,
            proposal=doc([50.0, 50.5], [49.0, 49.0]),
        )
        b = Candidate(
            target=TARGET, indicator="ind.non-manufacturing", level=green,
            changes=[
                FieldChange("blocks[1].series[1].points", "series", [49.0, 49.0], [50.1, 49.4],
                            changed_count=2),
                FieldChange("updated_at", "scalar", "2026-09-01", "2026-08-31"),
            ],
            checks=[], as_of=None, sources=[], fetched_at=None,
            proposal=doc([49.2, 49.8], [50.1, 49.4]),
        )
        return ChangeSet(
            run_id="r", generated_at=datetime(2026, 9, 23),
            candidates=[a, b],
        )

    def test_both_series_end_up_in_one_document(self) -> None:
        from beacon.cli import _merge_proposals
        from beacon.core.path import get as path_get

        merged, conflicts = _merge_proposals(self._changeset(), store=None)

        assert conflicts == []
        assert list(merged) == [TARGET]
        doc = merged[TARGET]
        assert path_get(doc, "blocks[1].series[0].points") == [50.0, 50.5], "第一条的改动不能丢"
        assert path_get(doc, "blocks[1].series[1].points") == [50.1, 49.4], "第二条的改动也要在"

    def test_a_single_candidate_is_passed_through_unchanged(self) -> None:
        from beacon.cli import _merge_proposals

        cs = self._changeset()
        cs.candidates = cs.candidates[:1]
        merged, conflicts = _merge_proposals(cs, store=None)
        assert conflicts == []
        assert merged[TARGET] is cs.candidates[0].proposal

    def test_conflicting_values_are_reported_not_silently_picked(self) -> None:
        """两条候选给同一路径写了不同的值 → 报冲突，而不是静默选最后一个。"""
        from beacon.cli import _merge_proposals
        from beacon.core.candidate import FieldChange

        cs = self._changeset()
        cs.candidates[1].changes.append(
            FieldChange("updated_at", "scalar", "2026-09-01", "2026-09-15")  # 与第一条不同
        )
        cs.candidates[1].proposal["updated_at"] = "2026-09-15"

        merged, conflicts = _merge_proposals(cs, store=None)

        assert conflicts, "必须报出冲突"
        assert "updated_at" in conflicts[0]
        assert TARGET not in merged, "有冲突的目标本次不产出片段"
