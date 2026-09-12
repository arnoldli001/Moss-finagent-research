"""模拟产业数据连接器（Demo用，替代付费产业数据接口）。

付费产业数据源（Wind/iFinD/Choice/Tushare Pro产业库等）接入前，用确定性
合成的月度行业指标序列打通A13-A16行业层端到端链路。所有产出三重标记为
模拟数据：source_name含"模拟"、source_url为mock://、extra.simulated=True，
confidence降为0.5，报告引用时天然披露，杜绝被误读为真实行情。

指标id约定："ind:{指标名}"，月度频率，最近_MONTHS个月（截至当前自然月）。
序列 = 基准 + 线性趋势 + 年度季节项 + 按指标名播种的伪随机噪声（同输入同输出）。

遗留：真实付费接口接入后，保持indicator id不变，仅替换本连接器实现即可
（见 docs/DEVELOPMENT_ROADMAP.md 遗留工作）。
"""

from __future__ import annotations

import math
import random
import zlib
from datetime import date
from typing import Any

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

INDICATOR_PREFIX = "ind:"
_MONTHS = 24

# 指标序列参数：基准 / 每月趋势斜率 / 季节振幅 / 噪声振幅 / 保留小数位
_SERIES_SPECS: dict[str, tuple[float, float, float, float, int]] = {
    # ---- A13 科技 ----
    "半导体销售额同比": (12.0, 0.35, 4.0, 1.5, 1),
    "芯片出货量同比": (8.0, 0.45, 3.5, 1.8, 1),
    "科技行业PE(TTM)": (42.0, 0.25, 3.0, 1.2, 1),
    # ---- A14 消费 ----
    "社会消费品零售总额同比": (3.5, 0.08, 1.2, 0.5, 1),
    "白酒批价(元/瓶)": (920.0, -1.5, 18.0, 8.0, 0),
    "消费行业PE(TTM)": (28.0, 0.05, 1.8, 0.8, 1),
    # ---- A15 周期 ----
    "动力煤价格(元/吨)": (700.0, -2.0, 45.0, 15.0, 0),
    "重点电厂煤炭库存(万吨)": (2400.0, 12.0, 180.0, 60.0, 0),
    "周期行业PE(TTM)": (12.0, -0.05, 1.2, 0.5, 1),
    # ---- A16 医药 ----
    "创新药IND申报数量(个)": (52.0, 1.2, 5.0, 3.0, 0),
    "医保集采药品均价同比": (-18.0, 0.6, 4.0, 1.5, 1),
    "医药行业PE(TTM)": (33.0, 0.15, 2.2, 0.9, 1),
}


def _month_iter(end: date, count: int) -> list[str]:
    """返回截至end所在月（含）的最近count个月份，升序YYYY-MM。"""
    total_index = end.year * 12 + (end.month - 1) - count + 1
    months = []
    for i in range(count):
        idx = total_index + i
        months.append(f"{idx // 12:04d}-{idx % 12 + 1:02d}")
    return months


def synthesize_series(name: str, periods: list[str]) -> list[float]:
    """按指标名确定性生成月度序列（同名同序，跨进程可复现）。"""
    base, slope, amp, noise, decimals = _SERIES_SPECS[name]
    # crc32稳定播种：内置hash()对字符串按进程加盐，跨进程不可复现
    rng = random.Random(zlib.crc32(name.encode("utf-8")))
    phase = rng.uniform(0, 2 * math.pi)
    return [
        round(
            base
            + slope * t
            + amp * math.sin(2 * math.pi * t / 12 + phase)
            + rng.uniform(-noise, noise),
            decimals,
        )
        for t in range(len(periods))
    ]


class MockIndustryConnector(BaseConnector):
    """合成行业产业指标（24个月度点），仅响应 ind: 前缀指标。"""

    source_name = "模拟产业数据(Demo)"
    source_url = "mock://industry-demo"

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "simulated": True,
            "indicators": [INDICATOR_PREFIX + n for n in _SERIES_SPECS],
            "notes": "确定性模拟数据，非真实行情；付费产业接口接入后替换本连接器",
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator.startswith(INDICATOR_PREFIX) and (
            indicator[len(INDICATOR_PREFIX):] in _SERIES_SPECS
        )

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        name = indicator[len(INDICATOR_PREFIX):] if indicator.startswith(
            INDICATOR_PREFIX
        ) else indicator
        if name not in _SERIES_SPECS:
            available = ", ".join(_SERIES_SPECS)
            raise DataFetchError(
                f"模拟产业连接器不支持的指标: {indicator}；可用: {available}"
            )
        periods = _month_iter(date.today(), _MONTHS)
        values = synthesize_series(name, periods)
        return [
            DataPoint(
                indicator=indicator,
                value=value,
                period_date=period,
                extra={"simulated": True, "frequency": "monthly"},
                source_name=self.source_name,
                source_url=self.source_url,
                source_type=DataSourceType.API,
                fetch_method=FetchMethod.API_CALL,
                confidence=0.5,
                verified=False,
            )
            for period, value in zip(periods, values, strict=True)
        ]
