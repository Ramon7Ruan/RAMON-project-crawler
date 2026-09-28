"""候选（Candidate）与变更集（ChangeSet）。

候选是什么
----------
一条候选 = 「某指标的本次数据，准备写进某个目标的某个槽位」这件事的完整说明：

* **改成什么**（变更前后的值）
* **为什么是这个级别**（`LevelDecision`，含规则编号与理由）
* **凭什么**（校验结论 + 溯源：哪个源、哪个 URL、什么时间）
* **能不能直接用**（只有绿/黄产出可直接应用的片段；红级只出建议）

"能不能直接用"这条是关键设计：红级**不产出**可直接合入的片段，
而不是"产出但标记为红"。后者迟早会有人手滑把红级片段合进去。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from .crosscheck import CheckOutcome
from .leveling import Level, LevelDecision
from .path import get as path_get
from .series import coerce_number, format_number
from .config import Mapping

ChangeKind = Literal["series", "scalar", "labels"]


@dataclass(frozen=True)
class FieldChange:
    """一处字段改动。**必须能说清"从什么变成什么"。**"""

    path: str
    kind: ChangeKind
    old: Any
    new: Any
    changed_count: int = 0
    """序列类改动：实际发生变化的点数（不是总点数）。"""

    @property
    def is_noop(self) -> bool:
        return self.old == self.new

    def summary(self) -> str:
        if self.kind == "series":
            old_list = list(self.old or [])
            new_list = list(self.new or [])
            if len(old_list) == len(new_list) and self.changed_count:
                return (
                    f"{self.path}：{len(new_list)} 点中 {self.changed_count} 点变化，"
                    f"首 {format_number(float(new_list[0]))} → 末 {format_number(float(new_list[-1]))}"
                )
            return f"{self.path}：{len(old_list)} 点 → {len(new_list)} 点"
        return f"{self.path}：{self.old!r} → {self.new!r}"


@dataclass
class Candidate:
    """一条待审的变更。"""

    target: str
    indicator: str
    level: LevelDecision
    changes: list[FieldChange]
    checks: list[CheckOutcome]
    as_of: date | None
    sources: list[str]
    fetched_at: datetime | None
    target_fingerprint: str | None = None
    """生成候选时目标文档的**语义指纹**。写回前会再算一次，不一致就拒绝写回。

    防的是：从生成候选到你确认之间，内容库被人手工改过。
    此时无条件写回会**静默覆盖那次手工修改**——写回"成功"、diff 也"对"，
    只是丢掉了一个不属于本次流程的改动。这是最难发现的一类数据丢失。
    """
    proposal: dict[str, Any] | None = None
    """改动后的目标文档（深拷贝）。**红级为 None** —— 不产出可合入的片段。"""

    suggestions: list[str] = field(default_factory=list)
    """红级时给出的"该怎么改"的文字建议（只出现在人审文件里）。"""

    @property
    def level_value(self) -> Level:
        return self.level.level

    @property
    def applies_automatically(self) -> bool:
        """能否产出可直接合入的片段。红级不行。"""
        return self.level_value in (Level.GREEN, Level.YELLOW) and self.proposal is not None

    @property
    def changed_paths(self) -> set[str]:
        return {c.path for c in self.changes}


@dataclass
class SourceFailure:
    source: str
    reason: str


@dataclass
class ChangeSet:
    """一次运行的全部产出。"""

    run_id: str
    generated_at: datetime
    candidates: list[Candidate] = field(default_factory=list)
    failures: list[SourceFailure] = field(default_factory=list)
    flags_banner: str = ""
    up_to_date: list[str] = field(default_factory=list)
    """本次跑完发现已经是最新的目标。

    单独列出来而不是塞进候选：一个"什么都没变"的候选进了候选列表，
    人审文件就要为它写一整节，而你只能看到"（无字段变化）"——
    那是纯噪音，还会稀释真正需要看的那几条。"""
    skipped: list[str] = field(default_factory=list)
    """被丢弃的观测及其原因（F6 不在映射表内等）。**必须留痕**，否则"为什么没有这条"无从回答。"""

    contract_version: str = "1.0"

    def by_level(self, level: Level) -> list[Candidate]:
        return [c for c in self.candidates if c.level_value is level]

    @property
    def counts(self) -> dict[str, int]:
        return {
            "green": len(self.by_level(Level.GREEN)),
            "yellow": len(self.by_level(Level.YELLOW)),
            "red": len(self.by_level(Level.RED)),
        }

    def sorted_candidates(self) -> list[Candidate]:
        """红在前、绿在后。人审文件的顺序应当由严重程度决定，不由抓取顺序决定。"""
        return sorted(
            self.candidates,
            key=lambda c: (-c.level_value.rank, c.target, c.indicator),
        )


# --------------------------------------------------------------------------- #
# 变更计算
# --------------------------------------------------------------------------- #


def compute_series_change(path: str, old_values: Any, new_values: list[float]) -> FieldChange:
    old_list = list(old_values or [])
    changed = sum(
        1
        for i, v in enumerate(new_values)
        if i >= len(old_list) or _num_differs(old_list[i], v)
    )
    return FieldChange(
        path=path,
        kind="series",
        old=old_list,
        new=list(new_values),
        changed_count=changed,
    )


def _num_differs(old: Any, new: float) -> bool:
    """比较旧值与新值。**把 `16` / `16.0` / `"16"` 视为同一个值。**

    不做这层归一化，人审文件会把格式差异报成数据变化，
    真正的变化就淹在噪音里了。
    """
    try:
        return abs(coerce_number(old) - float(new)) > 1e-9
    except ValueError:
        return True


def compute_scalar_change(path: str, old: Any, new: Any) -> FieldChange:
    return FieldChange(path=path, kind="scalar", old=old, new=new)


def compute_label_change(path: str, old: Any, new: Any) -> FieldChange:
    return FieldChange(path=path, kind="labels", old=old, new=new)


def changed_paths_outside_whitelist(
    changes: list[FieldChange], mapping: Mapping
) -> tuple[str, ...]:
    """改动集合里**不在白名单**的部分。

    正常情况下应当恒为空——因为这个模块只往白名单里的路径写。
    它存在的意义是：**万一哪天不是这样，必须能被发现**，而不是静默改掉别的内容。
    """
    return tuple(sorted({c.path for c in changes if not mapping.allows(c.path)}))


def is_illustrative_slot(document: Any, mapping: Mapping) -> bool:
    """判断目标槽位**当前承载的是示意数据还是真实数据**。

    两个线索，命中任一即认为是"还没有真实数据"：

    1. 该槽位所属块的 `source` 里含「示意」二字 —— 内容是作者自己声明的占位。
    2. 现有 `series[].name` 与映射声明的名字不一致 ——
       说明这个槽位当前装的是**别的东西**，不是"同一个东西的旧值"。

    第 2 条是核心：把"新订单"的槽位换成"制造业综合 PMI"，
    名与含义同时改变，这是改写内容而不是刷新数值。
    """
    parts = mapping.slot.path.split(".series[")
    if len(parts) != 2:
        return False
    block_path = parts[0]
    series_name_path = f"{block_path}.series[{parts[1].split(']')[0]}].name"

    # 线索 2：槽位语义不符
    try:
        name_now = path_get(document, series_name_path)
    except Exception:  # noqa: BLE001 —— 路径不存在时按"语义不符"处理更安全
        name_now = None
    if mapping.slot.series_name and name_now != mapping.slot.series_name:
        return True

    # 线索 1：块级 source 自称示意数据
    try:
        block_source = path_get(document, f"{block_path}.source")
    except Exception:  # noqa: BLE001
        block_source = None
    if isinstance(block_source, str) and "示意" in block_source:
        return True

    return False
