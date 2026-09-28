"""分级闸门 F7（PRD §4.8）。

判定顺序是刻意的：**从红开始，第一个命中的即为结果。**
反过来的写法（"满足 a、b、c…才是绿"）有个隐蔽的坏处——每加一条新规则都要回头
检查旧规则的组合是否被打破；而顺序命中的写法里，新规则插进来不会改变已有结论。

每条结论都必须带 `reason`，且 reason 要能回答**"为什么不是更高一级"**。
只给级别不给原因的分级，等于让人去猜机器的判断依据——那还不如不分级。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .crosscheck import Status


class Level(str, Enum):
    RED = "red"
    YELLOW = "yellow"
    GREEN = "green"

    @property
    def zh(self) -> str:
        return {"red": "红", "yellow": "黄", "green": "绿"}[self.value]

    @property
    def rank(self) -> int:
        """越大越严重。用于排序与人审文件置顶。"""
        return {"green": 0, "yellow": 1, "red": 2}[self.value]


@dataclass(frozen=True)
class LevelDecision:
    level: Level
    rule: str
    reason: str


@dataclass(frozen=True)
class LevelInputs:
    """分级的全部输入。**每一项都必须能追溯到一次具体的检查或一次具体的比对。**"""

    # 红级触发器
    touched_readonly_field: tuple[str, ...] = ()
    """改到了解读文字字段（白名单外的文字）。空元组表示没碰到。"""

    flagged: bool = False
    flag_summary: str = ""

    first_real_replacement: bool = False
    """首次以真实数据替换示意数据 / 槽位语义不符 —— 属结构性变更。"""

    structure_change: bool = False
    structure_summary: str = ""

    checks_contradict: bool = False
    contradiction_summary: str = ""

    # 黄级触发器
    jump_exceeded: bool = False
    jump_summary: str = ""

    check_status: Status = "pass"
    check_detail: str = ""

    missing_periods: tuple[str, ...] = ()


def decide(inp: LevelInputs) -> LevelDecision:
    # ---- 红：改动落在可写白名单之外 -------------------------------------------
    if inp.touched_readonly_field:
        fields = "、".join(sorted(set(inp.touched_readonly_field)))
        return LevelDecision(
            Level.RED, "R1",
            f"改动落在可写白名单之外（{fields}）。解读文字与结构字段本就在白名单之外——"
            f"这不是配置漏项，而是这份白名单存在的意义："
            f"让「能改哪些内容」成为可审计的枚举。"
            f"需要改这些字段时，必须由人直接编辑内容。",
        )

    # ---- 红：该概念有未处理的「待修正」标记 -----------------------------------
    if inp.flagged:
        return LevelDecision(
            Level.RED, "R2",
            f"该概念有未处理的「待修正」标记（{inp.flag_summary}）。"
            f"在标记被处理前，任何自动改动都可能是建立在错误内容上的修补。",
        )

    # ---- 红：首次以真实数据替换示意数据 ---------------------------------------
    if inp.first_real_replacement:
        return LevelDecision(
            Level.RED, "R3",
            "首次以真实数据替换示意数据：数据点数、刻度语义与图的含义会同时改变，"
            "这不是数值刷新而是结构性变更。合入前需人工确认这个槽位适合承载该指标。",
        )

    # ---- 红：结构性变更（增删块、映射改动会改变结构签名）----------------------
    if inp.structure_change:
        return LevelDecision(
            Level.RED, "R4",
            f"本次改动会改变内容的结构（{inp.structure_summary}）。"
            f"结构是人的编排，工具只负责填数。",
        )

    # ---- 红：C1 与 C2 结论互相矛盾 -------------------------------------------
    if inp.checks_contradict:
        return LevelDecision(
            Level.RED, "R5",
            f"两条校验结论互相矛盾：{inp.contradiction_summary}。"
            f"这说明至少有一条校验的前提不成立，必须人判。",
        )

    # ---- 黄：触发跳变阈值 ----------------------------------------------------
    if inp.jump_exceeded:
        return LevelDecision(
            Level.YELLOW, "Y1",
            f"触发跳变阈值：{inp.jump_summary}。"
            f"超阈值不代表数据是错的，只代表这里值得人多看一眼，所以降黄而不丢弃。",
        )

    # ---- 黄：校验超容差 ------------------------------------------------------
    if inp.check_status == "fail":
        return LevelDecision(
            Level.YELLOW, "Y2",
            f"交叉校验未通过：{inp.check_detail}",
        )

    # ---- 黄：序列缺期 --------------------------------------------------------
    if inp.missing_periods:
        gaps = "、".join(inp.missing_periods[:6])
        more = f" 等 {len(inp.missing_periods)} 期" if len(inp.missing_periods) > 6 else ""
        return LevelDecision(
            Level.YELLOW, "Y3",
            f"序列缺期：{gaps}{more}。缺期可能是口径调整，也可能是源漏发——"
            f"直接丢弃会掩盖问题，所以保留并交人判断。",
        )

    # ---- 黄：声明的校验跑不动 ------------------------------------------------
    if inp.check_status == "unavailable":
        return LevelDecision(
            Level.YELLOW, "Y4",
            f"声明的交叉校验本次无法执行：{inp.check_detail}"
            f"（**检查跑不了不等于检查通过了**）。",
        )

    # ---- 绿 ------------------------------------------------------------------
    return LevelDecision(
        Level.GREEN, "G1",
        f"仅涉及可写字段；{inp.check_detail or '已通过声明的校验'}；无跳变、无缺期、无待修正标记。",
    )


def sort_key(decision: LevelDecision) -> int:
    """人审文件的排序键：红在前、绿在后。"""
    return -decision.level.rank
