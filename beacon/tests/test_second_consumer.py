"""第二个消费者（IND-5 / A-C13）。

**"能被别的 app 用"这句话，只有一个证明方式：真的接一个别的 app 进来。**

所以这个测试实现了一个与 Recall 完全不同的消费者：
JSON 文件、单个文档包含多条记录、字段结构不是 blocks/series。

如果 core 或 sources 里绑定了 Recall 的任何东西，这个测试根本写不出来——
不是"会失败"，而是"没有可用的接口"。这就是它与"扫词表"（IND-4）的差别：
IND-4 查的是"有没有出现不该出现的词"，这条查的是"不做任何修改能不能用"。

⚠️ 它不修改 core/ 与 sources/ 的任何一行。这正是要证明的事。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from beacon.core.config import load_mapping, load_settings, load_thresholds
from beacon.core.contract import Caliber, Observation, Provenance, SourceRef, Tier
from beacon.core.flags import FlagsBook
from beacon.core.funnel import FunnelContext, run_funnel
from beacon.core.leveling import Level
from beacon.core.path import clone, get as path_get
from beacon.core.store import TargetError


# --------------------------------------------------------------------------- #
# 一个与 Recall 毫无关系的消费者
# --------------------------------------------------------------------------- #


class JsonLedgerStore:
    """把「一条记录 = 一个 JSON 文件」当成目标仓库。

    与 Recall 的差异是刻意的（越不像，证明力越强）：

    | | Recall | 这里 |
    |---|---|---|
    | 载体 | 一个区一个 YAML，含多条记录 | 一条记录一个 JSON 文件 |
    | 目标 id | 记录里的 `id` | **文件名** |
    | 数值槽位 | `blocks[2].series[0].points` | `charts.byWindow.points` |
    | 溯源文字 | `source` / `updated_at` | `provenanceLine` / `revisionDate` |

    它只需要实现 core 的四个方法（targets / exists / read / write），
    **接口之外的东西 core 一概不知道**。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, target_id: str) -> Path:
        # 文件名 = 目标 id。用一个极简的"扁平化"规则，与 Recall 完全不同。
        return self.root / f"{target_id.replace('.', '__')}.json"

    def targets(self) -> list[str]:
        return sorted(
            p.stem.replace("__", ".") for p in self.root.glob("*.json")
        )

    def exists(self, target_id: str) -> bool:
        return self._path(target_id).exists()

    def read(self, target_id: str) -> Any:
        p = self._path(target_id)
        if not p.exists():
            raise TargetError(f"账本里没有这条记录：{target_id}")
        return clone(json.loads(p.read_text(encoding="utf-8")))

    def write(self, target_id: str, document: Any) -> None:
        if not self.exists(target_id):
            raise TargetError(f"账本里没有这条记录：{target_id}")
        self._path(target_id).write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


# --------------------------------------------------------------------------- #
# 用一套只属于这个消费者的映射表
# --------------------------------------------------------------------------- #


LEDGER_MAPPING = {
    "version": 1,
    "operators": {
        "percentile-rank": {"kind": "rank-share"},
    },
    # 模板名是中性的；写到哪个字段由本消费者自己声明（text_targets）。
    # core 里没有 "source"、"updated_at" 这类字段名——这是接第二个消费者时才修掉的。
    "templates": {
        "revision_date": "{as_of}",
        "item_source": "{authority}{via_note}，截至 {as_of}",
        "block_source": "{series_label}；数据来源：{authority}，截至 {as_of}",
    },
    "indicators": {
        "cn.index.pe.000300": {
            "target": "ledger.pe.000300",
            "role": "content",
            "slot": {
                "path": "charts.byWindow.values",       # ← 字段名完全不同
                "series_name": "分位",
                "series_label": "沪深300 估值分位",
                "xTicks": ["近 3 年", "近 5 年", "近 10 年", "全历史"],
            },
            "expected_caliber": "level",
            "transform": {
                "operator": "percentile-rank",
                "params": {
                    "windows": [
                        {"label": "近 3 年", "days": 1095},
                        {"label": "近 5 年", "days": 1825},
                        {"label": "近 10 年", "days": 3650},
                        {"label": "全历史", "days": None},
                    ]
                },
            },
            "checks": ["C3"],
            "text_targets": {
                "revision_date": "revisionDate",     # ← 字段名与 Recall 完全不同
                "item_source": "provenanceLine",
            },
            "writable_fields": [
                "charts.byWindow.values",
                "charts.byWindow.xTicks",
                "charts.byWindow.source",
                "provenanceLine",       # ← 名字也不同
                "revisionDate",
            ],
        }
    },
}

LEDGER_DOCUMENT = {
    "key": "ledger.pe.000300",
    "revisionDate": "2026-09-01",
    "provenanceLine": "旧的一行出处，截至 2026-09-01",
    "charts": {
        "byWindow": {
            "source": "沪深300 估值分位；数据来源：中证指数有限公司",
            "xTicks": ["近 3 年", "近 5 年", "近 10 年", "全历史"],
            # 槽位路径就是这里：扁平的一段数值，没有 series 这一层。
            # 越不像 Recall，这个测试的证明力越强。
            "values": [88, 72, 54, 46],
        }
    },
}


def pe_series(days: int = 1200, *, end: str = "2026-09-22") -> list[Observation]:
    """日度 PE 序列。与 Recall 的测试构造独立写一遍——故意不共用夹具。"""
    from datetime import timedelta

    end_date = date.fromisoformat(end)
    out: list[Observation] = []
    for i in range(days - 1, -1, -1):
        d = end_date - timedelta(days=i)
        out.append(
            Observation(
                indicator="cn.index.pe.000300",
                period=d.isoformat(),
                value=round(12.0 + (i % 97) * 0.05, 4),
                unit="倍",
                caliber=Caliber.LEVEL,
                source=SourceRef(
                    name="中证指数有限公司",
                    url="https://example.test/pe",
                    tier=Tier.L1,
                    upstream="中证指数有限公司（指数编制机构，一手）",
                    channel=None,
                    published_at=d,
                ),
                provenance=Provenance(
                    fetched_at=datetime(2026, 9, 23, tzinfo=UTC),
                    http_status=200,
                    from_cache=False,
                    raw_sha256="b" * 64,
                ),
            )
        )
    return out


def build_ctx(tmp_path: Path, mapping_path: Path) -> tuple[FunnelContext, JsonLedgerStore]:
    store = JsonLedgerStore(tmp_path / "ledger")
    # 用这个消费者自己的文档与槽位结构。注意 store 是**先**读后写：
    # 这里手工放入一条记录
    (tmp_path / "ledger" / "ledger__pe__000300.json").write_text(
        json.dumps(LEDGER_DOCUMENT, ensure_ascii=False), encoding="utf-8"
    )
    ctx = FunnelContext(
        mappings=load_mapping(mapping_path),
        thresholds=load_thresholds(),
        settings=load_settings(),
        flags=FlagsBook(source="missing"),
        store=store,
        today=date(2026, 9, 23),
        generated_at=datetime(2026, 9, 23, tzinfo=UTC),
    )
    return ctx, store


class TestSecondConsumer:
    def test_a_completely_different_consumer_just_works(self, tmp_path: Path) -> None:
        """整条漏斗跑在一个结构完全不同的仓库上。"""
        import yaml

        mapping_path = tmp_path / "ledger-mapping.yaml"
        mapping_path.write_text(
            yaml.safe_dump(LEDGER_MAPPING, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        ctx, store = build_ctx(tmp_path, mapping_path)

        outcome = run_funnel(pe_series(), ctx)

        assert len(outcome.changeset.candidates) == 1
        cand = outcome.changeset.candidates[0]
        assert cand.target == "ledger.pe.000300"
        assert cand.proposal is not None, "该消费者也应当能拿到可合入的片段"

        # 值确实算对了
        values = path_get(cand.proposal, "charts.byWindow.values")
        assert len(values) == 4
        assert all(0 <= v <= 100 for v in values), "分位必须在 0-100"

        # 溯源文字由**模板**生成，且用的是这个消费者自己的字段名
        assert str(cand.proposal["revisionDate"]).startswith("2026-09-22")
        assert "截至 2026-09-22" in cand.proposal["provenanceLine"]

    def test_the_same_funnel_serves_both_consumers(self, tmp_path: Path) -> None:
        """同一套 core，两个消费者：唯一的差别是映射表与仓库实现。

        这条是"加一个 app 不改 core 与 sources"（A-C13）最直接的证明。
        """
        import yaml

        from test_funnel import PE_TARGET, pe_daily, pe_document
        from beacon.core.store import MemoryStore

        # 消费者 A：Recall 形态（用内存仓库代替真实内容库）
        recall_ctx = FunnelContext(
            mappings=load_mapping(),                     # 包内那份 = Recall 的映射
            thresholds=load_thresholds(),
            settings=load_settings(),
            flags=FlagsBook(source="missing"),
            store=MemoryStore({PE_TARGET: pe_document()}),
            today=date(2026, 9, 23),
            generated_at=datetime(2026, 9, 23, tzinfo=UTC),
        )
        a = run_funnel(pe_daily(1200), recall_ctx)

        # 消费者 B：账本形态
        mapping_path = tmp_path / "ledger-mapping.yaml"
        mapping_path.write_text(
            yaml.safe_dump(LEDGER_MAPPING, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        ctx_b, _ = build_ctx(tmp_path, mapping_path)
        b = run_funnel(pe_series(), ctx_b)

        # 两边都产出了候选，且用的是同一份 core 代码
        assert len(a.changeset.candidates) == 1
        assert len(b.changeset.candidates) == 1
        assert a.changeset.candidates[0].target == PE_TARGET
        assert b.changeset.candidates[0].target == "ledger.pe.000300"

        # 派生值的计算方式完全相同（同一个算子）
        va = path_get(a.changeset.candidates[0].proposal, "blocks[1].series[0].points")
        vb = path_get(b.changeset.candidates[0].proposal, "charts.byWindow.values")
        assert va == vb, "同一个算子给出同一套值——差别只在写到哪里"

    def test_unknown_field_path_is_a_config_error(self, tmp_path: Path) -> None:
        """映射到不存在的字段 → 报配置错误，而不是静默跳过。

        这是 F6 的"映射到不存在的字段路径 → 报错"。
        """
        import yaml

        import pytest

        from beacon.core.path import PathError

        broken = json.loads(json.dumps(LEDGER_MAPPING))
        broken["indicators"]["cn.index.pe.000300"]["slot"]["path"] = "charts.nope.values"
        broken["indicators"]["cn.index.pe.000300"]["writable_fields"] = [
            "charts.nope.values", "charts.nope.source", "provenanceLine", "revisionDate"
        ]
        mapping_path = tmp_path / "broken.yaml"
        mapping_path.write_text(
            yaml.safe_dump(broken, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        ctx, _ = build_ctx(tmp_path, mapping_path)

        with pytest.raises(PathError, match="没有 charts.nope.values 这个路径"):
            run_funnel(pe_series(), ctx)
