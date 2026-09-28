"""CLI 契约测试（IND-3）+ 两条硬门禁在**命令行层面**的行为。

为什么要在 CLI 层再测一遍已经测过的逻辑：

* 硬门禁「失败不产假数据」的判定标准是**"没有写出文件、退出码非 0"**，
  这是调用方（人 / 未来的调度器）能看到的东西。适配器层抛没抛异常并不等价于
  "产物目录是干净的"——中间任何一步写文件都可能破坏这条门禁。
* 失败隔离的可见形式是 `feed.meta.json` 里的 `failures` 数组。
  没有它，下游只会看到"少了两个指标"，而不知道少的原因。

⚠️ 每条用例都必须传 `--cache-dir`（指向 tmp_path）。
曾经漏传过，后果很隐蔽：CLI 默认缓存目录是 `./.beacon-cache`，
只要开发机上跑过一次真实抓取，测试就会**命中真实缓存拿到真实数据**，
于是"断网应当失败"的用例反而全部通过——测试变成了自我验证。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from beacon.core.fetch import FetchError, Fetcher
from beacon.sources.registry import ADAPTERS
from cli_helpers import run_cli
from conftest import load_fixture

HOSTS = {
    "fred": "https://fred.stlouisfed.org",
    "eastmoney": "https://datacenter-web.eastmoney.com",
    "csindex": "https://www.csindex.com.cn",
}


@dataclass
class CliEnv:
    config_path: Path
    out_dir: Path
    cache_dir: Path

    def run(self, command: str, *extra: str) -> int:
        return run_cli(
            command,
            "--config",
            str(self.config_path),
            "--cache-dir",
            str(self.cache_dir),
            *extra,
        )

    def export(self) -> int:
        return self.run("export", "--out", str(self.out_dir))


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch, sources_config):
    """把 CLI 的 Fetcher 换成注入桩的实例，其余流程（配置、适配器、写文件）保持真实。"""

    def _make(stub) -> CliEnv:
        config_path = tmp_path / "sources.yaml"
        config_path.write_text(
            yaml.safe_dump(sources_config, allow_unicode=True), encoding="utf-8"
        )

        def fake_build_fetcher(config, cache_dir, **kwargs):
            return Fetcher(
                cache_dir=Path(cache_dir),
                transport=stub,
                timeout=5.0,
                min_interval=0.0,
                max_retries=1,
                cache_ttl_hours=12.0,
                sleeper=lambda _: None,
            )

        monkeypatch.setattr("beacon.cli.build_fetcher", fake_build_fetcher)
        return CliEnv(config_path, tmp_path / "out", tmp_path / "cache")

    return _make


def route_all(stub, entries: dict[str, object]) -> None:
    for name, value in entries.items():
        stub.routes[HOSTS[name]] = value


# --------------------------------------------------------------------------- #
# 硬门禁：全源失败 → 不写任何产物 + 非 0 退出码
# --------------------------------------------------------------------------- #


class TestExportHardGate:
    def test_all_sources_down_writes_nothing(self, cli_env, stub) -> None:
        route_all(stub, {n: FetchError(HOSTS[n], "连接超时") for n in HOSTS})
        env = cli_env(stub)

        assert env.export() == 1, "全源失败必须返回非 0"
        assert not (env.out_dir / "feed.jsonl").exists(), "绝不能写出空的 feed"
        assert not (env.out_dir / "feed.meta.json").exists()

    def test_empty_but_http_200_also_counts_as_failure(self, cli_env, stub) -> None:
        """`200 + data: []` 是"伪成功"，同样不许产出文件。"""
        route_all(
            stub,
            {
                "fred": (200, "observation_date,DGS10\n"),
                "eastmoney": (
                    200,
                    json.dumps({"success": True, "result": {"data": []}, "code": 0}),
                ),
                "csindex": (200, json.dumps({"code": "200", "data": []})),
            },
        )
        env = cli_env(stub)

        assert env.export() == 1
        assert not (env.out_dir / "feed.jsonl").exists()

    def test_partial_failure_still_exports_and_records_the_failure(self, cli_env, stub) -> None:
        """单源失败不影响其他源，但必须留痕在 meta 里。"""
        route_all(
            stub,
            {
                "fred": FetchError(HOSTS["fred"], "连接超时"),
                "eastmoney": (200, load_fixture("eastmoney_pmi.json")),
                "csindex": (200, load_fixture("csindex_perf.json")),
            },
        )
        env = cli_env(stub)

        assert env.export() == 0
        meta = json.loads((env.out_dir / "feed.meta.json").read_text(encoding="utf-8"))
        assert sorted(meta["sources"]) == ["csindex", "eastmoney"]
        assert [f["source"] for f in meta["failures"]] == ["fred"], "失败必须留痕"
        assert meta["counts"]["observations"] > 0

    def test_failure_reason_carries_the_http_status(self, cli_env, stub) -> None:
        """错误串里必须带状态码——"403 还是 502"决定了下一步怎么查。"""
        route_all(
            stub,
            {
                "fred": FetchError(HOSTS["fred"], "连接超时"),
                "eastmoney": (200, load_fixture("eastmoney_pmi.json")),
                "csindex": (503, "service unavailable"),
            },
        )
        env = cli_env(stub)

        assert env.export() == 0
        meta = json.loads((env.out_dir / "feed.meta.json").read_text(encoding="utf-8"))
        reason = next(f["reason"] for f in meta["failures"] if f["source"] == "csindex")
        assert "503" in reason


class TestHealth:
    def test_all_down_returns_nonzero(self, cli_env, stub, capsys) -> None:
        route_all(stub, {n: FetchError(HOSTS[n], "连接超时") for n in HOSTS})
        env = cli_env(stub)

        assert env.run("health") == 1
        assert "不可用" in capsys.readouterr().out

    def test_all_ok_returns_zero(self, cli_env, stub) -> None:
        route_all(
            stub,
            {
                "fred": (200, load_fixture("fred_dgs10.csv")),
                "eastmoney": (200, load_fixture("eastmoney_pmi.json")),
                "csindex": (200, load_fixture("csindex_perf.json")),
            },
        )
        env = cli_env(stub)

        assert env.run("health") == 0


class TestConfigGuard:
    def test_unregistered_source_in_config_is_reported(self, tmp_path: Path, capsys) -> None:
        """配置挂了要给人看得懂的错，而不是 traceback。"""
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            yaml.safe_dump({"sources": {"nosuchsource": {"tier": "L1"}}}, allow_unicode=True),
            encoding="utf-8",
        )
        code = run_cli(
            "export",
            "--config",
            str(bad),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--out",
            str(tmp_path / "o"),
        )

        assert code == 2, "配置错误必须有独立的退出码"
        assert "配置错误" in capsys.readouterr().err

    def test_adapters_registry_matches_the_shipped_config(self) -> None:
        """每条已实现的适配器都必须在真实配置里登记，且反过来也成立。"""
        from beacon.sources.registry import load_config

        shipped = load_config(None)
        assert set(shipped["sources"]) == set(ADAPTERS), "配置与适配器实现必须一一对应"

        for name, spec in shipped["sources"].items():
            if spec.get("tier") == "L2":
                assert spec.get("upstream"), f"{name} 是 L2，必须声明 upstream"


# --------------------------------------------------------------------------- #
# 中性产物（IND-2 / A-C16）
# --------------------------------------------------------------------------- #

RECALL_VOCAB = (
    "recipe", "key_points", "one_liner", "blocks", "concept",
    "domain", "track", "hotspot", "concept_id", "due_at",
)
"""另一个 app 的专有词汇。Feed 里出现任何一个，它就不再是"中性产物"了。"""


class TestFeedIsAppAgnostic:
    """`feed.jsonl` 是给**任何** app 读的，因此不得带任何 app 的语义。"""

    def _export(self, cli_env, stub) -> Path:
        route_all(
            stub,
            {
                "eastmoney": (200, load_fixture("eastmoney_pmi.json")),
                "csindex": (200, load_fixture("csindex_perf.json")),
                "fred": FetchError(HOSTS["fred"], "连接超时"),
            },
        )
        env = cli_env(stub)
        assert env.export() == 0
        return env.out_dir / "feed.jsonl"

    def test_keys_are_exactly_the_contract(self, cli_env, stub) -> None:
        rows = [
            json.loads(line)
            for line in self._export(cli_env, stub).read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert rows
        expected = {"indicator", "period", "value", "unit", "caliber", "source", "provenance"}
        for row in rows[:5]:
            assert set(row) == expected, f"字段集与契约不符：{sorted(set(row) ^ expected)}"

    def test_no_other_app_vocabulary_appears(self, cli_env, stub) -> None:
        text = self._export(cli_env, stub).read_text(encoding="utf-8")
        hits = [w for w in RECALL_VOCAB if w in text]
        assert not hits, f"Feed 里出现了某个 app 的专有词汇：{hits}"

    def test_meta_records_contract_version_and_failures(self, cli_env, stub) -> None:
        """调用方要能一眼看出"这批数据是按哪个契约产出的"以及"哪些源没取到"。"""
        feed = self._export(cli_env, stub)
        meta = json.loads((feed.parent / "feed.meta.json").read_text(encoding="utf-8"))

        assert meta["contractVersion"]
        assert [f["source"] for f in meta["failures"]] == ["fred"]
        assert meta["counts"]["observations"] > 0


# --------------------------------------------------------------------------- #
# 中性产物（IND-2 / A-C16）
# --------------------------------------------------------------------------- #

RECALL_VOCAB = (
    "recipe", "key_points", "one_liner", "blocks", "concept",
    "domain", "track", "hotspot", "concept_id", "due_at",
)
"""另一个 app 的专有词汇。Feed 里出现任何一个，它就不再是"中性产物"了。"""


class TestFeedIsAppAgnostic:
    """`feed.jsonl` 是给**任何** app 读的，因此不得带任何 app 的语义。"""

    def _export(self, cli_env, stub) -> Path:
        route_all(
            stub,
            {
                "eastmoney": (200, load_fixture("eastmoney_pmi.json")),
                "csindex": (200, load_fixture("csindex_perf.json")),
                "fred": FetchError(HOSTS["fred"], "连接超时"),
            },
        )
        env = cli_env(stub)
        assert env.export() == 0
        return env.out_dir / "feed.jsonl"

    def test_keys_are_exactly_the_contract(self, cli_env, stub) -> None:
        rows = [
            json.loads(line)
            for line in self._export(cli_env, stub).read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert rows
        expected = {"indicator", "period", "value", "unit", "caliber", "source", "provenance"}
        for row in rows[:5]:
            assert set(row) == expected, f"字段集与契约不符：{sorted(set(row) ^ expected)}"

    def test_no_other_app_vocabulary_appears(self, cli_env, stub) -> None:
        text = self._export(cli_env, stub).read_text(encoding="utf-8")
        hits = [w for w in RECALL_VOCAB if w in text]
        assert not hits, f"Feed 里出现了某个 app 的专有词汇：{hits}"

    def test_meta_records_contract_version_and_failures(self, cli_env, stub) -> None:
        """调用方要能一眼看出"这批数据是按哪个契约产出的"以及"哪些源没取到"。"""
        feed = self._export(cli_env, stub)
        meta = json.loads((feed.parent / "feed.meta.json").read_text(encoding="utf-8"))

        assert meta["contractVersion"]
        assert [f["source"] for f in meta["failures"]] == ["fred"]
        assert meta["counts"]["observations"] > 0
