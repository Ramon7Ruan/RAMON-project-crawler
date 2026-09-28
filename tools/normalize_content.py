#!/usr/bin/env python3
"""内容库缩进规范化（一次性工具）。

为什么需要它
------------
`content/**/*.yaml` 用「顶层序列顶格 + 嵌套序列比父键多缩 2」的风格，
而 `ruamel.yaml` 的 `indent(mapping, sequence, offset)` 用**同一个 offset**
同时决定顶层与嵌套序列，因此无法复现这种混合风格（已穷举 7 组参数实测）。

后果是写回时整份重排，让 diff 失去审核价值。所以做一次规范化，
把三个文件统一到 ruamel 能逐字节复现的风格，此后长期稳定。

规范化的目标风格（ruamel 的 `indent(2, 2, 0)`）
----------------------------------------------
    - id: hotspot.data.pmi-reading      # 顶层序列顶格（不变）
      key_points:
      - 50 是荣枯线                      # 嵌套序列与父键对齐（← 唯一变化）
      blocks:
      - {type: prose, label: 定义}

三条安全约束
------------
1. **必须先备份**。备份落在 `--backup-dir`，失败即中止。
2. **必须逐文件校验语义**：规范化前后用两套解析器分别解析，深比较必须完全相等。
   不等就**不写这个文件**——缩进改动是排版，绝不允许夹带语义变化。
3. **默认只预演**。要真正落盘必须显式加 `--apply`。

用法
----
    python3 tools/normalize_content.py --content-dir ../content            # 预演
    python3 tools/normalize_content.py --content-dir ../content --apply    # 落盘
"""

from __future__ import annotations

import argparse
import io
import shutil
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml as pyyaml
    from ruamel.yaml import YAML
except ImportError as exc:  # pragma: no cover
    sys.exit(f"需要 pyyaml 与 ruamel.yaml：{exc}")

# 与 consumers/recall.py 里的设置**必须一致**。
# 不一致会导致"规范化之后仍然重排"——那时排查成本很高，因为两边看起来都对。
CANONICAL = {"mapping": 2, "sequence": 2, "offset": 0}


def make_ruamel():
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096
    y.indent(**CANONICAL)
    return y


def normalize_text(src: str) -> str:
    y = make_ruamel()
    doc = y.load(io.StringIO(src))
    buf = io.StringIO()
    y.dump(doc, buf)
    return buf.getvalue()


def semantic_equal(a: str, b: str) -> tuple[bool, str]:
    """用**PyYAML**（与 ruamel 无关的第三方解析器）比较语义。

    刻意不用写回时用的那个库来验证自己：同一个库的读写两边都会接受同一套误解。
    换一个解析器才能发现"只是把东西搬错了地方"这类错误。
    """
    try:
        left = pyyaml.safe_load(a)
        right = pyyaml.safe_load(b)
    except pyyaml.YAMLError as exc:
        return False, f"解析失败：{exc}"

    if left == right:
        return True, ""
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        for i, (x, z) in enumerate(zip(left, right)):
            if x != z:
                ida = x.get("id") if isinstance(x, dict) else "?"
                return False, f"第 {i + 1} 条记录（{ida}）语义不同"
    return False, "语义不同"


def diff_lines(before: str, after: str) -> tuple[int, int, list[tuple[int, str, str]]]:
    b, a = before.splitlines(), after.splitlines()
    changes: list[tuple[int, str, str]] = []
    for i in range(max(len(b), len(a))):
        x = b[i] if i < len(b) else "<缺行>"
        y = a[i] if i < len(a) else "<缺行>"
        if x != y:
            changes.append((i + 1, x, y))
    return len(b), len(a), changes


def oneline(s: str, width: int = 68) -> str:
    s = s.strip()
    return s if len(s) <= width else s[: width - 1] + "…"


def process(
    path: Path,
    *,
    apply: bool,
    backup_dir: Path | None,
    args_content_dir: str,
) -> dict:
    before = path.read_text(encoding="utf-8")

    # 已经规范化过？
    if normalize_text(before) == before:
        return {"path": path, "status": "already", "changed": 0, "lines": len(before.splitlines())}

    after = normalize_text(before)

    ok, why = semantic_equal(before, after)
    if not ok:
        return {"path": path, "status": "refused", "reason": why, "changed": 0}

    nb, na, changes = diff_lines(before, after)

    if apply:
        if backup_dir is None:
            return {"path": path, "status": "refused", "reason": "未指定备份目录", "changed": 0}
        # 备份**保留目录结构**。平铺是不够的：一旦两个区出现同名文件，
        # 后一份会静默覆盖前一份，而那时备份已经"看起来成功了"。
        rel = path.resolve().relative_to(Path(args_content_dir).resolve())
        dest = backup_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)

        # 写回后立刻复验：读取并与"期望内容"逐字节比较。
        # 不信任"写成功"这件事本身——缩进类改动出错时不会有任何异常。
        path.write_text(after, encoding="utf-8")
        written = path.read_text(encoding="utf-8")
        if written != after:
            return {"path": path, "status": "refused", "reason": "写回内容与预期不符", "changed": 0}

        again = normalize_text(written)
        if again != written:
            return {
                "path": path, "status": "refused",
                "reason": "规范化后仍不稳定（再次规范化还会变）", "changed": 0,
            }

    return {
        "path": path,
        "status": "normalized" if apply else "would-normalize",
        "changed": len(changes),
        "lines": nb,
        "after_lines": na,
        "sample": changes[:6],
        "remaining": max(0, len(changes) - 6),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="内容库缩进规范化（默认只预演）")
    ap.add_argument("--content-dir", required=True, help="内容库目录")
    ap.add_argument("--apply", action="store_true", help="真正落盘（默认只预演）")
    ap.add_argument("--backup-dir", default=None, help="备份目录；--apply 时必需")
    args = ap.parse_args()

    root = Path(args.content_dir).resolve()
    if not root.exists():
        print(f"内容目录不存在：{root}", file=sys.stderr)
        return 2

    files = sorted(root.rglob("*.yaml"))
    if not files:
        print("没找到任何 yaml 文件", file=sys.stderr)
        return 2

    backup = Path(args.backup_dir).resolve() if args.backup_dir else None
    if args.apply and backup is None:
        backup = root.parent / ".workbuddy" / "backups" / f"content-{datetime.now():%Y%m%d-%H%M%S}"
        print(f"未指定 --backup-dir，自动使用：{backup}\n")

    print(f"模式：{'落盘' if args.apply else '预演（不改文件）'}")
    print(f"目录：{root}")
    print(f"文件：{len(files)} 个\n")

    results = [
        process(p, apply=args.apply, backup_dir=backup, args_content_dir=args.content_dir)
        for p in files
    ]

    total_changed = 0
    for r in results:
        name = r["path"].relative_to(root)
        if r["status"] == "already":
            print(f"  [已是规范形式] {name}")
            continue
        if r["status"] == "refused":
            print(f"  [拒绝]        {name} —— {r['reason']}")
            continue
        total_changed += r["changed"]
        mark = "已规范化" if r["status"] == "normalized" else "待规范化"
        print(f"  [{mark}] {name}：{r['changed']} / {r['lines']} 行受影响")

    print(f"\n合计受影响行：{total_changed}")

    sample_shown = False
    for r in results:
        if r.get("sample"):
            name = r["path"].relative_to(root)
            print(f"\n样例差异（{name}，前 6 处）：")
            for lineno, b, a in r["sample"]:
                print(f"  第 {lineno} 行")
                print(f"    改前 |{b}")
                print(f"    改后 |{a}")
            if r.get("remaining"):
                print(f"  （另有 {r['remaining']} 处，形态相同：仅缩进变化）")
            sample_shown = True
            break
    if not sample_shown and total_changed == 0:
        print("\n所有文件都已是规范形式，无需改动。")

    if not args.apply and total_changed:
        print("\n这是预演。要落盘请加 --apply（会自动先备份）。")
    if args.apply:
        print(f"\n备份位置：{backup}")
        print("落盘后已逐文件复验：语义不变、且再次规范化不会再变。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
