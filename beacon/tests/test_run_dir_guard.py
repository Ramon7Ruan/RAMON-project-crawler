"""运行目录不得被复用（一次运行 = 一个目录）。

这条守卫防的是一种**最难发现的审计链断裂**：

    run A（16:01:12）→ changes.md / run.meta.json / proposals/
    run B（16:01:38，同一分钟内）→ 覆盖了 A 的 changes.md 与 run.meta.json，
                                    但 proposals/ 里还留着 A 的文件

结果目录看起来是完整的，实际是两个运行的混合。事后想回答
"当时到底提了什么、依据哪个指纹写回的" 已经不可能。

所以：运行号精确到秒；目录已存在就**拒绝**，不覆盖。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from beacon.cli import main
from beacon.core.contract import Caliber, Observation, Provenance, SourceRef, Tier

FIXED = datetime(2026, 9, 23, 16, 1, 12, tzinfo=UTC)
EXPECTED_RUN_ID = "2026-09-23-160112"

CONTENT = """\
- id: hotspot.data.valuation-percentile
  domain: hotspot
  track: data
  title: 估值分位
  source: 中证指数有限公司，截至 2026-09-01
  updated_at: 2026-09-01
  recipe: [prose, formula, dataviz]
  blocks:
  - {type: prose, label: 定义, body: "分位把当前估值放进历史区间里排序。"}
  - type: dataviz
    label: 不同区间下的分位
    chart: bar
    xTicks: ["近 3 年", "近 5 年", "近 10 年", "全历史"]
    series:
    - {name: 分位, points: [88, 72, 54, 46]}
    source: 沪深300 估值分位；数据来源：中证指数有限公司
"""


class _FixedDatetime:
    """钉住时钟，让"同一秒跑两次"成为可复现的测试场景。"""

    @staticmethod
    def now(tz: object = None) -> datetime:
        return FIXED


class _StubAdapter:
    name = "csindex"
    tier = Tier.L2

    def observations(self) -> list[Observation]:
        return [
            Observation(
                indicator="cn.index.pe.000300",
                period="2026-09-22",
                value=14.0,
                unit="倍",
                caliber=Caliber.LEVEL,
                source=SourceRef(
                    name="中证指数有限公司",
                    url="https://example.test/pe",
                    tier=Tier.L1,
                    upstream="中证指数有限公司（指数编制机构，一手）",
                    channel=None,
                    published_at=date(2026, 9, 22),
                ),
                provenance=Provenance(
                    fetched_at=FIXED, http_status=200, from_cache=False, raw_sha256="c" * 64
                ),
            )
        ]


@pytest.fixture
def wired(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """把取数换成桩、时钟钉死，其余流程（配置、漏斗、写文件）保持真实。"""
    content = tmp_path / "content" / "hotspot"
    content.mkdir(parents=True)
    (content / "hotspot.yaml").write_text(CONTENT, encoding="utf-8")

    monkeypatch.setattr("beacon.cli.datetime", _FixedDatetime)
    monkeypatch.setattr("beacon.cli._prepare", lambda args: ([_StubAdapter()], tmp_path / "cache"))
    return tmp_path / "content", tmp_path / "out"


def run_once(content: Path, out: Path) -> int:
    return int(
        main(["run", "--content-dir", str(content), "--out", str(out)])
    )


class TestRunDirIsNeverReused:
    def test_first_run_creates_a_second_precision_dir(self, wired) -> None:
        content, out = wired
        assert run_once(content, out) == 0

        dirs = sorted(p.name for p in out.iterdir() if p.is_dir())
        assert dirs == [EXPECTED_RUN_ID], "运行号应当精确到秒"
        assert (out / EXPECTED_RUN_ID / "changes.md").exists()

    def test_second_run_in_the_same_second_is_refused(self, wired, capsys) -> None:
        content, out = wired
        run_once(content, out)
        before = (out / EXPECTED_RUN_ID / "changes.md").read_text(encoding="utf-8")

        assert run_once(content, out) == 2, "目录已存在时必须拒绝，而不是覆盖"
        assert "不覆盖" in capsys.readouterr().err

        after = (out / EXPECTED_RUN_ID / "changes.md").read_text(encoding="utf-8")
        assert after == before, "第一次运行的产物必须逐字节不变"

    def test_no_half_mixed_directory(self, wired) -> None:
        """拒绝之后，目录里不能出现两次运行混在一起的痕迹。"""
        content, out = wired
        run_once(content, out)
        run_once(content, out)

        d = out / EXPECTED_RUN_ID
        meta = json.loads((d / "run.meta.json").read_text(encoding="utf-8"))
        assert meta["runId"] == EXPECTED_RUN_ID
        assert len([p for p in out.iterdir() if p.is_dir()]) == 1, "不得产生第二个目录"
