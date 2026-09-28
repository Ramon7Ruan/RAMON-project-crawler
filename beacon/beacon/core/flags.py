"""读「待修正」清单（FB-1 / FB-2）。

文件由另一个 app 导出（PRD §5.4 A3），因此**一律当不可信输入**处理：
空文件、截断的 JSON、少字段、字段类型不对，都不许把整次采集带崩。

三种输入状态，处置各不相同
--------------------------
| 状态 | 算不算错 | 处置 |
|---|---|---|
| 文件不存在 | **不算错** | 正常继续，但在产物里记「未提供」 |
| 存在但无法解析 | **不算错** | 正常继续，但**必须显式标注**在产物顶部 |
| 正常 | — | 命中标记的目标强制红级 |

为什么"无法解析"不算错
----------------------
这是一个**尽力而为的增强输入**。因为一个辅助文件格式变了就让整次采集失败，
会让工具在最需要它的时候（内容恰好出问题、你正想标记它）不可用。

但**必须显式标注**——否则"这次确实没有标记"与"这次没读到标记"在产物里长得一样，
而这个区别对一个"防止错误内容被自动改写"的机制来说是致命的。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUPPORTED_SCHEMA = "1.0"

# 目标 id 的候选键名。
# 允许两种写法是刻意的：这个文件由另一个项目产出，接口尚未双向冻结（FB-3），
# 与其等到联调时才发现键名不一致，不如现在就容忍地读。
_TARGET_KEYS = ("targetId", "conceptId", "id")
_KIND_KEYS = ("kind", "type")
_NOTE_KEYS = ("note", "说明", "detail")
_AT_KEYS = ("at", "time", "createdAt")


@dataclass(frozen=True)
class Flag:
    target: str
    kind: str
    note: str

    @property
    def summary(self) -> str:
        return f"{self.kind}：{self.note}" if self.note else self.kind


@dataclass
class FlagsBook:
    """解析结果 + **它是怎么来的**。

    第二项不是附赠信息：产物里必须能看出"未提供 / 读不懂 / 已生效"，
    否则读产物的人无法判断"没有红级"到底是好事还是漏读。
    """

    by_target: dict[str, list[Flag]] = field(default_factory=dict)
    source: str = "missing"          # file / missing / unreadable
    detail: str = ""
    exported_at: str | None = None

    @property
    def usable(self) -> bool:
        return self.source == "file"

    def get(self, target: str) -> list[Flag]:
        return self.by_target.get(target, [])

    def has(self, target: str) -> bool:
        return bool(self.by_target.get(target))

    def banner(self) -> str:
        """给人审文件顶部用的一行说明。**不能是空字符串。**"""
        if self.source == "file":
            n = sum(len(v) for v in self.by_target.values())
            when = f"，导出时间 {self.exported_at}" if self.exported_at else ""
            return f"已读取待修正清单：{n} 条标记，覆盖 {len(self.by_target)} 个目标{when}。"
        if self.source == "missing":
            return "未提供待修正清单（这是正常的，不代表没有标记）。"
        return f"⚠️ 待修正清单存在但**无法解析**，本次未应用任何标记。原因：{self.detail}"


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def parse_flags(raw: str) -> FlagsBook:
    """解析清单文本。**任何畸形输入都返回 unreadable，不抛异常。**"""
    text = (raw or "").strip()
    if not text:
        return FlagsBook(source="unreadable", detail="文件为空")

    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        return FlagsBook(source="unreadable", detail=f"JSON 解析失败（{exc.msg}，第 {exc.lineno} 行）")

    if not isinstance(doc, dict):
        return FlagsBook(source="unreadable", detail=f"顶层应为对象，实际是 {type(doc).__name__}")

    version = doc.get("schemaVersion")
    if version is not None and str(version) != SUPPORTED_SCHEMA:
        # 不当作错误：宁可少用一条标记，也不要让整次采集失败。
        # 但必须说清"是按哪个版本读的"，否则未来换了版本会静默读错字段。
        return FlagsBook(
            source="unreadable",
            detail=f"schemaVersion 为 {version!r}，本版只认识 {SUPPORTED_SCHEMA!r}",
        )

    entries = doc.get("flags")
    if entries is None:
        entries = doc.get("items") or doc.get("marks") or []
    if not isinstance(entries, list):
        return FlagsBook(source="unreadable", detail="flags 字段不是数组")

    book = FlagsBook(
        source="file",
        detail="ok",
        exported_at=str(doc.get("exportedAt") or doc.get("exported_at") or "") or None,
    )
    for item in entries:
        if not isinstance(item, dict):
            continue
        target = _first(item, _TARGET_KEYS)
        if not target:
            continue  # 没有目标 id 的标记无法定位，丢弃它比猜一个更安全
        book.by_target.setdefault(str(target), []).append(
            Flag(
                target=str(target),
                kind=str(_first(item, _KIND_KEYS) or "其他"),
                note=str(_first(item, _NOTE_KEYS) or ""),
            )
        )
    return book


def load_flags(path: Path | str | None) -> FlagsBook:
    """按路径读取。路径为 None 或文件不存在 → `missing`（**不算错**）。"""
    if path is None:
        return FlagsBook(source="missing", detail="未指定路径")
    p = Path(path)
    if not p.exists():
        return FlagsBook(source="missing", detail=f"文件不存在：{p}")
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        return FlagsBook(source="unreadable", detail=f"无法读取：{exc}")
    except UnicodeDecodeError as exc:
        return FlagsBook(source="unreadable", detail=f"不是 UTF-8 文本：{exc}")
    return parse_flags(raw)
