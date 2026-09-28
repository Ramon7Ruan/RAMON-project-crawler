"""源注册表：把"配置里的源名"变成"可用的适配器实例"。

存在这一层的意义
----------------
让 CLI 与上层拿到的是**一组同构的适配器**，而不是一堆 if/else。
新增一个源只改两处：加一个模块、在 config/sources.yaml 登记。
`core/` 与既有 `sources/` 都不用动（PRD §6.5 的可更新性承诺）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..core.fetch import Fetcher
from .base import ConfigError, SourceAdapter
from .csindex import CsindexAdapter
from .eastmoney import EastmoneyAdapter
from .fred import FredAdapter

ADAPTERS: dict[str, type[SourceAdapter]] = {
    "fred": FredAdapter,
    "eastmoney": EastmoneyAdapter,
    "csindex": CsindexAdapter,
}
"""源名 → 适配器类。这是唯一需要登记新源的地方。"""

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "sources.yaml"


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """读取源登记表。路径可由调用方指定（I5：不硬编码父项目相对路径）。"""
    config_path = Path(path) if path else DEFAULT_CONFIG
    if not config_path.exists():
        raise ConfigError(f"源配置文件不存在：{config_path}")
    with config_path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data.get("sources"), dict):
        raise ConfigError(f"配置缺少 sources 段：{config_path}")
    return data


def build_fetcher(
    config: dict[str, Any],
    cache_dir: Path | str,
    *,
    transport_name: str = "httpx",
    proxy: str | None = None,
    trust_env: bool = True,
    **overrides: Any,
) -> Fetcher:
    """按配置里的 defaults 段构造 Fetcher。

    传输后端由调用方选择（默认 httpx）；`curl` 只在某些站点对 Python 客户端
    不友好时才需要，见 core/curl_transport.py。
    """
    defaults = dict(config.get("defaults") or {})
    defaults.update({k: v for k, v in overrides.items() if v is not None})
    return Fetcher(
        cache_dir=Path(cache_dir),
        transport_name=transport_name,
        proxy=proxy,
        trust_env=trust_env,
        timeout=float(defaults.get("timeout_seconds", 30)),
        min_interval=float(defaults.get("min_interval_seconds", 1.0)),
        max_retries=int(defaults.get("max_retries", 3)),
        cache_ttl_hours=float(defaults.get("cache_ttl_hours", 12)),
    )


def build_adapters(
    config: dict[str, Any],
    fetcher: Fetcher,
    names: list[str] | None = None,
) -> list[SourceAdapter]:
    """按配置实例化适配器。只实例化登记过的源（未登记 = 不启用）。"""
    sections = config["sources"]
    wanted = names or list(sections)

    adapters: list[SourceAdapter] = []
    for name in wanted:
        if name not in ADAPTERS:
            raise ConfigError(f"未知的源：{name}（已实现：{', '.join(ADAPTERS)}）")
        if name not in sections:
            raise ConfigError(f"源 {name} 有实现但未在 config/sources.yaml 登记——未登记不得启用")
        adapters.append(ADAPTERS[name](sections[name], fetcher))
    return adapters
