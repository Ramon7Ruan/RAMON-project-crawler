"""FRED（圣路易斯联储）—— 美债收益率曲线。

实测记录（2026-09-23，见 docs/数据源清单.md §2.1）
--------------------------------------------------
* 端点：`GET /graph/fredgraph.csv?id=<SERIES_ID>`，**无需 API key**
* 返回：纯 CSV 两列 `observation_date,<SERIES_ID>`，1962 年至今
* 单次 268KB / 5.7s → 配置里给了独有的 40s 超时
* 层级：L2（FRED 由美联储系统运营，但性质是"官方数据的分发渠道"）

两个必须小心的点
----------------
1. **缺失值以 `.` 表示**，不是 0、也不是空。把它当 0 会污染整个序列。
2. 官方 API（`api.stlouisfed.org`）**需要 api_key**，本适配器因此不用它。
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from ..core.contract import Caliber, Observation, Provenance
from ..core.fetch import FetchResult
from .base import SourceAdapter, SourceError

SERIES_MAP: dict[str, str] = {
    "DGS10": "us.treasury.dgs10",
    "DGS2": "us.treasury.dgs2",
    "DGS3MO": "us.treasury.dgs3mo",
}
"""FRED 序列 id → 我们的指标 id。新增期限只需在这里加一行。"""

_MISSING_TOKENS = {".", "", "NA", "N/A", "null"}
"""FRED 用 `.` 表示缺失。这些一律**跳过**，绝不填 0（PRD §4.3 E5 宁缺勿造）。"""


class FredAdapter(SourceAdapter):
    name = "fred"
    upstream = "美国财政部 / 美联储"

    def __init__(
        self,
        config: dict,
        fetcher,
        series: list[str] | None = None,
        lookback_days: int | None = None,
    ) -> None:
        super().__init__(config, fetcher)
        self.series = series or list(SERIES_MAP)
        # 只取近期序列。FRED 的全量 CSV 是 268KB / 1962 年至今，
        # 而我们画图与判断倒挂只需要最近一年上下。限定起始日期能让响应降到几 KB，
        # 既快又符合"礼貌抓取"（PRD §3.4）——不为了取 200 个点去下载 16000 行。
        self.lookback_days = int(
            lookback_days
            if lookback_days is not None
            else config.get("lookback_days", 400)
        )

    # ------------------------------------------------------------------ 取数

    def series_url(self, sid: str, *, today: date | None = None) -> str:
        base = self.config.get("base_url", "https://fred.stlouisfed.org")
        start = (today or date.today()) - timedelta(days=self.lookback_days)
        return f"{base}/graph/fredgraph.csv?id={sid}&cosd={start.isoformat()}"

    def fetch_raw(self) -> dict[str, tuple[str, FetchResult]]:
        out: dict[str, tuple[str, FetchResult]] = {}
        for sid in self.series:
            if sid not in SERIES_MAP:
                raise SourceError(self.name, f"未登记的序列：{sid}")
            url = self.series_url(sid)
            out[sid] = (url, self._fetch_result(url))
        return out

    def normalize(self, raw: dict[str, tuple[str, FetchResult]]) -> list[Observation]:
        observations: list[Observation] = []
        for sid, (url, result) in raw.items():
            prov = self._provenance(result)
            for period, value, published in self._parse_csv(result.text, sid, url):
                observations.append(
                    self._observation(
                        indicator=SERIES_MAP[sid],
                        period=period,
                        value=value,
                        unit="%",
                        caliber=Caliber.LEVEL,
                        url=url,
                        provenance=prov,
                        published_at=published,
                    )
                )
        return observations

    # ------------------------------------------------------------- 内部解析

    def _parse_csv(
        self, text: str, sid: str, url: str
    ) -> list[tuple[str, float, date | None]]:
        reader = csv.reader(io.StringIO(text))
        header = next(reader, None)
        if not header or len(header) < 2:
            raise SourceError(self.name, f"{sid} 的 CSV 表头异常：{header!r}")

        rows: list[tuple[str, float, date | None]] = []
        for line_no, row in enumerate(reader, start=2):
            if len(row) < 2:
                continue
            raw_date, raw_value = row[0].strip(), row[1].strip()
            if raw_value in _MISSING_TOKENS:
                continue  # 缺失值：跳过，不填 0
            try:
                value = float(raw_value)
                obs_date = date.fromisoformat(raw_date)
            except ValueError as exc:
                raise SourceError(
                    self.name, f"{sid} 第 {line_no} 行无法解析（{raw_date!r}, {raw_value!r}）：{exc}"
                ) from exc

            period = obs_date.isoformat()
            # 日度市场数据当天发布，故「数据日期即发布日」。这不是推算，是市场数据的性质：
            # 收益率在交易日收盘后即为当日取值，不存在"统计期间结束后再发布"的时滞。
            rows.append((period, value, obs_date))

        if not rows:
            raise SourceError(self.name, f"{sid} 解析后没有任何有效观测（表头 {header!r}）")
        return rows

    def health_url(self) -> str:
        # 探活只要"通不通"，因此用最短区间（一周）而不是默认 lookback
        base = self.config.get("base_url", "https://fred.stlouisfed.org")
        start = date.today() - timedelta(days=7)
        return f"{base}/graph/fredgraph.csv?id={self.series[0]}&cosd={start.isoformat()}"
