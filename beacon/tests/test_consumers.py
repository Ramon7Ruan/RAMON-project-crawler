"""内容库读写测试（Recall 侧适配）。

其中 `test_read_write_roundtrip_changes_nothing` 是本阶段最重要的一条：
`content/*.yaml` 是人工维护的文件（含缩进、块顺序、行内 flow-map 写法、注释）。
用普通 YAML 库读写会重排格式、丢注释——把 diff 变成噪音，
从而让"人工审核"这件事失去意义。

所以那条测试的断言是**逐字节相同**，不是"解析出来相同"。
前者是审核能不能用的分界线，后者只是"程序没崩"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beacon.consumers.recall import RecallContentStore
from beacon.core.store import TargetError

CONTENT_A = """\
# 世界热点 20 个概念（事件解剖 12 / 数据观察 8）
# 差异化约束：本区每个概念必须包含 timeline，且 source 必填

- id: hotspot.data.pmi-reading
  domain: hotspot
  track: data
  title: PMI 怎么读
  one_liner: PMI 是 49.8，到底是好还是坏？
  key_points:
  - 50 是荣枯线，但方向比水平更重要
  source: 国家统计局 PMI 月度发布，截至 2026-09-01
  updated_at: 2026-09-01
  recipe: [prose, dataviz, timeline]
  blocks:
  - {type: prose, label: 定义, body: "PMI 是**环比扩散指数**，50 是荣枯线。"}
  - type: dataviz
    label: 制造业与非制造业
    chart: line
    xTicks: ["2026-07", "2026-08"]
    series:
    - {name: 制造业 PMI, points: [49.2, 49.8]}
    - {name: 非制造业 PMI, points: [49.0, 49.0]}
    source: 国家统计局，截至 2026-08-31
  links:
  - {to: econ.macro.gdp-three-ways, reason: 基础概念}

- id: hotspot.data.valuation-percentile
  domain: hotspot
  track: data
  title: 估值分位
  one_liner: PE 处于历史 90% 分位，就一定贵吗？
  source: 主要指数估值统计，截至 2026-09-19
  updated_at: 2026-09-19
  recipe: [prose, formula, dataviz]
  blocks:
  - {type: prose, label: 定义, body: "分位把当前估值放进历史区间里排序。"}
  - type: dataviz
    label: 不同区间下的分位
    chart: bar
    xTicks: ["近 3 年", "近 5 年", "近 10 年", "全历史"]
    series:
    - {name: 分位, points: [88, 72, 54, 46]}
    source: 示意数据，仅用于说明区间选择的影响
"""

CONTENT_B = """\
# 另一个区
- id: econ.macro.gdp-three-ways
  domain: econ
  track: macro
  title: GDP 的三种算法
  source: 国家统计局，截至 2026-09-01
  updated_at: 2026-09-01
  recipe: [prose, curve]
  blocks:
    - {type: prose, label: 定义, body: "生产法、收入法、支出法。"}
"""


@pytest.fixture
def content_dir(tmp_path: Path) -> Path:
    d = tmp_path / "content"
    (d / "hotspot").mkdir(parents=True)
    (d / "econ").mkdir(parents=True)
    (d / "hotspot" / "hotspot.yaml").write_text(CONTENT_A, encoding="utf-8")
    (d / "econ" / "econ.yaml").write_text(CONTENT_B, encoding="utf-8")
    return d


class TestScanning:
    def test_finds_every_record(self, content_dir: Path) -> None:
        store = RecallContentStore(content_dir)
        assert store.targets() == [
            "econ.macro.gdp-three-ways",
            "hotspot.data.pmi-reading",
            "hotspot.data.valuation-percentile",
        ]

    def test_missing_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(TargetError, match="内容目录不存在"):
            RecallContentStore(tmp_path / "nope")

    def test_duplicate_id_raises(self, tmp_path: Path) -> None:
        d = tmp_path / "c"
        d.mkdir()
        body = "- id: dup\n  title: a\n"
        (d / "a.yaml").write_text(body, encoding="utf-8")
        (d / "b.yaml").write_text(body, encoding="utf-8")
        with pytest.raises(TargetError, match="id 重复"):
            RecallContentStore(d)

    def test_non_list_top_level_raises(self, tmp_path: Path) -> None:
        d = tmp_path / "c"
        d.mkdir()
        (d / "a.yaml").write_text("id: notalist\ntitle: x\n", encoding="utf-8")
        with pytest.raises(TargetError, match="顶层应为记录列表"):
            RecallContentStore(d)

    def test_unknown_target_read_raises(self, content_dir: Path) -> None:
        store = RecallContentStore(content_dir)
        with pytest.raises(TargetError, match="没有这个概念"):
            store.read("no.such.target")


class TestIsolation:
    def test_read_returns_a_copy(self, content_dir: Path) -> None:
        """读出来的是副本——否则一条未被采纳的红级建议会污染真实内容。"""
        store = RecallContentStore(content_dir)
        doc = store.read("hotspot.data.pmi-reading")
        doc["title"] = "被改坏了"
        doc["blocks"][1]["series"][0]["points"][0] = 999

        again = store.read("hotspot.data.pmi-reading")
        assert again["title"] == "PMI 怎么读"
        assert again["blocks"][1]["series"][0]["points"][0] == 49.2

    def test_write_touches_only_the_target_record(self, content_dir: Path) -> None:
        store = RecallContentStore(content_dir)
        other_before = store.source_text("econ.macro.gdp-three-ways")

        doc = store.read("hotspot.data.pmi-reading")
        doc["updated_at"] = "2026-08-31"
        store.write("hotspot.data.pmi-reading", doc)

        # 另一个文件必须逐字节不变
        assert store.source_text("econ.macro.gdp-three-ways") == other_before

        re_read = RecallContentStore(content_dir)
        assert str(re_read.read("hotspot.data.pmi-reading")["updated_at"]) == "2026-08-31"
        # 同文件里的**另一条记录**语义上不能被碰到
        # （它的文本可能因整份重排而变化——见 TestRoundTripFidelity 里的说明）
        # 注意用 str() 包一层：ruamel 的 round-trip 解析器会把裸 ISO 日期读成 date 对象，
        # 而"写进去的字符串"仍是字符串。两者表示同一个值，比较时必须归一化。
        assert str(re_read.read("hotspot.data.valuation-percentile")["updated_at"]) == "2026-09-19"


class TestRoundTripFidelity:
    """往返保真：写回后**未改动的部分不得变一个字节**。

    这是本工具能不能用的分界线。一个会让 diff 变成噪音的写回比不写回更危险——
    它会让人以为自己在审核，其实没有。

    曾经不成立，原因与解决
    ----------------------
    内容库原先用「顶层序列顶格 + 嵌套序列比父键多缩 2 格」的混合风格，
    而 ruamel 的 `indent(mapping, sequence, offset)` 用**同一个 offset**
    同时决定顶层与嵌套序列的短横线位置，无法表达这种组合（穷举 7 组参数实测）。

    2026-09-23 做了一次性规范化（`tools/normalize_content.py`，经 Ramon 确认）：
    三个文件统一到 ruamel 能逐字节复现的形式（`indent(2, 2, 0)`），
    完成后用独立解析器逐文件确认语义零变化，并通过了内容门禁（0 error）。
    此后往返就是逐字节稳定的。

    ⚠️ 因此这两处的缩进设置**必须保持一致**：
    `consumers/recall.py` 的 `self._yaml.indent(...)` 与
    `tools/normalize_content.py` 的 `CANONICAL`。
    不一致的症状是"规范化之后仍然重排"，而两边看起来都对，排查成本很高。
    """

    def test_semantics_and_comments_survive(self, content_dir: Path) -> None:
        target = "hotspot.data.pmi-reading"
        store = RecallContentStore(content_dir)
        before_doc = store.read(target)

        store.write(target, store.read(target))

        reloaded = RecallContentStore(content_dir)
        assert reloaded.read(target) == before_doc, "语义必须完全不变"

        text = reloaded.source_text(target)
        assert "# 世界热点 20 个概念" in text, "文件头注释必须保留"
        assert "# 差异化约束" in text
        assert '{type: prose, label: 定义, body: "PMI 是**环比扩散指数**，50 是荣枯线。"}' in text, (
            "行内 flow-map 的写法必须原样保留"
        )

    def test_all_records_survive_a_roundtrip(self, content_dir: Path) -> None:
        store = RecallContentStore(content_dir)
        before = {t: store.read(t) for t in store.targets()}
        for t in store.targets():
            store.write(t, store.read(t))
        after = {t: RecallContentStore(content_dir).read(t) for t in store.targets()}
        assert after == before

    def test_only_the_written_file_is_touched(self, content_dir: Path) -> None:
        store = RecallContentStore(content_dir)
        other_before = store.source_text("econ.macro.gdp-three-ways")
        store.write("hotspot.data.pmi-reading", store.read("hotspot.data.pmi-reading"))
        assert store.source_text("econ.macro.gdp-three-ways") == other_before, (
            "未被写入的**文件**必须逐字节不变"
        )

    def test_roundtrip_is_byte_stable(self, content_dir: Path) -> None:
        """硬门禁：写回后**未改动的部分不得变一个字节**。"""
        target = "hotspot.data.pmi-reading"
        store = RecallContentStore(content_dir)
        before = store.source_text(target)
        store.write(target, store.read(target))
        after = store.source_text(target)
        assert after == before, f"差异片段：{_first_diff(before, after)}"

    def test_roundtrip_is_byte_stable_after_a_real_change(self, content_dir: Path) -> None:
        """改一个字段时，**只有那一行**能变。"""
        target = "hotspot.data.pmi-reading"
        store = RecallContentStore(content_dir)
        before = store.source_text(target).splitlines()

        doc = store.read(target)
        doc["blocks"][1]["series"][0]["points"] = [49.1, 49.9]
        store.write(target, doc)

        after = store.source_text(target).splitlines()
        assert len(after) == len(before), "行数不应变化"
        diff = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        assert len(diff) == 1, (
            f"应当只有 1 行变化，实际 {len(diff)} 行：{[after[i] for i in diff][:4]}"
        )
        assert "49.9" in after[diff[0]]


def _first_diff(a: str, b: str) -> str:
    for i, (x, y) in enumerate(zip(a.splitlines(), b.splitlines())):
        if x != y:
            return f"第 {i + 1} 行\n  改前：{x!r}\n  改后：{y!r}"
    if len(a.splitlines()) != len(b.splitlines()):
        return f"行数不同：{len(a.splitlines())} → {len(b.splitlines())}"
    return "（逐行相同，差异可能在行尾空白或换行符）"


# --------------------------------------------------------------------------- #
# 真实内容库的往返（只读，不写盘）
# --------------------------------------------------------------------------- #


def _real_content_dir() -> Path | None:
    """定位真实内容库。

    优先读环境变量，否则按目录层级往上推。**刻意不写死绝对路径**——
    那会让这个包绑死在某个具体位置（IND-6：路径全部参数化）。
    """
    import os

    env = os.environ.get("RECALL_CONTENT_DIR")
    if env:
        p = Path(env)
        return p if p.exists() else None
    # tests/ → beacon/ → 爬虫/ → 项目根 → content/
    guess = Path(__file__).resolve().parents[3] / "content"
    return guess if guess.exists() else None


@pytest.mark.skipif(_real_content_dir() is None, reason="本地没有真实内容库")
class TestRealContentRoundTrip:
    """在**真实内容文件**上验证往返保真（只在内存里往返，不回写）。"""

    def test_parse_and_normalize_is_byte_stable(self) -> None:
        import io

        from ruamel.yaml import YAML

        root = _real_content_dir()
        assert root is not None

        y = YAML(typ="rt")
        y.preserve_quotes = True
        y.width = 4096
        y.indent(mapping=2, sequence=2, offset=0)

        files = sorted(root.rglob("*.yaml"))
        assert files, "真实内容库里没有 yaml 文件？"

        for path in files:
            src = path.read_text(encoding="utf-8")
            buf = io.StringIO()
            y.dump(y.load(io.StringIO(src)), buf)
            out = buf.getvalue()
            assert out == src, (
                f"{path.name} 往返后与原文不同——"
                f"说明内容库的缩进风格与 adapters 的设置不一致（{_first_diff(src, out)}）"
            )

    def test_store_agrees_with_real_content(self) -> None:
        """适配层能读通真实内容库，且能找到映射表指向的目标。"""
        from beacon.core.config import load_mapping

        root = _real_content_dir()
        assert root is not None
        store = RecallContentStore(root)

        for mapping in load_mapping().mappings.values():
            if mapping.is_check_only:
                continue
            assert store.exists(mapping.target), (
                f"映射指向的目标在真实内容库里不存在：{mapping.target}（指标 {mapping.indicator}）"
            )
            doc = store.read(mapping.target)
            # 槽位路径必须真的存在——否则运行到 F6 会报配置错误
            from beacon.core.path import get as path_get

            path_get(doc, mapping.slot.path)
