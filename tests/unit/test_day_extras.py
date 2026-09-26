"""日K补充字段（换手率 / 主力资金净流入）测试。

钉住三件事：
  1. **按日期对齐**地取回来（两张表各自可能缺某些日期，缺就是 None，不填 0）；
  2. 只认收到的代码与区间，且日期两种写法（`YYYY-MM-DD` / `YYYYMMDD`）都吃；
  3. fail-open：仓库缺失/表缺失/该标的没数据 → 空表 + 一条缺口，**绝不影响日K主链路**。
"""

from __future__ import annotations

import sqlite3

import pytest

from src.intraday.day_extras import load_daily_extras


def _warehouse(path) -> str:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE quant_daily_basic ("
                 "code TEXT, trade_date TEXT, turnover_rate REAL)")
    conn.execute("CREATE TABLE quant_moneyflow ("
                 "code TEXT, trade_date TEXT, net_mf_amount REAL)")
    conn.executemany("INSERT INTO quant_daily_basic VALUES (?,?,?)", [
        ("300308", "20260921", 1.8886),
        ("300308", "20260922", 2.1153),
        ("300308", "20260923", None),        # 定稿日尚未生成 → NULL
        ("600036", "20260922", 0.51),        # 别的标的，不该被混进来
    ])
    conn.executemany("INSERT INTO quant_moneyflow VALUES (?,?,?)", [
        ("300308", "20260921", -924522000.0),
        ("300308", "20260922", -2371950100.0),
        # 20260923 没有资金流行 → 该日 net_mf 应为 None
    ])
    conn.commit()
    conn.close()
    return str(path)


def test_loads_both_tables_aligned_by_date(tmp_path) -> None:
    path = _warehouse(tmp_path / "warehouse.db")
    data = load_daily_extras("300308", "2026-09-21", "2026-09-23", warehouse=path)

    assert data["2026-09-21"].turnover == pytest.approx(1.8886)
    assert data["2026-09-21"].net_mf == pytest.approx(-924522000.0)
    assert data["2026-09-22"].turnover == pytest.approx(2.1153)
    assert data["2026-09-22"].net_mf == pytest.approx(-2371950100.0)
    # 换手率那行是 NULL → 跳过（不是 0）；资金流没这天的行 → None
    assert "2026-09-23" not in data or data["2026-09-23"].turnover is None
    assert "600036" not in {key for key in data}          # 别的标的没被混进来
    assert all(day.startswith("2026-09-") for day in data)


def test_accepts_both_date_formats(tmp_path) -> None:
    path = _warehouse(tmp_path / "warehouse.db")
    compact = load_daily_extras("300308", "20260921", "20260922", warehouse=path)
    dashed = load_daily_extras("300308", "2026-09-21", "2026-09-22", warehouse=path)
    assert {k: v.turnover for k, v in compact.items()} == \
           {k: v.turnover for k, v in dashed.items()}


def test_range_filter_excludes_other_days(tmp_path) -> None:
    path = _warehouse(tmp_path / "warehouse.db")
    data = load_daily_extras("300308", "2026-09-22", "2026-09-22", warehouse=path)
    assert list(data) == ["2026-09-22"]


def test_missing_warehouse_is_fail_open(tmp_path) -> None:
    """仓库文件不存在 → 空表（不抛错）：这两个字段是锦上添花，不能拖垮日K。"""
    data = load_daily_extras("300308", "2026-09-01", "2026-09-23",
                             warehouse=str(tmp_path / "nope.db"))
    assert data == {}


def test_empty_code_or_bad_dates_is_empty(tmp_path) -> None:
    path = _warehouse(tmp_path / "warehouse.db")
    assert load_daily_extras("", "2026-09-01", "2026-09-23", warehouse=path) == {}
    assert load_daily_extras("300308", "2026-9", "2026-09-23", warehouse=path) == {}


def test_missing_table_is_fail_open(tmp_path) -> None:
    """仓库在、但表被裁掉（公开仓库的裁剪形态）→ 空表，不抛错。"""
    path = tmp_path / "bare.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE something_else (a INTEGER)")
    conn.commit()
    conn.close()
    assert load_daily_extras("300308", "2026-09-01", "2026-09-23",
                             warehouse=str(path)) == {}
