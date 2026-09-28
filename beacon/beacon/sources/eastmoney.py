"""东方财富 datacenter —— 中国 PMI。

实测记录（2026-09-23，见 docs/数据源清单.md §2.2）
--------------------------------------------------
* 端点：`GET /api/data/v1/get?reportName=RPT_ECONOMY_PMI&columns=ALL&...`
* **必需 `Referer: https://data.eastmoney.com/`**（缺了可能被拒），已写在 config 里
* 实测拿到 224 条月度记录（2026-08 制造业 49.8 / 非制造业 49.0）
* 层级：L2（东财转发官方统计；上游为国家统计局与中国物流与采购联合会）

两个必须小心的点
----------------
1. **报表名不可想当然**：`RPT_ECONOMY_TOTAL_SOCIETY_FINANCE`、`RPT_ECONOMY_MONEY_SUPPLY`
   实测都返回"报表配置不存在"。新增指标必须先实测报表名。
2. **源不提供发布日**：只有 `REPORT_DATE`（统计期间）。所以 `published_at` 留空。

   ⚠️ **下游不能用"抓取时间"兜底**——PRD §4.3 要求「内容截至」是**实际发布日**，
   用抓取时间会把"数据很旧"掩盖成"刚刚更新过"。正确做法是在 `config/thresholds.yaml`
   的 `publication_convention` 里**显式声明**该指标的发布惯例（如 `period_end`），
   那是一条约定的、可核对的事实；没有声明惯例的指标会在 F3 被直接丢弃。

本适配器额外产出「同比」口径
----------------------------
除了水平值（`MAKE_INDEX` / `NMAKE_INDEX`），源还给同比百分比
（`MAKE_SAME` / `NMAKE_SAME`）。它们被标成 `caliber=YOY` 的观测，
**只用于 C2 同源多口径互校，不进内容**（在映射表里登记为 `role: check-only`）。

这不是顺手多抓：漏掉它，这个指标就失去了唯一可用的交叉校验手段。
实测（224 条真实记录）用它能做到 212/212 零偏差的自洽检验。
"""

from __future__ import annotations

import json
from typing import Any

from ..core.contract import Caliber, Observation
from .base import SourceAdapter, SourceError

# (报表字段, 指标 id, 单位, 口径)。新增口径只需在这里加一行。
FIELD_MAP: tuple[tuple[str, str, str, Caliber], ...] = (
    ("MAKE_INDEX", "cn.pmi.manufacturing", "点", Caliber.LEVEL),
    ("NMAKE_INDEX", "cn.pmi.non-manufacturing", "点", Caliber.LEVEL),
    # 同比百分比：只用于 C2 互校，不进内容（映射表里 role: check-only）
    ("MAKE_SAME", "cn.pmi.manufacturing.yoy", "%", Caliber.YOY),
    ("NMAKE_SAME", "cn.pmi.non-manufacturing.yoy", "%", Caliber.YOY),
)

REPORT_NAME = "RPT_ECONOMY_PMI"


class EastmoneyAdapter(SourceAdapter):
    name = "eastmoney"
    upstream = "国家统计局 / 中国物流与采购联合会（东财转发）"

    def __init__(self, config: dict, fetcher, page_size: int = 240) -> None:
        super().__init__(config, fetcher)
        self.page_size = page_size

    # ------------------------------------------------------------------ 取数

    def endpoint(self, *, page_size: int | None = None) -> str:
        base = self.config.get("base_url", "https://datacenter-web.eastmoney.com")
        size = self.page_size if page_size is None else page_size
        return (
            f"{base}/api/data/v1/get"
            f"?reportName={REPORT_NAME}&columns=ALL&pageSize={size}"
            f"&sortColumns=REPORT_DATE&sortTypes=-1"
        )

    def fetch_raw(self) -> tuple[str, dict[str, Any], object]:
        url = self.endpoint()
        result = self._fetch_result(url)
        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise SourceError(self.name, f"响应不是合法 JSON：{exc}") from exc
        return url, payload, result

    def normalize(self, raw: tuple[str, dict[str, Any], object]) -> list[Observation]:
        url, payload, result = raw

        # 东财用 success 字段表示业务层成败，HTTP 200 不代表业务成功
        if not payload.get("success"):
            raise SourceError(
                self.name,
                f"业务层失败：{payload.get('message')!r}（code={payload.get('code')}）",
            )
        rows = (payload.get("result") or {}).get("data")
        if not isinstance(rows, list):
            raise SourceError(self.name, "响应缺少 result.data 列表（源结构可能已变更）")

        prov = self._provenance(result)  # type: ignore[arg-type]
        observations: list[Observation] = []
        for row in rows:
            period = self._period_of(row.get("REPORT_DATE"))
            if period is None:
                continue
            for field, indicator, unit, caliber in FIELD_MAP:
                value = row.get(field)
                # 缺失/异常值一律跳过，**不填 0**（宁缺勿造）
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    continue
                observations.append(
                    self._observation(
                        indicator=indicator,
                        period=period,
                        value=float(value),
                        unit=unit,
                        caliber=caliber,
                        url=url,
                        provenance=prov,
                        published_at=None,  # 源不提供发布日；由声明在配置里的发布惯例决定
                    )
                )
        return observations

    # ------------------------------------------------------------- 内部解析

    @staticmethod
    def _period_of(raw_date: object) -> str | None:
        """`2026-08-01 00:00:00` → `2026-08`。格式不符则跳过该行。"""
        if not isinstance(raw_date, str) or len(raw_date) < 7:
            return None
        head = raw_date[:7]
        if len(head) != 7 or head[4] != "-":
            return None
        year, month = head[:4], head[5:7]
        if not (year.isdigit() and month.isdigit()) or not (1 <= int(month) <= 12):
            return None
        return f"{year}-{month}"

    def health_url(self) -> str:
        # 探活只要一个 2xx，不必拉 240 条
        return self.endpoint(page_size=1)
