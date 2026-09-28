"""配置加载（CORE-4）。

三条纪律
--------
1. **配置是数据，不是代码。** 改阈值、改映射、改运行参数都不该碰 Python。
2. **配置错误必须在启动时报出来。** 映射到不存在的字段路径属配置错误，
   不是"这次数据不好"——两者混淆会让一次配置失误伪装成一次正常运行。
3. **路径全部可覆盖。** 默认值指向包内 `config/`，调用方可以整套换掉。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .path import validate_path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
"""包内配置目录。

注意：这是**包自己的**默认配置，不是父项目的路径。
所有对外输入输出路径都由调用方传入（见 IND-6）。
"""


class ConfigError(RuntimeError):
    """配置层错误。必须在启动时抛出，不要留到处理数据时。"""


# --------------------------------------------------------------------------- #
# 原始文档加载
# --------------------------------------------------------------------------- #


def load_yaml(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"配置文件不存在：{p}")
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件无法解析：{p}\n{exc}") from exc
    if not isinstance(doc, dict):
        raise ConfigError(f"配置文件顶层必须是映射（key: value）：{p}")
    return doc


# --------------------------------------------------------------------------- #
# 指标映射表
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Slot:
    """目标字段在当前文档里**承载着什么语义**。

    存在的理由：只有知道槽位"本该是什么"，才能判断这次替换是**数值刷新**
    还是**结构性变更**。例如把"新订单"的槽位塞进"制造业综合 PMI"，
    名与含义都变了——那是改写内容，必须人审。
    """

    path: str
    series_name: str | None = None
    axis_labels: tuple[str, ...] | None = None
    series_label: str | None = None
    """整个块在图上的语义（如「制造业 / 非制造业 PMI」）。块级溯源模板要用它。"""


@dataclass(frozen=True)
class Transform:
    operator: str | None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Mapping:
    """一个指标要写到哪、怎么写、允许改什么、声明了什么校验。"""

    indicator: str
    target: str
    role: str
    slot: Slot
    transform: Transform
    checks: tuple[str, ...]
    writable_fields: tuple[str, ...]
    checks_extra: dict[str, Any] = field(default_factory=dict)
    expected_caliber: str | None = None
    text_targets: dict[str, str] = field(default_factory=dict)
    """模板名 → 写到哪个字段路径。

    **core 不认识任何具体字段名。** 曾经把 `source` / `updated_at` 写死在漏斗代码里，
    因为那样跑 Recall 没问题；接第二个消费者时立刻暴露——那是把某一个 app 的字段名
    混进了源无关层。现在由配置声明。
    """
    text_targets: dict[str, str] = field(default_factory=dict)
    """模板名 → 写到哪个字段路径。

    **core 不认识任何具体字段名。** 曾经把 `source` / `updated_at` 写死在漏斗代码里，
    因为那样跑 Recall 没问题；接第二个消费者时立刻暴露——那是把某一个 app 的字段名
    混进了源无关层。现在由配置声明。
    """

    def allows(self, field_path: str) -> bool:
        return field_path in self.writable_fields

    @property
    def is_check_only(self) -> bool:
        """只用于校验、不进内容的指标（如给 C2 提供第二口径的同比序列）。"""
        return self.role == "check-only"


@dataclass(frozen=True)
class MappingTable:
    mappings: dict[str, Mapping]
    operators: dict[str, Any]
    templates: dict[str, str] = field(default_factory=dict)
    """溯源文字模板。**空模板集是非法配置**——没有模板就没法生成溯源文字，
    而溯源文字是新值的必要组成部分（否则新数据挂着一个描述旧数据的出处）。"""

    def get(self, indicator: str) -> Mapping | None:
        return self.mappings.get(indicator)

    def known_indicators(self) -> set[str]:
        return set(self.mappings)

    def targets(self) -> dict[str, list[Mapping]]:
        """按目标文档分组——写回时是按文档写的，不是按指标写的。"""
        out: dict[str, list[Mapping]] = {}
        for m in self.mappings.values():
            out.setdefault(m.target, []).append(m)
        return out


def load_mapping(path: Path | str | None = None) -> MappingTable:
    doc = load_yaml(path or CONFIG_DIR / "mapping.yaml")

    operators = doc.get("operators") or {}
    templates = dict(doc.get("templates") or {})
    for key in ("revision_date", "item_source", "block_source"):
        if not templates.get(key):
            raise ConfigError(f"mapping.yaml 的 templates 缺少 {key!r}")
    raw = doc.get("indicators") or {}
    if not raw:
        raise ConfigError("映射表为空：没有任何指标声明了写入目标，运行起来会一条候选都产不出")

    # check-only 指标与内容指标走**同一套**结构，只是不需要 slot 与白名单。
    # 让它们共用一套解析，是为了避免出现"只在某一条路径上生效"的校验缺口。
    for indicator, spec in (doc.get("check_only_indicators") or {}).items():
        if indicator in raw:
            raise ConfigError(f"{indicator} 同时出现在 indicators 与 check_only_indicators")
        merged = dict(spec)
        merged.setdefault("role", "check-only")
        merged.setdefault("slot", {"path": "meta.check_only"})   # 占位，不会被写入
        merged.setdefault("writable_fields", [])
        merged.setdefault("target", spec.get("target") or "")
        raw = {**raw, indicator: merged}

    mappings: dict[str, Mapping] = {}
    for indicator, spec in raw.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"指标 {indicator} 的映射必须是映射结构")

        is_check_only = str(spec.get("role") or "content") == "check-only"
        required_fields = ("target",) if is_check_only else ("target", "slot", "writable_fields")
        for required in required_fields:
            if required not in spec:
                raise ConfigError(f"指标 {indicator} 的映射缺少字段 {required!r}")

        slot_raw = spec["slot"] or {}
        if "path" not in slot_raw:
            raise ConfigError(f"指标 {indicator} 的 slot 缺少 path")
        if is_check_only:
            slot_raw = {"path": str(slot_raw["path"])}
        slot = Slot(
            path=validate_path(slot_raw["path"]),
            series_name=slot_raw.get("series_name"),
            axis_labels=(
                tuple(slot_raw["xTicks"]) if slot_raw.get("xTicks") is not None else None
            ),
            series_label=slot_raw.get("series_label"),
        )

        tf_raw = spec.get("transform") or {}
        transform = Transform(
            operator=tf_raw.get("operator"),
            params=dict(tf_raw.get("params") or {}),
        )
        if transform.operator and transform.operator not in operators:
            raise ConfigError(
                f"指标 {indicator} 声明了未注册的算子 {transform.operator!r}——"
                f"派生公式必须登记在 operators 段，否则「派生可复现」只是口头承诺"
            )

        writables = tuple(validate_path(f) for f in spec.get("writable_fields") or [])
        if not is_check_only and slot.path not in writables:
            raise ConfigError(
                f"指标 {indicator} 的写入目标 {slot.path!r} 不在 writable_fields 里——"
                f"白名单漏了目标字段，运行到 F6 会被自己拦下"
            )

        text_targets = {
            str(k): validate_path(str(v))
            for k, v in (spec.get("text_targets") or {}).items()
        }
        for name, path in text_targets.items():
            if name not in templates:
                raise ConfigError(f"指标 {indicator} 的 text_targets 引用了未定义的模板 {name!r}")
            if path not in writables:
                raise ConfigError(
                    f"指标 {indicator} 的 text_targets[{name}] 指向 {path!r}，"
                    f"但它不在 writable_fields 里——那样运行到 F6 会被自己拦下"
                )

        text_targets = {
            str(k): validate_path(str(v))
            for k, v in (spec.get("text_targets") or {}).items()
        }
        for name, path in text_targets.items():
            if name not in templates:
                raise ConfigError(f"指标 {indicator} 的 text_targets 引用了未定义的模板 {name!r}")
            if path not in writables:
                raise ConfigError(
                    f"指标 {indicator} 的 text_targets[{name}] 指向 {path!r}，"
                    f"但它不在 writable_fields 里——那样运行到 F6 会被自己拦下"
                )

        checks = tuple(spec.get("checks") or ())
        for c in checks:
            if c not in ("C1", "C2", "C3"):
                raise ConfigError(f"指标 {indicator} 声明了未知的校验手段 {c!r}")

        mappings[indicator] = Mapping(
            indicator=indicator,
            target=str(spec["target"]),
            role=str(spec.get("role") or "content"),
            slot=slot,
            transform=transform,
            checks=checks,
            writable_fields=writables,
            checks_extra=dict(spec.get("checks_extra") or {}),
            expected_caliber=spec.get("expected_caliber"),
            text_targets=text_targets,
        )

    # 同一槽位被两个指标写入 → 后者会覆盖前者。这是配置错误，不是运行时问题。
    claimed: dict[tuple[str, str], str] = {}
    for m in mappings.values():
        if m.is_check_only:
            continue
        key = (m.target, m.slot.path)
        if key in claimed:
            raise ConfigError(
                f"槽位冲突：{m.target} 的 {m.slot.path} 同时被 "
                f"{claimed[key]} 与 {m.indicator} 声明"
            )
        claimed[key] = m.indicator

    return MappingTable(mappings=mappings, operators=operators, templates=templates)


# --------------------------------------------------------------------------- #
# 阈值与设置
# --------------------------------------------------------------------------- #


@dataclass
class Thresholds:
    value_range: dict[str, Any]
    value_range_of: dict[str, str]
    jump: dict[str, Any]
    tolerance: dict[str, float]
    tolerance_of: dict[str, str]
    allowed_units: list[str]
    period_patterns: dict[str, str]
    publication_convention: dict[str, str] = field(default_factory=dict)
    jump_of: dict[str, str] = field(default_factory=dict)

    def convention_for(self, indicator: str) -> str | None:
        """该指标的发布日按什么惯例确定。None = 源必须自己给出发布日。"""
        return self.publication_convention.get(indicator)

    def range_for(self, indicator: str) -> tuple[float, float, str] | None:
        """返回 (min, max, 说明)。未声明值域的指标返回 None —— 调用方必须当失败处理。"""
        key = self.value_range_of.get(indicator)
        if not key:
            return None
        spec = self.value_range.get(key)
        if not spec:
            return None
        return float(spec["min"]), float(spec["max"]), spec.get("desc", key)

    def jump_for(self, indicator: str) -> dict[str, Any] | None:
        """跳变阈值。优先用 `jump_of` 显式指定的档，否则退回值域那一档。

        之所以要能按指标覆盖：跳变要看的是**写进内容的那个量**。
        PE 抓的是"市盈率"、写进内容是"历史分位"，两者的合理波动幅度差一个量级。
        """
        key = self.jump_of.get(indicator) or self.value_range_of.get(indicator)
        return self.jump.get(key) if key else None

    def tolerance_for(self, indicator: str) -> tuple[float, str] | None:
        key = self.tolerance_of.get(indicator)
        if not key:
            return None
        return float(self.tolerance[key]), key


def load_thresholds(path: Path | str | None = None) -> Thresholds:
    doc = load_yaml(path or CONFIG_DIR / "thresholds.yaml")
    for required in ("value_range", "value_range_of", "jump", "tolerance", "tolerance_of"):
        if required not in doc:
            raise ConfigError(f"thresholds 缺少 {required!r} 段")
    return Thresholds(
        value_range=doc["value_range"],
        value_range_of=doc["value_range_of"],
        jump=doc["jump"],
        tolerance=doc["tolerance"],
        tolerance_of=doc["tolerance_of"],
        allowed_units=list(doc.get("allowed_units") or []),
        period_patterns=dict(doc.get("period_patterns") or {}),
        publication_convention=dict(doc.get("publication_convention") or {}),
        jump_of=dict(doc.get("jump_of") or {}),
    )


@dataclass
class Settings:
    relookback_periods: int
    sample: str
    max_points: int
    keep_latest: bool
    out_dir: str | None
    flags_path: str | None


def load_settings(path: Path | str | None = None) -> Settings:
    doc = load_yaml(path or CONFIG_DIR / "settings.yaml")
    f = doc.get("funnel") or {}
    s = (f.get("series") or {})

    n = int(f.get("relookback_periods", 6))
    if n < 2:
        # PRD §4.3 明确"强制，不可配置为 1"。
        # N=1 会让官方回改的历史值永远留在库里——那是这套机制存在的唯一理由。
        raise ConfigError(
            f"relookback_periods 必须 ≥ 2（当前 {n}）。"
            f"官方数据会回改历史值，只取最新一期等于放弃修正。"
        )

    return Settings(
        relookback_periods=n,
        sample=str(s.get("sample", "last-in-period")),
        max_points=int(s.get("max_points", 60)),
        keep_latest=bool(s.get("keep_latest", True)),
        out_dir=(doc.get("candidates") or {}).get("out_dir"),
        flags_path=(doc.get("flags") or {}).get("path"),
    )
