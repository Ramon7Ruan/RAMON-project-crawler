"""排版风格的保持（不依赖任何具体的 YAML 库）。

这一层处理的是一件很小但会反复咬人的事：**空序列的风格是"未定"的。**

保序 YAML 库把"行内写法"这个选择记录在容器对象上。但作者写 `xTicks: []` 时，
加载出来的 `flow_style()` 是 **None（未定）**，而不是 True。于是当我们往里填内容后，
库按默认渲染成块状：

    - xTicks: []                                  ← 原来 1 行
    + xTicks:                                     ← 变成 8 行
    + - 2026-03
    + - 2026-04 …

要紧的不是这一次多出 7 行，而是**它会每期复发**：一旦落成块状，
后续每次只改一个点都会重写整个列表，人审 diff 就此长期不可用。

**判断依据**：作者写的 `[]` 本身就是行内写法。所以这不是我们强加风格，
而是把作者已经选过的风格保住。

实现刻意用**鸭子类型**（找容器上的 `fa` 属性），不 import 任何 YAML 库：
这样 core 依然与具体序列化实现无关，而调用方可以是任何保序库。
"""

from __future__ import annotations

from typing import Any


def stamp_flow_style(document: Any) -> None:
    """把"风格未定"的序列/映射钉成行内写法。**原地修改，无返回值。**

    只处理"未定"（`flow_style() is None`）的容器：

    * 已经明确是行内的 → 不动（本来就会渲染成行内）
    * 已经明确是块状的 → **不动**（那是作者的选择，不能覆盖）
    * 未定的 → 钉成行内

    递归到子容器，因为一个块的多个字段可能各自是未定的。
    """
    fa = getattr(document, "fa", None)
    if fa is not None:
        try:
            if fa.flow_style() is None:
                fa.set_flow_style()
        except (AttributeError, TypeError):  # pragma: no cover - 非保序库没有 fa
            pass

    if isinstance(document, dict):
        for value in document.values():
            stamp_flow_style(value)
    elif isinstance(document, list):
        for value in document:
            stamp_flow_style(value)
