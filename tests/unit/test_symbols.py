"""交易所归属规则的单测 —— 钉住一条**曾经三处实现都写错**的规则。

## 为什么这个文件必须有

"代码 → 交易所"这条规则原先散在 5 处，其中 3 处只判断 `6/9` 开头，
漏了沪市 `5` 段（51/56/58 ETF、500/510 基金）。后果是把 588170（科创 50 ETF）
判成深市 → 取数全空，而且**报错信息完全指不到原因**。

`src/core/symbols.py` 现在是唯一实现，本文件钉住它的边界。
"""
from __future__ import annotations

import pytest

from src.core import symbols


@pytest.mark.parametrize(("code", "market"), [
    # 沪市 5 段 —— **这三条是回归重点**，历史上三处实现都漏了
    ("588170", "sh"),      # 科创 50 ETF
    ("510300", "sh"),      # 沪深 300 ETF
    ("560000", "sh"),
    # 沪市 6 / 9 段
    ("600000", "sh"), ("601398", "sh"), ("603288", "sh"),
    ("688981", "sh"),      # 科创板
    ("900901", "sh"),      # B 股
    # 深市 0 / 1 / 2 / 3 段
    ("000001", "sz"), ("002415", "sz"), ("300308", "sz"),
    ("159915", "sz"),      # 深市 ETF
    ("128036", "sz"),      # 可转债
    ("200011", "sz"),
])
def test_market_of(code, market) -> None:
    assert symbols.market_of(code) == market


@pytest.mark.parametrize("code", ["830799", "430047", "920001", "870508"])
def test_bse_is_rejected_loudly(code) -> None:
    """北交所数据源未覆盖 → **显式抛错**，不静默当成深市。"""
    with pytest.raises(symbols.SymbolError, match="北交所"):
        symbols.market_of(code)


@pytest.mark.parametrize("bad", ["12345", "1234567", "abcdef", "", None, "60 0000"])
def test_invalid_code_raises(bad) -> None:
    with pytest.raises(symbols.SymbolError):
        symbols.normalize(bad)


def test_exchange_symbol_styles() -> None:
    assert symbols.exchange_symbol("600000") == "sh600000"
    assert symbols.exchange_symbol("600000", style="qmt") == "600000.SH"
    assert symbols.exchange_symbol("300308", style="qmt") == "300308.SZ"
    assert symbols.exchange_symbol(" 600000 ") == "sh600000"      # 去空白
    assert symbols.exchange_symbol("600000", style="plain") == "600000"
    with pytest.raises(symbols.SymbolError, match="未知 style"):
        symbols.exchange_symbol("600000", style="bloomberg")


@pytest.mark.parametrize(("code", "expected"), [
    ("588170", True), ("510300", True), ("159915", True), ("560000", True),
    ("600000", False), ("300308", False), ("000001", False),
    ("830799", False),      # 北交所：非法代码 → 返回 False 而不是抛
])
def test_is_etf_code(code, expected) -> None:
    assert symbols.is_etf_code(code) is expected


def test_connectors_delegate_to_the_single_source() -> None:
    """**反重复实现**：连接器与做T的符号函数必须委托给 core.symbols。

    这条断言防的是"有人觉得顺手，又在连接器里写一遍 startswith(('6','9'))"。
    """
    from src.infrastructure.connectors import (  # noqa: F401
        akshare_connector,
        tencent_daily_connector,
        xtquant_connector,
    )
    from src.intraday import sources

    for module in (akshare_connector, tencent_daily_connector,
                   xtquant_connector, sources):
        src = module.__loader__.get_source(module.__name__)  # type: ignore[union-attr]
        assert "symbols.exchange_symbol" in src or "symbols.market_of" in src
        # 旧写法（缺 5 段）不得复活
        assert 'startswith(("6", "9"))' not in src
