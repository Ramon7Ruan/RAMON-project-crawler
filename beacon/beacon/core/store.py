"""目标文档的读写接口。

**为什么要有这一层**
------------------
core 处理的是"某个目标文档的某个字段"。至于这个文档是 YAML 还是 JSON、
是一整个文件还是文件里的一条记录、写回时要不要保序——**core 一概不知道，也不该知道**。

这不是洁癖。把这一层抽出来以后：

* 加第二个 app 只需实现一个 `TargetStore`，core 一行不改（IND-4 / A-C13）
* 测试可以用内存实现，不必造真实文件（NF-C9）
* "写回时如何保持格式"变成实现细节，可以各自最优

接口只有四个方法：存在吗、读一份、写一份、列出全部目标。
刻意不提供"部分读取"或"流式遍历"——候选生成是"逐目标"进行的，
提供批量接口只会诱导写出难以测试的代码。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class TargetError(RuntimeError):
    """目标文档层错误：目标不存在、结构不符合预期、写回失败。"""


@runtime_checkable
class TargetStore(Protocol):
    """目标文档仓库。实现方负责格式、保序与落盘。"""

    def targets(self) -> list[str]:
        """全部目标 id。"""
        ...

    def exists(self, target_id: str) -> bool:
        ...

    def read(self, target_id: str) -> Any:
        """读取并返回**可自由修改的副本**（调用方会原地改它）。

        必须返回副本：core 会拿它在内存里改出"改完之后长什么样"，
        若返回的是仓库内部对象的引用，一次未采纳的红级建议就会污染真实数据。
        """
        ...

    def write(self, target_id: str, document: Any) -> None:
        """落盘。实现方负责保序与格式保持。"""
        ...


class MemoryStore(TargetStore):
    """内存实现。测试与"试跑不落盘"都用它。"""

    def __init__(self, documents: dict[str, Any] | None = None) -> None:
        from .path import clone

        self._docs: dict[str, Any] = {k: clone(v) for k, v in (documents or {}).items()}
        self.writes: list[str] = []

    def targets(self) -> list[str]:
        return sorted(self._docs)

    def exists(self, target_id: str) -> bool:
        return target_id in self._docs

    def read(self, target_id: str) -> Any:
        from .path import clone

        if target_id not in self._docs:
            raise TargetError(f"目标不存在：{target_id}")
        return clone(self._docs[target_id])

    def write(self, target_id: str, document: Any) -> None:
        from .path import clone

        if target_id not in self._docs:
            raise TargetError(f"目标不存在：{target_id}")
        self._docs[target_id] = clone(document)
        self.writes.append(target_id)

    def snapshot(self, target_id: str) -> Any:
        return self.read(target_id)
