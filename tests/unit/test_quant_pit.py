"""PIT 基本面面板单测（离线，含"对拍"与"未来信息"专项）。

M1 的验收口径：**任何回测日都只能用该日之前已公告的财报**。本文件用三类证据锁死它：
1. 正向：报告期与公告日错位时，按报告期对齐会"提前看到"，本实现不会；
2. 反向：故意构造含未来公告的面板，`validate()`/`as_of` 必须暴露而不是放行；
3. 实现层：慢速可读实现（`as_of`）与快路径（`as_of_panel`/merge_asof）必须逐格一致。
"""
from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd
import pytest

from src.quant.fundamental_source import (
    filter_universe,
    market_of,
    normalize_indicators,
    normalize_yjbb,
    report_periods,
)
from src.quant.pit import (
    FundamentalStore,
    PitConfig,
    PitPanel,
    pit_leak_report,
    summarize_panel,
    to_yyyymmdd,
)

# ==================== 日期归一化 ====================


@pytest.mark.parametrize(("raw", "expected"), [
    ("2026-06-30", "20260630"),
    ("2026/06/30", "20260630"),
    ("20260630", "20260630"),
    (20260630, "20260630"),
    ("20260630.0", "20260630"),
    ("2026-06-30 00:00:00", "20260630"),
    (None, pd.NA),
    ("", pd.NA),
    ("不是日期", pd.NA),
])
def test_to_yyyymmdd_normalizes_shapes(raw, expected) -> None:
    result = to_yyyymmdd(pd.Series([raw])).iloc[0]
    if expected is pd.NA:
        assert pd.isna(result)
    else:
        assert result == expected


# ==================== 构造：公告日不可缺 ====================


def _yjbb_frame() -> pd.DataFrame:
    """模拟东财业绩报表（含最新公告日期）。"""
    return pd.DataFrame({
        "股票代码": ["600519", "000001", "300750"],
        "股票简称": ["贵州茅台", "平安银行", "宁德时代"],
        "每股收益": [30.0, 1.2, 5.5],
        "营业总收入-营业总收入": [1.0e11, 8.0e10, 4.0e11],
        "营业总收入-同比增长": [12.0, 3.0, 25.0],
        "净利润-净利润": [7.0e10, 2.0e10, 3.0e10],
        "净利润-同比增长": [15.0, 2.0, 30.0],
        "每股净资产": [200.0, 20.0, 60.0],
        "净资产收益率": [18.0, 9.0, 22.0],
        "每股经营现金流量": [32.0, 3.0, 8.0],
        "销售毛利率": [91.0, 40.0, 20.0],
        "所处行业": ["白酒", "银行", "电池"],
        "最新公告日期": ["2026-08-28", "2026-08-29", "2026-08-20"],
    })


def test_normalize_yjbb_maps_fields_and_period() -> None:
    out = normalize_yjbb(_yjbb_frame(), "20260630")
    assert set(["code", "name", "report_period", "ann_date", "eps", "roe",
                "industry"]).issubset(out.columns)
    assert out["report_period"].unique().tolist() == ["20260630"]
    assert out["ann_date"].tolist() == ["20260828", "20260829", "20260820"]
    assert out["industry"].tolist() == ["白酒", "银行", "电池"]


def test_normalize_yjbb_drops_rows_without_announcement_date() -> None:
    """没有公告日的行必须被丢掉：按报告期对齐就会引入未来信息。"""
    frame = _yjbb_frame()
    frame.loc[1, "最新公告日期"] = None
    out = normalize_yjbb(frame, "20260630")
    assert out["code"].tolist() == ["600519", "300750"]


def test_normalize_yjbb_rejects_missing_columns() -> None:
    with pytest.raises(ValueError, match="缺少必要列"):
        normalize_yjbb(pd.DataFrame({"股票代码": ["600519"]}), "20260630")


def test_normalize_indicators_has_no_ann_date() -> None:
    """新浪财务指标没有公告日 —— 这一点必须在测试里被显式记住。"""
    frame = pd.DataFrame({
        "日期": ["2026-06-30", "2026-03-31"],
        "净资产收益率(%)": [18.0, 9.0],
        "资产负债率(%)": [20.0, 21.0],
        "销售毛利率(%)": [91.0, 90.0],
    })
    out = normalize_indicators(frame, "600519")
    assert "ann_date" not in out.columns
    assert out["report_period"].tolist() == ["20260630", "20260331"]
    assert out["roe_sina"].tolist() == [18.0, 9.0]
    assert out["debt_to_assets"].tolist() == [20.0, 21.0]


def test_report_periods_sequence() -> None:
    assert report_periods(2025, 2025, as_of="20261231") == [
        "20250331", "20250630", "20250930", "20251231"]
    with pytest.raises(ValueError):
        report_periods(2026, 2025)


def test_report_periods_skips_not_yet_disclosed() -> None:
    """尚未披露的报告期不该去请求（实测会稳定报 akshare 内部 TypeError）。"""
    periods = report_periods(2026, 2026, as_of="20260915", grace_days=15)
    assert periods == ["20260331", "20260630"]
    # 宽限期内的当期也要跳过：9月30日刚结束，首份三季报还没出
    assert "20260930" not in periods


def test_report_periods_grace_window_boundary() -> None:
    assert report_periods(2026, 2026, as_of="20261015", grace_days=15) == [
        "20260331", "20260630", "20260930"]
    assert report_periods(2026, 2026, as_of="20261014", grace_days=15) == [
        "20260331", "20260630"]
    # grace_days=0 表示"报告期一结束就请求"（会拿到空数据）
    assert report_periods(2026, 2026, as_of="20260930", grace_days=0)[-1] == "20260930"


# ==================== 股票池过滤（实测：报表里 54% 是新三板） ====================


@pytest.mark.parametrize(("code", "expected"), [
    ("600519", "sh"), ("601398", "sh"), ("603259", "sh"), ("605499", "sh"),
    ("688981", "sh"), ("689009", "sh"),
    ("000001", "sz"), ("001979", "sz"), ("002594", "sz"), ("003816", "sz"),
    ("300750", "sz"), ("301269", "sz"),
    ("920002", "bse_or_neeq"), ("430047", "bse_or_neeq"), ("830799", "bse_or_neeq"),
    ("872656", "bse_or_neeq"), ("400001", "neeq"),
    ("510300", "fund"), ("159915", "fund"),
])
def test_market_of_classification(code, expected) -> None:
    assert market_of(code) == expected


def test_filter_universe_drops_neeq_but_reports_counts() -> None:
    """业绩报表里混着新三板：默认只留沪深A股，并把各市场行数报出来。"""
    frame = pd.DataFrame({
        "code": ["600519", "300750", "874729", "872656", "920002", "510300"],
        "eps": [30.0, 5.5, 0.76, 0.04, 1.0, 2.0]})
    filtered, counts = filter_universe(frame)
    assert filtered["code"].tolist() == ["600519", "300750"]
    assert counts["sh"] == 1 and counts["sz"] == 1
    assert counts["bse_or_neeq"] == 3 and counts["fund"] == 1
    kept_all, _ = filter_universe(frame, universe="all")
    assert len(kept_all) == 6


def test_filter_universe_rejects_unknown_pool() -> None:
    with pytest.raises(ValueError, match="未知股票池"):
        filter_universe(pd.DataFrame({"code": ["600519"]}), universe="star")


def test_normalize_yjbb_applies_a_share_universe() -> None:
    frame = _yjbb_frame()
    frame.loc[len(frame)] = ["874729", "深达威", 0.76, 1.6e8, 4.0, 3.8e7, 5.4,
                             7.8, 10.1, 0.4, 37.2, "", "2026-09-15"]
    kept = normalize_yjbb(frame, "20260630")
    assert "874729" not in kept["code"].tolist()
    assert len(kept) == 3
    everything = normalize_yjbb(frame, "20260630", universe="all")
    assert len(everything) == 4


# ==================== PIT 核心：不能提前看到 ====================


def _records() -> pd.DataFrame:
    """报告期 20260630 的半年报，公告日分散在 8 月；另有 20260331 一季报。"""
    return pd.DataFrame({
        "code": ["600519", "600519", "000001", "000001", "300750"],
        "name": ["贵州茅台", "贵州茅台", "平安银行", "平安银行", "宁德时代"],
        "report_period": ["20260331", "20260630", "20260331", "20260630", "20260630"],
        "ann_date": ["20260425", "20260828", "20260426", "20260829", "20260820"],
        "eps": [10.0, 30.0, 0.5, 1.2, 5.5],
        "roe": [9.0, 18.0, 4.0, 9.0, 22.0],
    })


def test_as_of_never_uses_future_announcement() -> None:
    """7月1日只能看到一季报；半年报（8月才公告）绝不可见。"""
    panel = PitPanel(_records())
    snapshot = panel.as_of("20260701")
    assert "600519" in snapshot.index
    assert snapshot.loc["600519", "eps"] == pytest.approx(10.0)   # 一季报
    assert snapshot.loc["600519", "roe"] == pytest.approx(9.0)
    # 宁德时代当时还没公告过任何财报
    assert "300750" not in snapshot.index


def test_as_of_uses_latest_announced_after_announcement() -> None:
    panel = PitPanel(_records())
    snapshot = panel.as_of("20260901")
    assert snapshot.loc["600519", "eps"] == pytest.approx(30.0)   # 半年报已生效
    assert snapshot.loc["000001", "eps"] == pytest.approx(1.2)
    assert snapshot.loc["300750", "eps"] == pytest.approx(5.5)


def test_report_period_alignment_would_leak_but_we_do_not() -> None:
    """对拍：如果按报告期对齐（文档 §3.3 的口径），7月1日就会看到半年报。"""
    records = _records()
    leaky = (records[records["report_period"] <= "20260630"]
             .sort_values("report_period")
             .drop_duplicates("code", keep="last")
             .set_index("code")["eps"])
    assert leaky.loc["600519"] == pytest.approx(30.0), "报告期口径确实会提前看到"

    panel = PitPanel(records)
    strict = panel.as_of("20260701")
    assert strict.loc["600519", "eps"] == pytest.approx(10.0)
    assert strict.loc["600519", "eps"] != leaky.loc["600519"]


def test_lag_days_makes_announcement_effective_next_day() -> None:
    """默认滞后 1 天：公告当日不可用（财报多在盘后披露）。"""
    panel = PitPanel(_records(), config=PitConfig(lag_days=1))
    assert "600519" in panel.as_of("20260828").index
    assert panel.as_of("20260828").loc["600519", "eps"] == pytest.approx(10.0)
    assert panel.as_of("20260829").loc["600519", "eps"] == pytest.approx(30.0)

    same_day = PitPanel(_records(), config=PitConfig(lag_days=0))
    assert same_day.as_of("20260828").loc["600519", "eps"] == pytest.approx(30.0)


def test_lag_days_negative_is_harmless_but_flagged() -> None:
    """lag_days 为负等于允许"提前知道"，validate 必须报出来。"""
    panel = PitPanel(_records(), config=PitConfig(lag_days=-5))
    assert "usable_date 早于 ann_date" in " ".join(panel.validate())


def test_revised_announcement_keeps_each_version_visible_at_its_time() -> None:
    """修正公告：不同公告日是**不同版本**，各自在其生效期内可见。"""
    records = pd.DataFrame({
        "code": ["600519", "600519"],
        "name": ["贵州茅台", "贵州茅台"],
        "report_period": ["20260630", "20260630"],
        "ann_date": ["20260828", "20260910"],
        "eps": [30.0, 29.0],
    })
    panel = PitPanel(records)
    assert len(panel) == 2, "两个公告日 = 两个版本，不能被去重掉"
    assert panel.as_of("20260901").loc["600519", "eps"] == pytest.approx(30.0)
    assert panel.as_of("20260911").loc["600519", "eps"] == pytest.approx(29.0)
    # 修正公告之前不能凭空少一只票
    assert "600519" in panel.as_of("20260829").index


def test_duplicate_same_announcement_is_deduped() -> None:
    """同一 (code, 报告期, 公告日) 的重复抓取要合并，否则面板会虚胖。"""
    records = pd.DataFrame({
        "code": ["600519", "600519"],
        "name": ["贵州茅台", "贵州茅台"],
        "report_period": ["20260630", "20260630"],
        "ann_date": ["20260828", "20260828"],
        "eps": [30.0, 30.0],
    })
    assert len(PitPanel(records)) == 1


def test_missing_announcement_rows_are_dropped_at_construction() -> None:
    """缺公告日的那条记录被丢弃后，该标的在公告日之前不可见。"""
    records = _records()
    records.loc[0, "ann_date"] = None          # 600519 的一季报没有公告日
    panel = PitPanel(records)
    assert len(panel) == 4
    assert "600519" not in panel.as_of("20260701").index
    assert "600519" in panel.as_of("20260901").index   # 半年报仍可用


# ==================== 快慢路径对拍 ====================


def test_as_of_panel_matches_as_of_for_every_date() -> None:
    """merge_asof 快路径必须与逐日慢实现对拍一致（快路径错了会静默给错数据）。"""
    records = pd.concat([_records(), _records().assign(
        code=lambda df: df["code"] + "X")], ignore_index=True)
    records["code"] = records["code"].str.slice(0, 6)
    panel = PitPanel(records)
    dates = ["20260401", "20260425", "20260426", "20260701", "20260820",
             "20260828", "20260829", "20260901", "20261231"]
    fast = panel.as_of_panel(dates)
    for date in dates:
        slow_snapshot = panel.as_of(date)
        fast_snapshot = fast.get(date, pd.DataFrame(columns=panel.metrics))
        assert sorted(fast_snapshot.index) == sorted(slow_snapshot.index), date
        if len(slow_snapshot):
            pd.testing.assert_frame_equal(
                fast_snapshot.sort_index()[panel.metrics],
                slow_snapshot.sort_index()[panel.metrics], check_dtype=False)


def test_as_of_panel_handles_empty_and_invalid_dates() -> None:
    panel = PitPanel(_records())
    assert panel.as_of_panel([]) == {}
    result = panel.as_of_panel(["20260701", "不是日期"])
    assert list(result.keys()) == ["20260701"]


def test_as_of_rejects_invalid_date() -> None:
    panel = PitPanel(_records())
    with pytest.raises(ValueError, match="非法日期"):
        panel.as_of("2026-13-99")


# ==================== 覆盖率 / 自检 ====================


def test_coverage_reports_universe_and_gaps() -> None:
    panel = PitPanel(_records())
    report = panel.coverage("20260701")
    assert report["universe"] == 3
    assert report["covered"] == 2      # 宁德时代当时无财报
    assert report["coverage"] == pytest.approx(2 / 3)


def test_rows_with_announcement_before_period_are_dropped() -> None:
    """公告日早于报告期 = 数据错位（等于提前看到未来数据）→ 必须丢弃并计数。

    实测来源：Tushare `fina_indicator_vip` 里确有这种行（603400 报告期 20260630
    却标公告日 20260422）。宁可少一行，也不能让未来信息进因子。
    """
    records = _records()
    records.loc[0, "ann_date"] = "20260101"     # 早于一季报报告期
    panel = PitPanel(records)
    assert panel.dropped_impossible == 1
    assert "20260101" not in panel.records["ann_date"].tolist()
    # 丢弃后数据是干净的，自检不应再报"公告日早于报告期"
    assert not any("公告日早于报告期" in problem for problem in panel.validate())


def test_validate_still_reports_other_structural_problems() -> None:
    """其它结构性问题（如 usable_date 被配置成早于公告日）仍要报出来。"""
    panel = PitPanel(_records(), config=PitConfig(lag_days=-5))
    assert any("usable_date 早于 ann_date" in problem
               for problem in panel.validate())


def test_validate_passes_on_clean_panel() -> None:
    panel = PitPanel(_records())
    assert panel.validate() == []
    summary = summarize_panel(panel)
    assert summary["codes"] == 3
    assert summary["periods"] == 2
    assert summary["lag_days"] == 1
    assert summary["problems"] == []


def test_empty_panel_is_safe() -> None:
    panel = PitPanel(pd.DataFrame(columns=["code", "report_period", "ann_date"]))
    assert len(panel) == 0
    assert panel.as_of("20260701").empty
    assert panel.validate() == ["PIT 面板为空"]


# ==================== 本地缓存 + 增量更新 ====================


def test_store_sync_is_incremental(tmp_dir) -> None:
    """已缓存的报告期不再重复拉取（增量更新的核心语义）。"""
    calls: list[str] = []

    def fake_fetcher(period: str):
        calls.append(period)
        frame = pd.DataFrame({
            "code": ["600519"], "name": ["贵州茅台"],
            "report_period": [period], "ann_date": ["20260828"], "eps": [30.0]})
        return frame, None

    store = FundamentalStore(tmp_dir, fetcher=fake_fetcher)
    periods = ["20260331", "20260630"]
    first = asyncio.run(store.sync(periods))
    assert first.fetched_periods == periods
    assert calls == periods

    second = asyncio.run(store.sync(periods))
    assert second.fetched_periods == []
    assert calls == periods, "第二次同步不应再取数"

    forced = asyncio.run(store.sync(["20260630"], force=True))
    assert forced.fetched_periods == ["20260630"]
    assert calls.count("20260630") == 2


def test_store_roundtrip_preserves_pit_behaviour(tmp_dir) -> None:
    def fake_fetcher(period: str):
        frame = pd.DataFrame({
            "code": ["600519"], "name": ["贵州茅台"],
            "report_period": [period], "ann_date": ["20260828"],
            "eps": [30.0], "roe": [18.0]})
        return frame, None

    store = FundamentalStore(tmp_dir, fetcher=fake_fetcher)
    asyncio.run(store.sync(["20260630"]))
    assert store.cached_periods() == ["20260630"]

    reloaded = FundamentalStore(tmp_dir).load_panel()
    assert len(reloaded) == 1
    assert "600519" not in reloaded.as_of("20260701").index
    assert reloaded.as_of("20260901").loc["600519", "eps"] == pytest.approx(30.0)


def test_store_records_failed_periods(tmp_dir) -> None:
    def failing_fetcher(period: str):
        return pd.DataFrame(), None

    store = FundamentalStore(tmp_dir, fetcher=failing_fetcher)
    info = asyncio.run(store.sync(["20260630"]))
    assert info.failed_periods == ["20260630"]
    assert info.fetched_periods == []
    assert store.load().empty


def test_store_rejects_invalid_period(tmp_dir) -> None:
    store = FundamentalStore(tmp_dir, fetcher=lambda period: (pd.DataFrame(), None))
    with pytest.raises(ValueError, match="非法报告期"):
        asyncio.run(store.sync(["2026-06-30"]))


# ==================== 回测事后自检 ====================


def test_pit_leak_report_clean_on_correct_panel() -> None:
    panel = PitPanel(_records())
    report = pit_leak_report(panel, ["20260701", "20260901"])
    assert report["clean"] is True
    assert report["leaks"] == 0
    assert report["codes_checked"] == 5


def test_numeric_summary_helper() -> None:
    from src.quant.pit import _numeric_summary

    assert _numeric_summary(pd.Series([1.0, 2.0, 3.0]))["median"] == 2.0
    assert _numeric_summary(pd.Series([np.nan]))["count"] == 0
