"""面板按需加载的字段推导单测（离线，纯静态分析）。

为什么值得单独测：`needs` 算错**不会报错**，只会让某个因子悄悄变成一列 NaN
（IC = None，看起来像"这个因子没用"）。这里的断言分两类：

1. **具体字段**：几个取值方式特殊的因子（走财务、走指数、走资金流、
   用未复权价、用 high/low）逐个点名，防止以后改因子实现时静默漂移；
2. **全体合法性**：35 个因子推导出来的每个字段名都必须是面板真实存在的字段 ——
   这条能抓到"因子代码里写错字段名"（例如 `panels.basic("pe_tttm")`），
   而那种错误在运行时只会表现为"这一列全空"。
"""
from __future__ import annotations

import pytest

from src.quant.factor_library_v2 import FACTORS
from src.quant.panel_needs import (
    PanelNeeds,
    needs_for_factor,
    needs_for_factors,
    needs_for_screening,
)
from src.quant.panels import (
    BAK_FIELDS,
    BASIC_FIELDS,
    FLOW_FIELDS,
    PRICE_SOURCES,
)


# ============== 具体因子的字段 ==============

def test_fundamental_factor_needs_only_fundamentals() -> None:
    needs = needs_for_factor("roe")
    assert needs.fundamentals is True
    assert needs.field_count == 1
    assert not needs.price and not needs.basic


def test_price_factor_needs_close() -> None:
    needs = needs_for_factor("momentum_20")
    assert needs.price == {"close"}
    assert not needs.fundamentals


def test_market_cap_factor_needs_raw_close_and_shares() -> None:
    """自由流通市值 = 自由流通股本 × **未复权**收盘价。

    `close_raw` 必须同时带出 `close`（未复权收盘列就是 daily.close，
    面板里 `close_raw` 是从它派生的）—— 少这一个字段，因子会整列 NaN。
    """
    needs = needs_for_factor("free_float_mv")
    assert needs.basic == {"free_share"}
    assert needs.price == {"close_raw", "close"}


def test_index_factor_needs_index_panel() -> None:
    assert needs_for_factor("relative_strength").index is True


def test_flow_factor_needs_moneyflow_and_amount() -> None:
    needs = needs_for_factor("money_flow_ratio")
    assert needs.flow == {"net_mf_amount"}
    assert needs.price == {"amount"}


def test_volatility_factor_needs_high_low_close() -> None:
    assert needs_for_factor("atr_20").price == {"high", "low", "close"}


def test_helper_function_fields_are_included() -> None:
    """`gross_margin_trend` 的字段访问藏在 `_year_ago(panels, ...)` 里。

    只解析因子函数本身会漏掉它（→ 面板少装字段 → 因子静默变 NaN）。
    这条锁的是"闭包解析"这件事本身。
    """
    needs = needs_for_factor("gross_margin_trend")
    assert needs.fundamentals is True
    assert needs.field_count == 1


# ============== 全体 ==============

def test_no_factor_needs_bak_daily() -> None:
    """实测发现：`bak_daily` 的 11 个字段**一个因子都没用**。

    而它要读 950 万行 —— 全量装配时这笔开销是纯浪费。这条测试防止
    "以后不小心又去读它"，同时也提醒：真要加 bak 因子，这里会失败，
    那时应当同时确认 `PanelNeeds.bak` 会被推导出来（那是自动的）。
    """
    assert needs_for_factors().bak == set()


def test_all_35_factors_are_covered() -> None:
    needs = needs_for_factors()
    assert len(FACTORS) == 35
    # 每个因子都能推出点什么（要么字段，要么表级内容）
    for key in FACTORS:
        single = needs_for_factor(key)
        assert single.field_count >= 1, f"因子 {key} 推导不出任何面板需求"


def test_every_derived_field_exists_in_the_panel() -> None:
    """推导出来的字段名必须真实存在 —— 抓 `panels.basic("pe_tttm")` 这类笔误。"""
    needs = needs_for_factors()
    assert set(needs.price) <= (set(PRICE_SOURCES) | {"close_raw"}), needs.price
    assert set(needs.basic) <= set(BASIC_FIELDS), needs.basic
    assert set(needs.flow) <= set(FLOW_FIELDS), needs.flow
    assert set(needs.bak) <= set(BAK_FIELDS), needs.bak


def test_unknown_factor_raises() -> None:
    with pytest.raises(KeyError):
        needs_for_factor("not_a_factor")


def test_lazy_needs_are_far_smaller_than_everything() -> None:
    """按需比全量小得多 —— 这正是"内存终于够用"的来源。

    断言用一个宽松区间（不是精确值）：具体数字会随因子增减变化，
    但"按需 ≤ 全量的一半"这条性质不该变。
    """
    lazy = needs_for_factors()
    everything = PanelNeeds.everything()
    assert lazy.field_count < everything.field_count * 0.5
    assert len(lazy.datasets) < len(everything.datasets)
    assert "bak_daily" not in lazy.datasets
    assert "stk_limit" not in lazy.datasets
    assert "suspend_d" not in lazy.datasets


# ============== 筛选场景 ==============

def test_screening_needs_include_flow_requirements() -> None:
    """只选一个纯财务因子时，流程自身要用的字段必须被补上。

    - `close`：IC 的前瞻收益；
    - `total_mv`：市值中性化；
    - `amount`：股票池过滤（开了才要）。
    """
    needs = needs_for_screening(["roe"], neutralize_mv=True, liquidity=False)
    assert needs.fundamentals is True
    assert "close" in needs.price
    assert needs.basic == {"total_mv"}

    without_mv = needs_for_screening(["roe"], neutralize_mv=False)
    assert without_mv.basic == set()

    with_liquidity = needs_for_screening(["roe"], neutralize_mv=False,
                                         liquidity=True)
    assert "amount" in with_liquidity.price


def test_screening_needs_cover_every_selected_factor() -> None:
    """选中的每个因子的需求都必须被筛选口径覆盖（否则运行时会抛
    `MissingPanelField`，用户看到的是报错而不是结果）。"""
    keys = ["roe", "relative_strength", "money_flow_ratio", "free_float_mv",
            "atr_20"]
    needs = needs_for_screening(keys, neutralize_mv=True, liquidity=True)
    for key in keys:
        single = needs_for_factor(key)
        assert set(single.price) <= set(needs.price), key
        assert set(single.basic) <= set(needs.basic), key
        assert set(single.flow) <= set(needs.flow), key
        assert set(single.bak) <= set(needs.bak), key
        if single.fundamentals:
            assert needs.fundamentals
        if single.index:
            assert needs.index
