"""CLI 测试辅助。

单独成文件而不是放进 conftest：它只是一个纯函数，没有夹具语义。
放在 tests/ 下是为了让 `pythonpath = ["."]` + pytest 的 rootdir 插入机制能直接 import。
"""

from __future__ import annotations

from beacon.cli import main


def run_cli(*argv: str) -> int:
    """执行 CLI 并返回退出码。

    直接调 `main()` 而不是 `subprocess`：这样能覆盖真实进程行为（同一份代码路径），
    又不需要为每个用例付一次解释器启动成本；输出仍会正常打到 pytest 捕获的流里。
    """
    return int(main(list(argv)))
