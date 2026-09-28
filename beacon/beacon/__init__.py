"""Beacon —— 内容采集工具。

独立工具：不依赖父项目的任何代码或工具链，所有输入输出路径由调用方传入。
"""

__version__ = "0.1.0"

CONTRACT_VERSION = "1.0"
"""数据契约版本。破坏性变更（删字段、改语义）必须递增，并更新 docs/数据契约.md。"""

CONSUMER_NAMES = ("recall",)
"""已实现的消费者。新增 app 只需在 beacon/consumers/ 下加一个模块，core 与 sources 不动。"""
