"""FRED 通用连接器：**按序列号取数**，不需要为每条序列写代码、也不需要提示词。

## 为什么需要它（用户 2026-09-29 两条要求撞在一起）

1. 「美联储概率 / 宏观利率 / 失业率缺失…**需要补齐到最新**」；
2. 「尽可能不要把任务交给 **prompt 教学**，联网查询各类问题**能否不用提示词也精准连接**」。

实测根因（现拉源侧证据）：AkShare 的东财 `macro_usa_*` **接口本身停更**——
`us_unemployment` 最新行 2025-09-05 且值为 `nan`、`us_nonfarm` 2025-09-05 `nan`、
`us_core_cpi` 2025-09-11 `nan`、`us_fed_rate` 2025-10-30 `nan`、
`us_pce` 2025-08-29（最后有效值 2.9）。**不是我们没跑作业**（同批的
`us_cpi_yoy` 到 2026-09，证明作业在跑），是**源侧没有新数据**。

换源已实测可行（FRED 免费 CSV，**无需 API key**、本机可达）：
`UNRATE → 2026-08-01 = 4.1` / `PAYEMS → 2026-08-01` /
`CPILFESL → 2026-08-01 = 337.765` / `PCEPILFE → 2026-07-01 = 130.658` /
`DFEDTARU → 2026-09-29 = 4.00`（与库里既有的 `fed:target_upper` 一致 ⇒ 同源已验证）。

## 为什么做成"通用 + 按 id"（而不是给每条序列写一个连接器）

* **不用提示词**：可达性由 **`supports()` 的机器判据**决定
  （`fred:<SERIES_ID>` 形态即认），而不是"模型记不记得有这个源"；
* **不用逐条写代码**：加一条新序列 = 在 `configs/indicators.yaml` 登记
  `fred:XXX` 一条；**没有第二个地方要改**；
* 与既有 `fed:effr`/`fed:target_upper/lower` **同源**（那三条本来就走 FRED），
  不引入新依赖、不新增付费额度（FRED 免费且无需 key）。

## 口径（随 extra 下发）

* 值是**序列原始值**：指数类（`CPILFESL`/`PCEPILFE`）是**指数**不是同比
  ——同比要在派生层算（`configs/derived_indicators.yaml` 的登记方式已就绪）；
* `PAYEMS` 是**就业人数（千人）**，不是"新增"——"非农新增"= 本月 − 上月（派生）；
* 期间口径为**月度**（该序列自己给的 `DATE`），缺失周以空值行存在，已跳过。
"""

from __future__ import annotations

import csv
import io
import logging
import re
import urllib.request
from typing import Any

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

#: 序列号形态（FRED 的 id 是 1~20 位大写字母/数字，可能含下划线）。
_SERIES_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,19}$")

#: 端点（**无需 key**；`fredgraph.csv` 是官方免费下载入口）。
_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"

#: 已知序列的**人话口径**（没有也能取；有则随 extra 下发，省掉"模型去猜单位"）。
_KNOWN: dict[str, tuple[str, str]] = {
    "UNRATE": ("美国失业率", "%"),
    "PAYEMS": ("美国非农就业人数（千人，水平值；新增需自算）", "千人"),
    "CPIAUCSL": ("美国CPI指数（同比需自算）", "指数"),
    "CPILFESL": ("美国核心CPI指数（同比需自算）", "指数"),
    "PCEPI": ("美国PCE指数（同比需自算）", "指数"),
    "PCEPILFE": ("美国核心PCE指数（同比需自算）", "指数"),
    "DFEDTARU": ("美联储目标区间上限", "%"),
    "DFEDTARL": ("美联储目标区间下限", "%"),
    "DFF": ("有效联邦基金利率", "%"),
    "DGS10": ("美国10年期国债收益率", "%"),
    "DGS2": ("美国2年期国债收益率", "%"),
    "T10Y2Y": ("10年-2年利差", "%"),
    "VIXCLS": ("VIX 收盘", "点"),
    "DTWEXBGS": ("美元指数（广义贸易加权）", "指数"),
}


class FredConnector(BaseConnector):
    """按序列号从 FRED 取数（免费、无 key、公开 CSV）。"""

    source_name = "FRED(圣路易斯联储)"
    source_url = "https://fred.stlouisfed.org/"

    def get_capabilities(self) -> dict[str, Any]:
        # ⚠️ **刻意不在 `indicators` 里声明具体序列**：序列空间是开放的
        #   （任何 `fred:XXX` 都认），声明几条反而会让"能力清单"与"实际可认"
        #   不一致 —— 而契约护栏（①↔①）就是查这个不一致的。
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "indicators": [],
            "supports_template": "fred:{SERIES_ID}（如 fred:UNRATE / fred:DGS10）",
            "known_series": {k: v[0] for k, v in _KNOWN.items()},
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        prefix, sep, series = indicator.partition(":")
        return bool(sep) and prefix == "fred" and bool(_SERIES_RE.match(series.strip()))

    @staticmethod
    def _download(series: str) -> str:
        url = _CSV_URL.format(series=series)
        with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310 固定官方域名
            return resp.read().decode("utf-8", errors="replace")

    async def fetch(
        self, indicator: str, start_date: str | None = None,
        end_date: str | None = None, **kwargs: Any,
    ) -> list[DataPoint]:
        import asyncio

        if not self.supports(indicator):
            raise DataFetchError(f"FRED 连接器不支持指标 {indicator!r}")
        series = indicator.partition(":")[2].strip()

        def _load() -> list[tuple[str, float]]:
            text = self._download(series)
            rows: list[tuple[str, float]] = []
            for row in csv.reader(io.StringIO(text)):
                if len(row) < 2 or row[0] == "DATE":
                    continue
                raw = (row[1] or "").strip()
                # FRED 用空串表示"该期无值"——**跳过，绝不填 0**
                if not raw or raw in (".", "nan"):
                    continue
                try:
                    value = float(raw)
                except ValueError:
                    continue
                rows.append((row[0], value))
            return rows

        rows = await asyncio.to_thread(_load)
        if not rows:
            raise DataFetchError(
                f"fred:{series} 没有可用读数（序列号是否存在？空值行已跳过；"
                "**没量到 ≠ 量到 0**）")
        label, unit = _KNOWN.get(series, (series, ""))
        points: list[DataPoint] = []
        for day, value in rows:
            if start_date and day < start_date:
                continue
            if end_date and day > end_date:
                continue
            points.append(DataPoint(
                indicator=f"fred:{series}", value=value, period_date=day,
                source_name=f"{self.source_name}（{series}）",
                source_url=self.source_url, fetch_method=FetchMethod.API_CALL,
                source_type=DataSourceType.API,
                extra={
                    "series": series, "label": label, "unit": unit,
                    "caliber": "FRED 原始序列值（免费 CSV，无需 API key）",
                    "not_derived": True,
                    "hint": ("指数类/水平值类序列的'同比/新增'要在派生层算："
                             "见 configs/derived_indicators.yaml"),
                },
            ))
        return points


__all__ = ["FredConnector"]
