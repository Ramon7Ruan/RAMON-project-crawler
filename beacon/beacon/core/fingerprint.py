"""文档指纹。

它存在的唯一理由是回答一个问题：**我审核的那份内容，和现在磁盘上的是同一份吗？**

从生成候选到人工确认之间可能隔了几小时，这期间内容库可能被人手工改过。
此时若无条件写回，就会**静默覆盖掉那次手工修改**——这是最难发现的一类数据丢失：
写回"成功"了，diff 也"对"，只是丢掉了一个不属于本次流程的改动。

所以：生成候选时记下目标文档的指纹，写回前再算一次，不一致就**拒绝写回**并说明原因。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any


def _default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (set, tuple)):
        return sorted(value)
    raise TypeError(f"文档里出现了无法序列化的类型：{type(value).__name__}")


def fingerprint(document: Any) -> str:
    """对文档的**语义**取指纹。

    刻意走 JSON 规范化而不是"文件字节的哈希"：

    * 字节哈希会被缩进、行尾空白、注释这类排版差异影响，
      于是"只是重新排版了一下"会被误报成"内容被改过"，导致写回被无谓地拒绝。
    * 语义指纹只关心值。这正是我们想防的那种改动（被人改了一个数）能被抓住、
      而无关改动不会误报的分界线。

    排序键固定、`ensure_ascii=False`、无多余空白 —— 同一份内容在任何机器上
    必须得到同一个指纹，否则这个机制在多机场景下会失效。
    """
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_default,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
