"""面板装配单测（离线，直接写分区缓存后装配）。

重点锁三件事：
1. **复权**：未复权价在除权日会产生假跳空，面板必须用 adj_factor 转成后复权；
2. **单位**：金额元 / 股本股（由 tushare_source 规范化，这里复核到因子输入端）；
3. **缺口不静默**：缺哪个数据集必须出现在 panels.gaps 里。
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.quant.dataset_store import DatasetStore
from src.quant.factor_library_v2 import compute_factors
from src.quant.panels import build_panels

DATES = ["20260911", "20260914", "20260915"]
CODES = ["600519", "300750"]


def _write_daily(root: str, *, close: float, with_adj: bool = True,
                 adj: float = 1.0) -> None:
    daily = DatasetStore("daily", root=root)
    basic = DatasetStore("daily_basic", root=root)
    for date in DATES:
        daily.write(date, pd.DataFrame({
            "code": CODES, "open": [close] * 2, "high": [close * 1.01] * 2,
            "low": [close * 0.99] * 2, "close": [close] * 2,
            "volume_lot": [10000.0] * 2, "amount": [1.0e9] * 2,
        }))
        basic.write(date, pd.DataFrame({
            "code": CODES, "pe_ttm": [20.0, 30.0], "pb": [2.0, 3.0],
            "ps_ttm": [3.0, 4.0], "dv_ttm": [1.5, 0.5],
            "turnover_rate": [0.8, 1.2], "volume_ratio": [1.1, 0.9],
            "free_share": [1.0e8, 2.0e8], "total_mv": [1.0e10, 2.0e10],
            "circ_mv": [8.0e9, 1.6e10],
        }))
    if with_adj:
        adj_store = DatasetStore("adj_factor", root=root)
        for date in DATES:
            adj_store.write(date, pd.DataFrame({
                "code": CODES, "adj_factor": [adj, adj]}))


def test_build_panels_reads_cached_partitions(tmp_dir) -> None:
    _write_daily(tmp_dir, close=100.0)
    panels = build_panels(DATES, root=tmp_dir)
    assert panels.dates == DATES
    assert set(panels.codes) == set(CODES)
    assert panels.price("close").shape == (3, 2)
    assert panels.basic("pe_ttm").iloc[-1]["600519"] == pytest.approx(20.0)


def test_adjustment_removes_fake_gap_on_ex_rights_day(tmp_dir) -> None:
    """除权日：未复权价 −50%，复权后收益率应接近 0（这才是真实持有收益）。"""
    daily = DatasetStore("daily", root=tmp_dir)
    basic = DatasetStore("daily_basic", root=tmp_dir)
    adj_store = DatasetStore("adj_factor", root=tmp_dir)
    # 600519：除权前 100 → 除权后 50（10 送 10），复权因子 1 → 2
    prices = [100.0, 50.0, 50.0]
    factors = [1.0, 2.0, 2.0]
    for date, price, adj in zip(DATES, prices, factors, strict=True):
        daily.write(date, pd.DataFrame({
            "code": CODES, "open": [price] * 2, "high": [price] * 2,
            "low": [price] * 2, "close": [price] * 2,
            "volume_lot": [1e4] * 2, "amount": [1e9] * 2}))
        basic.write(date, pd.DataFrame({
            "code": CODES, "pe_ttm": [20.0, 30.0], "pb": [2.0, 3.0],
            "ps_ttm": [3.0, 4.0], "dv_ttm": [1.0, 1.0],
            "turnover_rate": [1.0, 1.0], "volume_ratio": [1.0, 1.0],
            "free_share": [1e8, 2e8], "total_mv": [1e10, 2e10],
            "circ_mv": [8e9, 1.6e10]}))
        adj_store.write(date, pd.DataFrame({"code": CODES, "adj_factor": [adj, adj]}))

    panels = build_panels(DATES, root=tmp_dir)
    adjusted = panels.price("close")["600519"]
    assert adjusted.iloc[1] / adjusted.iloc[0] - 1 == pytest.approx(0.0, abs=1e-9), \
        "复权后除权日不应有跳空"
    raw = panels.price("close_raw")["600519"]
    assert raw.iloc[1] / raw.iloc[0] - 1 == pytest.approx(-0.5), "原始价确实是 −50%"
    assert -0.5 < adjusted.iloc[1] / adjusted.iloc[0] - 1 < 0.5


def test_missing_adjustment_is_reported_as_gap(tmp_dir) -> None:
    _write_daily(tmp_dir, close=100.0, with_adj=False)
    panels = build_panels(DATES, root=tmp_dir)
    assert any("未复权" in gap for gap in panels.gaps)
    # 回退到未复权价，链路仍可跑（不崩）
    assert panels.price("close").notna().any().any()


def test_free_float_mv_uses_raw_price(tmp_dir) -> None:
    """自由流通市值必须用未复权价 × 股本，不能被复权因子放大。"""
    daily = DatasetStore("daily", root=tmp_dir)
    basic = DatasetStore("daily_basic", root=tmp_dir)
    adj_store = DatasetStore("adj_factor", root=tmp_dir)
    for date in DATES:
        daily.write(date, pd.DataFrame({
            "code": ["600519"], "open": [100.0], "high": [100.0],
            "low": [100.0], "close": [100.0],
            "volume_lot": [1e4], "amount": [1e9]}))
        basic.write(date, pd.DataFrame({
            "code": ["600519"], "free_share": [1e8], "total_mv": [1e10],
            "pe_ttm": [20.0]}))
        adj_store.write(date, pd.DataFrame({
            "code": ["600519"], "adj_factor": [50.0]}))   # 大复权因子
    panels = build_panels(DATES, root=tmp_dir)
    factor = compute_factors(panels, keys=["free_float_mv"])["free_float_mv"]
    value = factor.iloc[-1]["600519"]
    assert value == pytest.approx(-(1e8 * 100.0)), "市值被复权因子污染了"


def test_missing_dataset_leaves_nan_and_gap(tmp_dir) -> None:
    _write_daily(tmp_dir, close=100.0)
    panels = build_panels(DATES, root=tmp_dir)     # 没写 moneyflow
    factors = compute_factors(panels, keys=["money_flow_ratio"])
    assert factors["money_flow_ratio"].isna().all().all()


def test_panels_summary_shape(tmp_dir) -> None:
    _write_daily(tmp_dir, close=100.0)
    panels = build_panels(DATES, root=tmp_dir)
    summary = panels.summary()
    assert summary["dates"] == 3
    assert summary["codes"] == 2
    assert "close" in summary["price_fields"]
    assert "close_raw" in summary["price_fields"]


def test_suspended_cells_are_collected(tmp_dir) -> None:
    _write_daily(tmp_dir, close=100.0)
    suspend = DatasetStore("suspend_d", root=tmp_dir)
    suspend.write("20260914", pd.DataFrame({
        "code": ["600519"], "suspend_type": ["S"]}))
    panels = build_panels(DATES, root=tmp_dir)
    assert panels.is_suspended("20260914", "600519") is True
    assert panels.is_suspended("20260915", "600519") is False


def test_fundamental_panel_prefers_tushare(tmp_dir) -> None:
    """PIT 财务优先用 Tushare fina_indicator_vip（字段全）。"""
    from src.quant.panels import load_fundamental_panel

    _write_daily(tmp_dir, close=100.0)
    store = DatasetStore("fina_indicator_vip", root=tmp_dir)
    store.write("20260630", pd.DataFrame({
        "code": ["600519"], "name": ["贵州茅台"], "report_period": ["20260630"],
        "ann_date": ["20260828"], "end_date": ["20260630"],
        "roe": [18.0], "fcff": [1e10]}))
    panel = load_fundamental_panel(root=tmp_dir)
    assert panel is not None
    assert "fcff" in panel.metrics
    snapshot = panel.as_of("20260901")
    assert snapshot.loc["600519", "roe"] == pytest.approx(18.0)
    # 公告日之前不可见
    assert "600519" not in panel.as_of("20260701").index


# ============== 按需装配（needs） ==============

def test_lazy_panels_load_only_requested_fields(tmp_dir) -> None:
    """给了 needs 就只装这些字段，且进入**严格模式**。"""
    from src.quant.panel_needs import needs_for_factor
    from src.quant.panels import MissingPanelField

    _write_daily(tmp_dir, close=100.0)
    panels = build_panels(DATES, root=tmp_dir,
                          needs=needs_for_factor("momentum_20"))
    assert panels.strict is True
    assert "price:close" in panels.loaded
    assert "price:close_raw" in panels.loaded        # close 的派生列
    assert panels.basics == {} and panels.flows == {}
    assert panels.fundamentals is None               # 纯价格因子不必读财务面板
    with pytest.raises(MissingPanelField) as excinfo:
        panels.basic("pe_ttm")
    message = str(excinfo.value)
    assert "daily_basic.pe_ttm" in message or "basic.pe_ttm" in message
    assert "needs" in message, "报错必须告诉用户怎么修"


def test_eager_panels_stay_permissive(tmp_dir) -> None:
    """不给 needs（= 单票回测的老路径）时不能因为严格模式而炸掉。"""
    _write_daily(tmp_dir, close=100.0)
    panels = build_panels(DATES, root=tmp_dir)
    assert panels.strict is False
    assert panels.basic("不存在的字段").isna().all().all()


def test_lazy_and_eager_produce_identical_factors(tmp_dir) -> None:
    """按需装配与全量装配算出的因子必须**逐位一致**（合成数据版）。

    真实数据上的同款对拍见 `_needs_check.py` 的记录（35 个因子 × 39 个交易日，
    面板/因子/IC 表三层全部逐位一致）。
    """
    from src.quant.panel_needs import needs_for_factors

    _write_daily(tmp_dir, close=100.0)
    keys = ["momentum_20", "ep", "free_float_mv", "amihud"]
    lazy = build_panels(DATES, root=tmp_dir, needs=needs_for_factors(keys))
    eager = build_panels(DATES, root=tmp_dir)
    for key in keys:
        pd.testing.assert_frame_equal(compute_factors(lazy, keys=[key])[key],
                                      compute_factors(eager, keys=[key])[key],
                                      check_dtype=False)


# ============== 股票池过滤 ==============

def _write_liquidity_fixture(root: str) -> None:
    """4 只票、成交额 log 级差；价格相同（只测列裁剪，不测因子）。"""
    codes = ["000001", "000002", "000003", "000004"]
    amounts = [1e9, 1e8, 1e7, 1e6]
    daily = DatasetStore("daily", root=root)
    for date in DATES:
        daily.write(date, pd.DataFrame({
            "code": codes, "open": [10.0] * 4, "high": [10.0] * 4,
            "low": [10.0] * 4, "close": [10.0] * 4,
            "volume_lot": [1e4] * 4, "amount": amounts}))


def test_liquidity_filter_narrows_columns_and_keeps_daily_mask(tmp_dir) -> None:
    from src.quant.liquidity import LiquidityFilter
    from src.quant.panel_needs import needs_for_factors

    _write_liquidity_fixture(tmp_dir)
    # window=1 让这个测试只关心"列裁剪 + 掩码"，不依赖滚动历史
    panels = build_panels(DATES, root=tmp_dir,
                          needs=needs_for_factors(["momentum_20"]),
                          liquidity=LiquidityFilter(enabled=True, drop_pct=0.5,
                                                    window=1, min_days=1,
                                                    keep_ratio=0.5))
    assert len(panels.codes) == 2, f"列没被裁窄：{panels.codes}"
    mask = panels.exclusion_mask()
    assert mask is not None and mask.shape == (3, 2)
    assert not mask.to_numpy().any(), "被裁掉的列不该再出现在掩码里"
    assert any("股票池过滤生效" in gap for gap in panels.gaps)


def test_liquidity_filter_off_by_default(tmp_dir) -> None:
    from src.quant.liquidity import LiquidityFilter

    _write_liquidity_fixture(tmp_dir)
    panels = build_panels(DATES, root=tmp_dir,
                          liquidity=LiquidityFilter())      # enabled=False
    assert len(panels.codes) == 4
    assert panels.exclusion_mask() is None
