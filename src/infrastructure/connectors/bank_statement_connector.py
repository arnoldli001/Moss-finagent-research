"""银行报表口径连接器（`利息净收入` / `利息收入` / `利息支出` / `总资产`）。

## 为什么单独一个连接器（而不是塞进 AkShare 主链）

用户 2026-09-29 的三步走第一条：「补两个输入：**利息净收入**（可拆
`利息收入 − 利息支出`）与**生息资产**（或先用 `总资产` 退化并标注 bias）」。

* 这几个字段**只有银行/金融类报表口径**才有意义（`利息净收入` 在一般工商企业
  的利润表里根本不存在），放进通用财务比率链会污染那一族的口径说明；
* 数据源与既有链**不同**：这里是新浪`财务分析`模板的**报表口径**
  （实测 600036：`净利息收入=1120.22亿` / `利息收入=1727.33亿` /
  `利息支出=607.11亿`，102 期，最新一期直接可用）；
* 新文件 = 对既有链路**零改动风险**（本仓库的纪律：注入式 > 新文件式 > 重构）。

## 口径与局限（随 extra 下发）

* 值取自**合并报表**、单位**元**、**累计口径**（三季报=前三季度累计）；
* 报表字段是**期末/累计值**，不是"平均余额" —— 净息差的**分母**若要严格
  按"生息资产平均余额"，需要在派生层做 (期初+期末)/2，**本连接器只给原始字段**；
* `总资产` 的列名按**模糊匹配**找（`资产总计` / `总资产`），找不到就**报缺口**
  （`DataFetchError`），不返回 0、不猜列。

登记：`configs/indicators.yaml`；路由：`src/api/runtime.py`。
"""

from __future__ import annotations

import logging
from typing import Any

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

#: 支持的指标前缀（`{code}` = 6 位 A 股代码；本连接器只认这几个**报表字段**）。
_INCOME_PREFIX = "利息净收入"
_SUPPORTED: dict[str, str] = {
    "利息净收入": "利润表：净利息收入（= 利息收入 − 利息支出，银行口径）",
    "利息收入": "利润表：利息收入",
    "利息支出": "利润表：利息支出",
    "总资产": "资产负债表：资产总计（**生息资产的粗近似**，见 bias）",
    "生息资产": "资产负债表：按监管口径需另行拆分，本连接器**不提供**",
}

#: 新浪报表模板里各字段的**候选列名**（按序模糊匹配，命中即用）。
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "利息净收入": ("净利息收入", "利息净收入"),
    "利息收入": ("利息收入",),
    "利息支出": ("利息支出",),
    "总资产": ("资产总计", "总资产"),
}


def _pick_column(columns: list[str], aliases: tuple[str, ...]) -> str | None:
    """在报表列里按候选名找（先精确、再包含）——**找不到返回 None**，不猜。"""
    for alias in aliases:
        for col in columns:
            if str(col).strip() == alias:
                return str(col)
    for alias in aliases:
        for col in columns:
            if alias in str(col):
                return str(col)
    return None


class BankStatementConnector(BaseConnector):
    """银行报表字段（新浪财务分析报表模板，按报告期）。"""

    source_name = "新浪财务分析(报表口径)"
    source_url = "https://vip.stock.finance.sina.com.cn/corp/go.php/vFD_FinanceSummary"

    #: 报表类型 → 新浪的 `symbol` 参数
    _SHEETS = {"利润表": ("利润表", ("利息净收入", "利息收入", "利息支出")),
               "资产负债表": ("资产负债表", ("总资产",))}

    def get_capabilities(self) -> dict[str, Any]:
        # ⚠️ **只声明真的会提供的前缀**：`生息资产` 本连接器**不提供**
        #    （需要监管口径拆分），所以它**不进** `indicators` ——
        #    声明即承诺，契约护栏
        #    （`test_connector_declared_code_prefixes_are_registered`）
        #    会因为"声明了但没登记"直接报红（实测就是这么被拦下的）。
        #    取它时**明确拒绝**并给出出路（见 `fetch` 的 DataFetchError）。
        provided = [k for k in _SUPPORTED if k != "生息资产"]
        return {
            "name": self.source_name,
            "source_type": DataSourceType.REPORT.value,
            "indicators": [f"{k}:{{code}}" for k in provided],
            "notes": {k: v for k, v in _SUPPORTED.items()},
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        prefix, sep, code = indicator.partition(":")
        return bool(sep) and prefix in _SUPPORTED and len(code) == 6 and code.isdigit()

    def _sheet_for(self, prefix: str) -> str:
        for sheet, (_param, fields) in self._SHEETS.items():
            if prefix in fields:
                return sheet
        raise DataFetchError(f"银行报表连接器没有 {prefix} 的报表归属（配置缺失）")

    async def fetch(
        self, indicator: str, start_date: str | None = None,
        end_date: str | None = None, **kwargs: Any,
    ) -> list[DataPoint]:
        """取该字段的**全部报告期**（让上层自己选最新/算平均）。"""
        import asyncio

        prefix, _, code = indicator.partition(":")
        if not self.supports(indicator):
            raise DataFetchError(f"银行报表连接器不支持指标 {indicator!r}")
        if prefix == "生息资产":
            raise DataFetchError(
                f"{prefix}:{code} 需要监管口径的生息资产拆分，本连接器不提供 —— "
                "派生层请先用 `总资产` 近似并在 extra 里标注 bias")

        sheet = self._sheet_for(prefix)
        param = self._SHEETS[sheet][0]
        aliases = _COLUMN_ALIASES[prefix]

        def _load() -> tuple[str, list[tuple[str, float]]]:
            import akshare as ak

            df = ak.stock_financial_report_sina(stock=f"{'sh' if code[0] == '6' else 'sz'}{code}",
                                                symbol=param)
            columns = [str(c) for c in df.columns]
            column = _pick_column(columns, aliases)
            if column is None:
                return "", []
            rows: list[tuple[str, float]] = []
            for _, row in df.iterrows():
                raw = row.get(column)
                period = str(row.get("报告日") or row.get(columns[1]) or "")
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                if period:
                    rows.append((period, value))
            return column, rows

        column, rows = await asyncio.to_thread(_load)
        if not rows:
            raise DataFetchError(
                f"{prefix}:{code} 在 {sheet} 里没有可用读数"
                f"（候选列 {aliases}；**没量到 ≠ 量到 0**，请用 [采集缺口] 日志排查）")
        points: list[DataPoint] = []
        for period, value in rows:
            iso = period if "-" in period else f"{period[:4]}-{period[4:6]}-{period[6:8]}"
            points.append(DataPoint(
                indicator=f"{prefix}:{code}", value=value, period_date=iso,
                source_name=f"{self.source_name}（{sheet}.{column}）",
                source_url=self.source_url, fetch_method=FetchMethod.API_CALL,
                source_type=DataSourceType.REPORT,
                extra={
                    "sheet": sheet, "column": column, "unit": "元",
                    "caliber": "合并报表；累计口径（三季报=前三季度累计）",
                    "not_average": True,
                    "bias": ("报表字段是**期末/累计值**；净息差分母若要严格按"
                             "「生息资产平均余额」，需在派生层做 (期初+期末)/2"),
                },
            ))
        return points


__all__ = ["BankStatementConnector"]
