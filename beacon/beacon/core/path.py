"""字段路径的解析与读写（中性工具）。

为什么需要单独一个模块
----------------------
映射表里写的是 `blocks[1].series[0].points` 这类路径。这串东西对 core 而言是
**不透明字符串**——core 不知道 `blocks` 是什么，也不需要知道。
它只需要能回答三个问题：

    这个路径在文档里存在吗？  →  resolve()
    读出来是什么？            →  get()
    能不能只改它、别的一个字节都不动？ →  set()

第三条是整个工具能否被信任的关键：**「只改该改的」不是靠自觉，是靠机制**。
所以 `set()` 必须精确到路径最后一段，不做任何"顺手规范化"。

路径语法
--------
    a.b.c          普通键
    a[0].b         列表下标
    a[0][1]        连续下标
    a.b[2].c       混合

**不支持通配符**。理由：通配符会让"这次改了哪些位置"变得不确定，
而候选产物必须能逐字段说清"哪里从什么变成什么"。
需要多目标时，在映射表里写多条 —— 显式优于聪明。
"""

from __future__ import annotations

import copy
import re
from typing import Any

_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")

_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[0-9]+\]|\.[A-Za-z_][A-Za-z0-9_]*)*$")


class PathError(ValueError):
    """路径本身写得不对（配置错误，不是数据错误）。"""


class PathMissing(KeyError):
    """路径合法，但目标文档里不存在（映射与内容对不上）。"""


def validate_path(path: str) -> str:
    """校验路径写法。**在加载配置时就查**，不要等到改内容时才发现写错了。"""
    if not path or not _PATH_RE.match(path):
        raise PathError(f"字段路径写法非法：{path!r}（示例：items[1].values[0].points）")
    return path


def parse(path: str) -> list[str | int]:
    """把路径拆成 token 序列。字符串键与整数下标混排。"""
    validate_path(path)
    tokens: list[str | int] = []
    for m in _TOKEN.finditer(path):
        key, idx = m.group(1), m.group(2)
        tokens.append(key if key is not None else int(idx))
    return tokens


def get(document: Any, path: str) -> Any:
    """读。路径不存在抛 `PathMissing`（**不返回 None**）。

    为什么不返回 None：`None` 是一个合法的字段值。用 None 表示"不存在"会让
    "字段是空的"与"字段根本没有"无法区分——而这两种情况的处置完全不同
    （前者可能是数据问题，后者一定是配置问题）。
    """
    node = document
    walked = ""
    for token in parse(path):
        walked += f"[{token}]" if isinstance(token, int) else (f".{token}" if walked else token)
        try:
            node = node[token]
        except (KeyError, IndexError, TypeError) as exc:
            raise PathMissing(f"路径不存在：{path}（在 {walked or '根'} 处断开）") from exc
    return node


def exists(document: Any, path: str) -> bool:
    try:
        get(document, path)
    except PathMissing:
        return False
    return True


def set_value(document: Any, path: str, value: Any) -> None:
    """**原地**改一个位置，其余部分一个字节都不动。

    原地修改（而不是"重建文档"）是刻意的：只要走的是"重建"，就一定会带上
    键顺序变化、缩进变化、注释丢失这类副作用，而这些副作用会让 diff 变成噪音，
    从而让人工审核失去意义。

    ⚠️ 遇到列表时**必须原地改内容，不能替换整个列表对象**。
    保序 YAML 库会把"行内写法"（`xTicks: ["a", "b"]`）这个风格记录在**容器对象**上，
    一旦把对象换成普通 list，风格就丢了，序列会被重新渲染成多行块：

        - xTicks: ["2026-03", "2026-04", …]      ← 原来 1 行
        + xTicks:                                 ← 变成 N 行
        + - 2026-03
        + - 2026-04
        + …

    diff 从 1 行膨胀到 N+1 行，而"内容其实只改了一层"。原地切片赋值能保住对象，
    因此也保住风格——而且这里不需要认识任何具体的 YAML 库。
    """
    tokens = parse(path)
    parent = document
    for token in tokens[:-1]:
        parent = parent[token]
    last = tokens[-1]

    current = parent[last] if isinstance(last, int) else parent.get(last, _MISSING)
    if current is _MISSING:
        raise PathMissing(f"路径不存在：{path}")

    # 列表 → 原地改内容（保住容器与它的风格）
    if isinstance(current, list) and isinstance(value, list):
        current[:] = value
        return
    # 映射同理
    if isinstance(current, dict) and isinstance(value, dict):
        current.clear()
        current.update(value)
        return

    if isinstance(last, int):
        parent[last] = value
    else:
        parent[last] = value


_MISSING = object()


def clone(document: Any) -> Any:
    return copy.deepcopy(document)


def describe_series_path(path: str, length: int) -> str:
    """给变更报告用的可读形式：`blocks[1].series[0].points` → `…points（共 6 点）`。"""
    return f"{path}（共 {length} 点）"
