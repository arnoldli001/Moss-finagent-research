"""股票池流动性过滤单测（离线，构造已知答案的成交额面板）。

这个模块的输出决定"哪些票、哪些天进入截面"，错了同样不会报错 ——
只会让 IC 悄悄变了样，所以每个性质都单独钉一条。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.quant.liquidity import (
    LiquidityFilter,
    describe_effect,
    liquidity_exclusion,
    select_codes,
)

DATES = [f"2026010{index}" for index in range(1, 6)]      # 5 个交易日
CODES = ["high", "mid", "low", "dead"]


def _amount() -> pd.DataFrame:
    """构造成交额：high 一路 10 亿、mid 1 亿、low 1000 万、dead 全 NaN。"""
    return pd.DataFrame({
        "high": [1e9] * 5,
        "mid": [1e8] * 5,
        "low": [1e7] * 5,
        "dead": [np.nan] * 5,
    }, index=DATES)


# ============== 逐日掩码 ==============

def test_bottom_share_is_excluded_each_day() -> None:
    """每日剔除成交额最差的 1/3（3 只有效票 → 1 只），且是**逐日**判定。

    用 `window=1` 把这个测试限定在"截面排序"这件事上（滚动窗口的行为另有测试）。
    """
    mask = liquidity_exclusion(_amount(), drop_pct=1 / 3, window=1, min_days=1)
    assert mask.shape == (5, 4)
    assert mask["high"].sum() == 0
    assert mask["mid"].sum() == 0
    assert mask["low"].sum() == 5      # 每日都是最差的那只
    assert mask["dead"].sum() == 5     # 无法度量 → 剔除


def test_missing_liquidity_is_excluded() -> None:
    """窗口内没有成交数据 → 剔除（不能证实它可交易）。"""
    mask = liquidity_exclusion(_amount(), drop_pct=1 / 3, window=1, min_days=1)
    assert bool(mask.loc[DATES[0], "dead"]) is True


def test_warmup_days_keep_everyone_instead_of_emptying_the_cross_section() -> None:
    """历史不够的日子**整天放行**，而不是把所有人剔掉。

    触发条件是"滚动均值还没攒够 `min_days` 天"。若任其剔光，那几天的截面为
    空 → IC 变 NaN、分层回测少几期，而现象看起来像"因子早期失效"。
    生产路径上 `build_panels` 会多取 `window` 个前置交易日来避免它，这里是兜底。
    """
    mask = liquidity_exclusion(_amount(), drop_pct=1 / 3, window=3, min_days=3)
    assert not mask.loc[DATES[0]].any(), "第一个交易日不该把全市场剔光"
    assert not mask.loc[DATES[1]].any()
    # 攒够 3 天之后恢复正常剔除
    assert bool(mask.loc[DATES[2], "low"]) is True
    assert bool(mask.loc[DATES[2], "dead"]) is True


def test_rolling_uses_only_past_data() -> None:
    """**不许有未来信息**：当天突然放量的票，只要过去均值仍低就照旧剔除。"""
    frame = pd.DataFrame({
        "was_dead_today_huge": [1e6, 1e6, 1e6, 1e6, 1e12],
        "steady": [1e9] * 5,
    }, index=DATES)
    # 两只票、剔除最差 50% = 剔除当日更差的那只
    mask = liquidity_exclusion(frame, drop_pct=0.5, window=3, min_days=1)
    # 前 4 天它都是更差的一只（100 万 vs 10 亿）→ 剔除
    assert bool(mask.loc[DATES[3], "was_dead_today_huge"]) is True
    # 第 5 天：过去 3 天均值被 1e12 抬高，它反而成了更好的那只 → 不再剔除
    assert bool(mask.loc[DATES[4], "was_dead_today_huge"]) is False


def test_threshold_is_cross_sectional_not_absolute() -> None:
    """阈值是"当日截面的排名分位"，不是绝对金额 —— 牛市/熊市成交额整体差 10 倍，
    绝对阈值会把熊市整个市场都判成不可交易。"""
    small = pd.DataFrame({"a": [1e6] * 3, "b": [2e6] * 3, "c": [3e6] * 3},
                         index=DATES[:3])
    large = small * 1000
    mask_small = liquidity_exclusion(small, drop_pct=1 / 3, window=1, min_days=1)
    mask_large = liquidity_exclusion(large, drop_pct=1 / 3, window=1, min_days=1)
    pd.testing.assert_frame_equal(mask_small, mask_large)


def test_empty_input_is_safe() -> None:
    assert liquidity_exclusion(pd.DataFrame()).empty
    assert liquidity_exclusion(None).empty          # type: ignore[arg-type]


# ============== 常驻列选择 ==============

def test_select_codes_keeps_only_persistent_survivors() -> None:
    """列子集：合格天数占比 ≥ keep_ratio 才装进面板（省内存靠这一步）。"""
    mask = liquidity_exclusion(_amount(), drop_pct=1 / 3, window=1, min_days=1)
    kept = select_codes(mask, keep_ratio=0.5, dates=DATES)
    assert set(kept) == {"high", "mid"}


def test_select_codes_ratio_boundary() -> None:
    # 一只票只在前半段合格 → 占比 50%，按 >= 判定应保留；门槛提到 0.8 就掉队
    mask = pd.DataFrame({"x": [False, False, True, True]},
                        index=DATES[:4])
    assert select_codes(mask, keep_ratio=0.5) == ["x"]
    assert select_codes(mask, keep_ratio=0.8) == []


def test_select_codes_ignores_warmup_days() -> None:
    """掩码里含为算滚动均值而多取的前置交易日时，只按请求区间统计合格率。"""
    mask = pd.DataFrame({"x": [True, True, False, False]},
                        index=DATES[:4])
    # 全区间：合格 2/4 = 50% → 保留；只看后两天：100% → 保留
    assert select_codes(mask, keep_ratio=0.5) == ["x"]
    assert select_codes(mask, keep_ratio=0.5, dates=DATES[2:4]) == ["x"]
    # 只看前两天：0% → 掉队
    assert select_codes(mask, keep_ratio=0.5, dates=DATES[:2]) == []


def test_filter_dataclass_defaults_are_off() -> None:
    """默认必须是关闭的：它会改变截面构成（也就是改变 IC 结果）。"""
    config = LiquidityFilter()
    assert config.enabled is False
    assert "30%" in config.describe()


def test_describe_effect_reports_reduction() -> None:
    mask = liquidity_exclusion(_amount(), drop_pct=1 / 3, window=1, min_days=1)
    text = describe_effect(mask, ["high", "mid"], universe=4, dates=DATES)
    assert "4 只" in text and "2 列" in text and "50.0%" in text
