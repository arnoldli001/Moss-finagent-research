"""AkShare连接器测试：df转换纯函数用真实pandas小样本；不联网。"""

import pandas as pd
import pytest

from src.core.exceptions import DataFetchError
from src.infrastructure.connectors.akshare_connector import (
    AkshareConnector,
    df_to_data_points,
)


def _sample_macro_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "月份": ["2026-08", "2026-07"],
            "全国同比": [2.1, 2.0],
            "全国环比": [0.3, 0.1],
        }
    )


def test_df_to_data_points_row_per_point():
    points = df_to_data_points(_sample_macro_df(), "CPI", "AkShare", "https://x")

    assert len(points) == 2
    assert points[0].indicator == "CPI"
    assert points[0].value == 2.1  # 优先"同比"列
    assert points[0].period_date == "2026-08"
    assert points[0].source_name == "AkShare"
    assert points[0].confidence == 0.8
    assert points[0].extra["全国环比"] == 0.3
    assert len(points[0].raw_content_hash) == 64


def test_df_to_data_points_empty():
    assert df_to_data_points(pd.DataFrame(), "CPI", "s", "u") == []


def test_stock_df_picks_close_column():
    df = pd.DataFrame({"日期": ["2026-09-10"], "开盘": [10.0], "收盘": [10.5]})
    points = df_to_data_points(df, "stock_close:000001", "AkShare", "https://x")
    assert points[0].value == 10.5  # 优先"收盘"列


def test_macro_report_format_picks_jinzhi_column():
    """2026版akshare宏观接口结构：商品/日期/今值/预测值/前值。"""
    df = pd.DataFrame(
        {
            "商品": ["中国CPI月率报告"],
            "日期": ["2026-08-01"],
            "今值": [2.1],
            "预测值": [2.0],
            "前值": [1.9],
        }
    )
    points = df_to_data_points(df, "CPI", "AkShare", "https://x")
    assert points[0].value == 2.1  # "今值"关键词命中，而非文本列"商品"
    assert points[0].period_date == "2026-08-01"


def test_unsupported_indicator_raises():
    connector = AkshareConnector()
    with pytest.raises(DataFetchError):
        connector._load_dataframe("GDP", None, None)


def test_capabilities():
    caps = AkshareConnector().get_capabilities()
    assert caps["name"] == "AkShare"
    assert "CPI" in caps["indicators"]
