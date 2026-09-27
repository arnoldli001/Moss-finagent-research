"""ST 状态模块单测（离线，构造对照样本 → 断言期望值）。

为什么每条都值得写：这个模块的输出决定"哪一天哪些票进入截面"。
它**错了不会报错**，只会让 IC/回测悄悄变了样 —— 与项目里
"毛利率同比恒为 0"属于同一类风险（比崩溃危险得多）。
"""
from __future__ import annotations

import random

import pandas as pd

from src.quant.st_status import StStatus, _norm_code, _norm_day, is_st_name


def _frame(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["code", "name", "start_date", "end_date"])


# ============== 名称判定 ==============

def test_st_name_prefix_rules() -> None:
    # 五类风险警示前缀都要命中
    assert is_st_name("ST舍得")
    assert is_st_name("*ST海航")
    assert is_st_name("SST前锋")
    assert is_st_name("S*ST前锋")
    assert is_st_name("S ST前锋")
    assert is_st_name("st康美")          # 大小写不敏感
    # 正常名称不能误判（这是限定"前 4 个字符"的原因）
    assert not is_st_name("贵州茅台")
    assert not is_st_name("TCL科技")
    assert not is_st_name("东旭光电")
    assert not is_st_name("")
    assert not is_st_name(None)


def test_normalizers() -> None:
    assert _norm_day("2006-05-25") == "20060525"
    assert _norm_day("20060525") == "20060525"
    assert _norm_day(float("nan")) is None
    assert _norm_day(None) is None
    assert _norm_day("") is None
    assert _norm_code("600519.SH") == "600519"
    assert _norm_code(1) == "000001"      # 数字列会被 pandas 读成 int
    assert _norm_code(None) is None


# ============== 区间判定 ==============

def test_interval_is_closed_and_missing_end_means_current() -> None:
    status = StStatus.from_frame(_frame([
        ("000001", "平安银行", "19910403", "20060524"),
        ("000001", "ST平安", "20060525", "20061008"),
        ("000001", "平安银行", "20061009", None),
    ]))
    assert not status.is_st("000001", "20060524")   # 生效前一天
    assert status.is_st("000001", "20060525")       # 生效首日（闭区间左端）
    assert status.is_st("000001", "20061008")       # 闭区间右端
    assert not status.is_st("000001", "20061009")   # 摘帽当天
    assert not status.is_st("000001", "20260924")   # end 缺失 = 仍在生效


def test_unknown_code_or_day_is_not_st() -> None:
    status = StStatus.from_frame(_frame([
        ("000001", "ST平安", "20060525", None)]))
    assert not status.is_st("600519", "20060601")   # 表里没有这只票
    assert not status.is_st("000001", "20010101")   # 区间之外
    assert not status.is_st("000001", None)         # 未知日期
    assert not status.is_st(None, "20060601")
    assert not StStatus.from_frame(None).is_st("000001", "20060601")


def test_rows_without_code_or_start_are_skipped() -> None:
    status = StStatus.from_frame(pd.DataFrame([
        {"code": None, "name": "ST坏行", "start_date": "20100101", "end_date": None},
        {"code": "000002", "name": "ST缺日", "start_date": None, "end_date": None},
        {"code": "000003", "name": "ST好行", "start_date": "20100101", "end_date": None},
    ]))
    assert status.available and status.code_count == 1
    assert status.is_st("000003", "20150601")
    assert not status.is_st("000002", "20150601")


def test_ts_code_column_is_accepted() -> None:
    frame = pd.DataFrame([{"ts_code": "600519.SH", "name": "*ST茅台",
                           "start_date": "20100101", "end_date": "20101231"}])
    assert StStatus.from_frame(frame).is_st("600519", "20100601")


# ============== 掩码（批量路径必须与逐格判定一致） ==============

def test_mask_matches_is_st_cell_by_cell() -> None:
    status = StStatus.from_frame(_frame([
        ("000001", "ST一号", "20100101", "20101231"),
        ("000002", "二号", "20100101", None),
        ("000002", "ST二号", "20150101", "20151231"),
        ("000003", "*ST三号", "20110101", None),
    ]))
    dates = [f"{year}{month:02d}{day:02d}"
             for year in (2009, 2010, 2011, 2015)
             for month in (1, 6, 12) for day in (1, 15, 31)]
    codes = ["000001", "000002", "000003", "000004"]
    grid = status.mask(dates, codes)
    for day in dates:
        for code in codes:
            assert bool(grid.loc[day, code]) == status.is_st(code, day), (day, code)


def test_mask_handles_large_grid_without_per_cell_calls() -> None:
    """向量化路径的正确性抽查：造 5000 天 × 600 只，逐格验证会被拖死，
    这里对随机抽样点比对（覆盖区间端点附近）。"""
    spans = []
    for index in range(60):
        code = f"{index:06d}"
        spans.append((code, "正常名", "20060101", "20100630"))
        spans.append((code, "ST" + f"{index:02d}", "20100701",
                      None if index % 2 else "20150630"))
    status = StStatus.from_frame(_frame(spans))
    dates = [f"{2006 + index // 240}{index // 20 % 12 + 1:02d}{index % 28 + 1:02d}"
             for index in range(5000)]
    codes = [f"{index:06d}" for index in range(600)]
    grid = status.mask(dates, codes)
    assert grid.shape == (5000, 600)
    rng = random.Random(20260925)
    for _ in range(300):
        day = rng.choice(dates)
        code = rng.choice(codes)
        assert bool(grid.loc[day, code]) == status.is_st(code, day)
    # 边界两侧也要比对（ST 生效首日 / 摘帽当天）
    for code in codes[:5]:
        assert status.is_st(code, "20100701")
        assert not status.is_st(code, "20100630")


def test_later_span_wins_when_intervals_overlap() -> None:
    """区间重叠时以"最近一次改名"为准。

    正常数据里各段首尾相接、不会重叠；但 Tushare 偶有 `end_date` 缺失的
    旧行。这条规则是**被单测逼出来的**：原实现里 `is_st` 取"第一个匹配"、
    `mask` 用 `|=` 全量置位，两者在重叠数据上会给出相反答案
    （实测断言失败：`mask=True` 而 `is_st=False`）。"""
    status = StStatus.from_frame(_frame([
        ("000002", "二号", "20100101", None),        # end 缺失的旧行
        ("000002", "ST二号", "20150101", "20151231"),
    ]))
    assert status.is_st("000002", "20150601")        # 落在更晚的那一段里
    assert not status.is_st("000002", "20130601")    # 只有旧行覆盖
    assert not status.is_st("000002", "20160601")    # 后段已结束 → 回到旧行
    grid = status.mask(["20130601", "20150601", "20160601"], ["000002"])
    assert grid["000002"].tolist() == [False, True, False]


def test_describe_and_stock_days() -> None:
    status = StStatus.from_frame(_frame([
        ("000001", "ST一号", "20100101", "20101231"),
        ("000002", "正常", "20100101", None),
    ]))
    assert status.st_codes == {"000001"}
    assert "2 只票" in status.describe()
    assert status.st_stock_days(["20100101", "20100601", "20101231"],
                               ["000001", "000002"]) == 3
    empty = StStatus({})
    assert not empty.available
    assert "未下载" in empty.describe()
