"""独立性与复用测试（IND-4 / IND-6 / IND-7）。

这组测试守护的是**这个工具能不能被别的 app 用**，而不是它能不能跑。
它们全都是"结构性"断言——不测功能，测的是"有没有把不该有的东西写进来"。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent / "beacon"
CORE = PKG / "core"

RECALL_VOCAB = (
    "recipe",
    "key_points",
    "one_liner",
    "blocks",
    "concept",
    "domain",
    "track",
    "hotspot",
)
"""某个 app（Recall）的专有词汇。core 层出现任何一个，工具就绑死了。"""

FORBIDDEN_PARENT_REFS = (
    "acknowledge",
    "content-dist",
    "node_modules",
    "package.json",
    "dist-electron",
)
"""父项目的标志物。出现即说明"不硬编码父项目相对路径"（I5）被破坏了。"""


def py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if p.name != "__pycache__")


def strip_comments_and_docstrings(src: str) -> str:
    """去掉注释与 docstring 后再扫描。

    理由：解释"为什么不能用某个词"的注释本身会包含那个词。
    扫描代码而非注释，才能既保留说明、又不误伤。
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    body[0].value.value = ""
    text = ast.unparse(tree)
    return re.sub(r"#.*", "", text)


class TestCoreHasNoAppBinding:
    """IND-4 / A-C11：core 层零绑定。"""

    def test_core_files_exist(self) -> None:
        assert py_files(CORE), "没找到 core 源码，扫描会静默通过——这本身是个错误"

    @pytest.mark.parametrize("word", RECALL_VOCAB)
    def test_core_does_not_mention_word(self, word: str) -> None:
        offenders = []
        for path in py_files(CORE):
            body = strip_comments_and_docstrings(path.read_text(encoding="utf-8"))
            if re.search(rf"\b{word}\b", body):
                offenders.append(path.name)
        assert not offenders, f"core 层出现了特定 app 的词汇 {word!r}：{offenders}"

    def test_scan_would_catch_a_violation(self) -> None:
        """自检：证明扫描不是假通过。故意造一段违规代码，扫描必须命中。"""
        fake = 'def f(recipe):\n    return recipe\n'
        stripped = strip_comments_and_docstrings(fake)
        assert re.search(r"\brecipe\b", stripped), "扫描器失灵了——那前面的通过都不算数"


class TestNoParentProjectCoupling:
    """IND-6：路径全部参数化，不引用父项目。"""

    @pytest.mark.parametrize("needle", FORBIDDEN_PARENT_REFS)
    def test_no_parent_marker_in_source(self, needle: str) -> None:
        offenders = []
        for path in py_files(PKG):
            src = path.read_text(encoding="utf-8")
            for line in src.splitlines():
                if needle in line and not line.strip().startswith("#"):
                    offenders.append(f"{path.name}: {line.strip()[:70]}")
        assert not offenders, f"源码里出现了父项目标志物 {needle!r}：{offenders}"

    def test_no_relative_path_escaping_package(self) -> None:
        offenders = []
        for path in py_files(PKG):
            for line in path.read_text(encoding="utf-8").splitlines():
                if re.search(r'Path\(\s*["\']\.\./', line):
                    offenders.append(f"{path.name}: {line.strip()[:70]}")
        assert not offenders, f"出现了向包外逃逸的相对路径：{offenders}"

    def test_cli_requires_explicit_paths(self) -> None:
        """CLI 的路径参数必须存在，且不能有写死的默认绝对路径。"""
        cli = (PKG / "cli.py").read_text(encoding="utf-8")
        assert "--cache-dir" in cli
        assert "--out" in cli
        assert "--config" in cli
        assert "/Users/" not in cli, "CLI 里出现了写死的绝对路径"


class TestNoNodeDependency:
    """IND-7：不依赖 Node 工具链——这是"能被别的 app 用"的前提之一。"""

    def test_no_node_tooling_references(self) -> None:
        offenders = []
        for path in py_files(PKG):
            body = strip_comments_and_docstrings(path.read_text(encoding="utf-8"))
            for token in ("npm", "node_modules", "npx", "electron"):
                if re.search(rf"\b{token}\b", body, re.IGNORECASE):
                    offenders.append(f"{path.name}: {token}")
        assert not offenders, f"源码引用了 Node 工具链：{offenders}"

    # 唯一被允许调用外部进程的模块：可选的 curl 后端。
    # 隔离在一个文件里，是为了让"默认路径是否依赖外部命令"这件事可以被机械检查。
    SUBPROCESS_ALLOWLIST = {"curl_transport.py"}

    def test_subprocess_only_in_allowlisted_module(self) -> None:
        """除 curl 后端外，任何模块都不得 shell out。"""
        offenders = []
        for path in py_files(PKG):
            if path.name in self.SUBPROCESS_ALLOWLIST:
                continue
            if re.search(r"^\s*import subprocess", path.read_text(encoding="utf-8"), re.M):
                offenders.append(path.name)
        assert not offenders, f"这些模块不该调用外部进程：{offenders}"

    def test_default_transport_is_pure_python(self) -> None:
        """默认后端必须是 httpx —— 用外部命令当默认会让工具在别的机器上悄悄失效。"""
        from beacon.core.fetch import make_transport

        assert type(make_transport("httpx")).__name__ == "HttpxTransport"
        assert type(make_transport()).__name__ == "HttpxTransport"

    def test_unknown_transport_is_rejected(self) -> None:
        from beacon.core.fetch import make_transport

        with pytest.raises(ValueError, match="未知的传输后端"):
            make_transport("wget")

    def test_only_third_party_deps_are_declared(self) -> None:
        """依赖必须写在 pyproject 里，且不含任何父项目相关的东西。"""
        pyproject = (PKG.parent / "pyproject.toml").read_text(encoding="utf-8")
        assert "httpx" in pyproject
        assert "pydantic" in pyproject
        for token in FORBIDDEN_PARENT_REFS:
            assert token not in pyproject, f"pyproject 里出现了 {token!r}"


class TestConfigIsDataNotCode:
    """可更新性（PRD §6.5）：抓取参数在配置里，改动不需要碰代码。"""

    def test_sources_config_declares_all_adapters(self) -> None:
        from beacon.sources.registry import ADAPTERS, load_config

        config = load_config()
        registered = set(config["sources"])
        assert set(ADAPTERS) == registered, (
            f"有实现但未登记 / 已登记但无实现：{set(ADAPTERS) ^ registered}"
        )

    def test_every_source_has_tier_and_upstream(self) -> None:
        from beacon.sources.registry import load_config

        for name, section in load_config()["sources"].items():
            assert section.get("tier") in ("L1", "L2", "L3"), f"{name} 缺少 tier"
            assert section.get("base_url"), f"{name} 缺少 base_url"
            if section["tier"] == "L2":
                assert section.get("upstream"), f"L2 源 {name} 必须标注上游一手机构"

    def test_thresholds_are_not_hardcoded_in_code(self) -> None:
        """超时/限速/重试次数必须来自配置，不在代码里写死。"""
        fetch_src = (CORE / "fetch.py").read_text(encoding="utf-8")
        # 允许作为**函数默认值**存在（便于单测直接构造），但必须能被构造参数覆盖
        assert "timeout: float = " in fetch_src
        assert "min_interval: float = " in fetch_src
        assert "max_retries: int = " in fetch_src
        registry_src = (PKG / "sources" / "registry.py").read_text(encoding="utf-8")
        assert "defaults" in registry_src, "Fetcher 必须从配置的 defaults 段取值"
