"""Recall 内容库的目标文档仓库。

它负责三件 core 不该知道的事
-----------------------------
1. **文件布局**：内容长在 `content/<区>/<区>.yaml`，每个文件是一个**列表**，
   列表里每条记录有一个 `id`。core 只知道"目标 id"。
2. **保序往返**：这些 YAML 是人工维护的（含缩进、块顺序、行内 flow-map 写法）。
   用普通 YAML 库读写会重排格式、丢注释，把 diff 变成噪音，
   从而让"人工审核"失去意义。所以用 `ruamel.yaml` 的 round-trip 模式。
3. **定位到列表里的第几条**：读出来的是那一条记录本身（一个映射），
   写回时替换回原位置，**其余记录一个字节都不动**。

关于 `ruamel.yaml`
------------------
必须用 `YAML(typ="rt")`（round-trip）。默认的 `typ="safe"` 是**保序的反面**——
它会重新格式化整份文档。这类问题不会报错，只会让 diff 变脏。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from ..core.path import clone
from ..core.store import TargetError
from ..core.style import stamp_flow_style

try:  # pragma: no cover - 依赖缺失时给出可执行的提示
    from ruamel.yaml import YAML
except ImportError as exc:  # pragma: no cover
    raise ImportError("需要 ruamel.yaml 才能读写内容库：pip install ruamel.yaml") from exc

ID_KEY = "id"
"""目标 id 在记录里的键名。**这是本 app 的约定，只出现在这一层。**"""


class RecallContentStore:
    """把内容库当作"一堆可按 id 定位的记录"来用。"""

    def __init__(self, content_dir: Path | str) -> None:
        self.content_dir = Path(content_dir)
        if not self.content_dir.exists():
            raise TargetError(f"内容目录不存在：{self.content_dir}")

        self._yaml = YAML(typ="rt")           # round-trip：保序、保注释、保行内写法
        self._yaml.preserve_quotes = True
        self._yaml.width = 4096               # 不让它为了换行把长字符串折断
        # ⚠️ 缩进必须与内容库的**规范形式**一致，否则写回会整份重排。
        #
        # 内容库的规范形式是「顶层序列顶格 + 嵌套序列与父键对齐」：
        #     - id: hotspot.data.pmi-reading      ← 顶层：短横线在第 0 列
        #       key_points:
        #       - 50 是荣枯线                      ← 嵌套：与父键对齐
        #
        # 为什么是这一种而不是"嵌套多缩 2 格"：ruamel 的
        # `indent(mapping, sequence, offset)` 用**同一个 offset** 同时决定
        # 顶层与嵌套序列的短横线位置，无法表达"顶层 0、嵌套 2"这种混合风格
        # （已穷举 7 组参数实测）。所以内容库在 2026-09-23 做了一次性规范化，
        # 统一到 ruamel 能逐字节复现的形式。工具见 `tools/normalize_content.py`。
        #
        # 这三个值与 `tools/normalize_content.py` 的 CANONICAL **必须一致**：
        # 不一致的症状是"规范化之后仍然重排"，而两边看起来都对，排查成本很高。
        self._yaml.indent(mapping=2, sequence=2, offset=0)
        self._files: dict[str, Path] = {}
        self._index: dict[str, tuple[Path, int]] = {}
        self._docs: dict[Path, Any] = {}
        self._scan()

    # ------------------------------------------------------------------ 扫描

    def _scan(self) -> None:
        for path in sorted(self.content_dir.rglob("*.yaml")):
            with path.open("r", encoding="utf-8") as fh:
                doc = self._yaml.load(fh)
            if not isinstance(doc, list):
                # 单条记录也允许（写成一份映射），统一包成列表再处理
                raise TargetError(f"{path} 顶层应为记录列表，实际是 {type(doc).__name__}")
            self._docs[path] = doc
            self._files[str(path)] = path
            for i, item in enumerate(doc):
                if not isinstance(item, dict) or ID_KEY not in item:
                    continue
                target_id = str(item[ID_KEY])
                if target_id in self._index:
                    raise TargetError(f"目标 id 重复：{target_id}（{path} 与 {self._index[target_id][0]}）")
                self._index[target_id] = (path, i)

    # --------------------------------------------------- TargetStore 协议实现

    def targets(self) -> list[str]:
        return sorted(self._index)

    def exists(self, target_id: str) -> bool:
        return target_id in self._index

    def read(self, target_id: str) -> Any:
        """返回该记录的**副本**。

        必须返回副本：core 会拿它在内存里改出"改完之后长什么样"。
        若返回内部引用，一条未被采纳的红级建议就会污染真实内容。
        """
        if target_id not in self._index:
            raise TargetError(f"内容库里没有这个概念：{target_id}")
        path, idx = self._index[target_id]
        return clone(self._docs[path][idx])

    def write(self, target_id: str, document: Any) -> None:
        """替换回原位置并落盘。**其余记录与文件格式保持不变。**"""
        if target_id not in self._index:
            raise TargetError(f"内容库里没有这个概念：{target_id}")
        path, idx = self._index[target_id]
        # 落盘前再钉一次：入口可能在别处（例如 proposal 是从文件加载的），
        # 而"未定"只出现在第一次；多钉一次不改变任何已定的风格。
        stamp_flow_style(document)
        self._docs[path][idx] = document
        self.save(path)

    def save(self, path: Path) -> None:
        with path.open("w", encoding="utf-8") as fh:
            self._yaml.dump(self._docs[path], fh)

    def save_all(self) -> None:
        for path in self._docs:
            self.save(path)

    # ------------------------------------------------------------------ 诊断

    def file_of(self, target_id: str) -> Path:
        return self._index[target_id][0]

    def source_text(self, target_id: str) -> str:
        """该目标所在文件的当前磁盘内容。用于"写回前后逐字节比对"的测试。"""
        return self.file_of(target_id).read_text(encoding="utf-8")

    def iterate_records(self) -> Iterator[tuple[str, Any]]:
        for target_id in sorted(self._index):
            yield target_id, self.read(target_id)
