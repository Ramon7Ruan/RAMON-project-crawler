"""中证指数有限公司 —— 指数估值（PE）。

实测记录（2026-09-23，见 docs/数据源清单.md §2.3）
--------------------------------------------------
* 端点：`GET /csindex-home/perf/index-perf?indexCode=000300&startDate=YYYYMMDD&endDate=YYYYMMDD`
* 实测拿到沪深300 日度 PE（2026-09-22 → 13.53，收盘 4544.59）
* 层级：**L1**（指数编制机构，一手）——首版三个源里唯一的一手源

三个必须小心的点
----------------
1. **`200 + data: []` 是伪成功**。参数格式写错时（例如日期写成 `2026-09-01`）
   服务端返回 `{"code":"200","msg":"Success","data":[]}`——HTTP 200、业务码 200、但**没有数据**。
   这是最容易误判为"接口没数据"的假象，必须在解析层拦下并报错。
2. **日期必须是 `YYYYMMDD`（无连字符）**。
3. **`peg` 字段实际是 PE（市盈率）**，不是 PEG。字段名不可望文生义，
   已用真实值核对（13.53 对沪深300 是合理的 PE 量级）。本模块用常量 `PE_FIELD` 显式声明这层映射。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from ..core.contract import Caliber, Observation
from .base import SourceAdapter, SourceError

PE_FIELD = "peg"
"""⚠️ 服务端字段名叫 peg，含义是 PE。改名会让代码可读但误导协作者，故保留原名并在此显式说明。"""


class CsindexAdapter(SourceAdapter):
    name = "csindex"
    upstream = "中证指数有限公司（一手）"

    def __init__(
        self,
        config: dict,
        fetcher,
        index_code: str = "000300",
        history_days: int = 3650,
    ) -> None:
        super().__init__(config, fetcher)
        self.index_code = index_code
        self.history_days = history_days

    # ------------------------------------------------------------------ 取数

    def endpoint(self, *, today: date | None = None, history_days: int | None = None) -> str:
        base = self.config.get("base_url", "https://www.csindex.com.cn")
        end = today or date.today()
        days = self.history_days if history_days is None else history_days
        start = end - timedelta(days=days)
        fmt = "%Y%m%d"  # ⚠️ 必须无连字符，写成 YYYY-MM-DD 会得到空数组
        return (
            f"{base}/csindex-home/perf/index-perf"
            f"?indexCode={self.index_code}"
            f"&startDate={start.strftime(fmt)}&endDate={end.strftime(fmt)}"
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
        rows = payload.get("data")

        if not isinstance(rows, list):
            raise SourceError(self.name, f"响应缺少 data 列表：{str(payload)[:160]}")
        if not rows:
            # 这一条是专门为"伪成功"写的：HTTP 200 + code 200，但一条数据都没有。
            # 报出来，而不是让空列表悄悄走到下游变成"这次没有值"。
            raise SourceError(
                self.name,
                "接口返回成功但 data 为空（常见原因：日期格式必须是 YYYYMMDD 且区间无效）",
            )

        prov = self._provenance(result)  # type: ignore[arg-type]
        observations: list[Observation] = []
        for row in rows:
            period = self._period_of(row.get("tradeDate"))
            value = row.get(PE_FIELD)
            if period is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                # PE ≤ 0 在指数层面不成立（负值意味着整体亏损），视为无效而非"便宜"
                continue
            observations.append(
                self._observation(
                    indicator=f"cn.index.pe.{self.index_code}",
                    period=period,
                    value=float(value),
                    unit="倍",
                    caliber=Caliber.LEVEL,
                    url=url,
                    provenance=prov,
                    # 指数估值在交易日收盘后更新，故「数据日期即发布日」
                    published_at=date.fromisoformat(period),
                )
            )
        return observations

    # ------------------------------------------------------------- 内部解析

    @staticmethod
    def _period_of(raw_date: object) -> str | None:
        """`20260922` → `2026-09-22`。格式不符则跳过该行。"""
        if not isinstance(raw_date, str) or len(raw_date) != 8 or not raw_date.isdigit():
            return None
        return f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"

    def health_url(self) -> str:
        # 探活只要 2xx，用一周区间而不是默认的十年
        return self.endpoint(history_days=7, today=date.today())
