"""跨模块共享口径的「单一权威」守卫。

## 这个测试防的是什么

项目的真实教训不是「数字太多」，而是**同一个名字在不同模块取了不同的值**：

- `TRADING_DAYS_PER_YEAR`：`quant/model_backtest.py` 是 242（A 股实测），
  `quant/single_backtest.py` 是 252（美股惯例）。同一个指标名两个基数，
  年化收益差约 4%、夏普差约 2%，而两边各自看起来都"有出处"。
- `_JOB_TTL_SECONDS`：`api/routes/quant.py` 是 1800、`backtest.py` 是 900。
- `session_state`：`intraday` 和 `fundflow` 各存一份逐行相同的实现。

这类问题靠读代码发现不了 —— 两个文件各自都自洽。所以把它变成 CI 断言：
**凡是声明为共享口径的名字，从任何模块导入都必须拿到同一个值。**

## 与 `scripts/scan_same_name_conflicts.py` 的分工

扫描脚本给人看（会报出大量同名的**局部**常量，比如各仓储自己的 `TABLE`，
那些是有意为之、不该合并）；本测试给 CI 用，只钉住**明确列为共享**的名字。
"""

from __future__ import annotations

import importlib

import pytest

from src.core import market_constants

#: 共享口径清单：常量名 → 必须与 `core.market_constants` 取值一致的模块。
#: 新增共享常量时在这里登记，测试会保证没有任何模块私自另立一份。
#: 只列**≥2 个来源**的名字 —— 单一来源没有"一致性"可言，
#: 它的取值由下面的数值断言负责（如 `LIMIT_UP_GAP_PCT_GEM`）。
SHARED: dict[str, tuple[str, ...]] = {
    "TRADING_DAYS_PER_YEAR": (
        "src.core.market_constants",
        "src.quant.model_backtest",
        "src.quant.single_backtest",
    ),
    "LIMIT_UP_GAP_PCT_MAIN": (
        "src.core.market_constants",
        "src.auction_select.features",
    ),
}


@pytest.mark.parametrize(("name", "modules"), sorted(SHARED.items()))
def test_shared_constant_has_single_value(name: str, modules: tuple[str, ...]) -> None:
    values = {}
    for module_name in modules:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            # 公开 checkout 不含 .gitignore 里的私有资产（`src/auction_select/` 等），
            # 缺模块时只校验剩下的模块，不把私有资产的存在当成公开仓库的前提。
            continue
        if not hasattr(module, name):
            continue
        values[module_name] = getattr(module, name)
    if len(values) < 2:
        pytest.skip(f"{name} 在本次 checkout 中只有 {len(values)} 个可见来源")
    distinct = {repr(value) for value in values.values()}
    assert len(distinct) == 1, (
        f"{name} 在不同模块取值不一致（同名不同值是维护陷阱）：\n"
        + "\n".join(f"  {mod} = {value!r}" for mod, value in values.items())
    )


def test_trading_days_per_year_is_a_share_convention() -> None:
    """A 股每年约 242 个交易日，不是美股的 252。

    用 252 会让 `years = 交易日 / 252` 偏小 → 年化收益被系统性高估。
    这条断言把口径钉在 A 股一侧，防止有人"顺手改回国际惯例"。
    """
    assert market_constants.TRADING_DAYS_PER_YEAR == 242.0
    assert market_constants.TRADING_DAYS_PER_YEAR != 252


def test_limit_up_gap_tolerance_is_documented_band() -> None:
    """涨停开盘判定留 0.2pct 容差（涨停价按分取整），不是精确的 10/20。"""
    assert market_constants.LIMIT_UP_GAP_PCT_MAIN == 10.0 - 0.2
    assert market_constants.LIMIT_UP_GAP_PCT_GEM == 20.0 - 0.2


def test_limit_pct_by_board() -> None:
    assert market_constants.LIMIT_PCT_MAIN == 0.10
    assert market_constants.LIMIT_PCT_GEM == 0.20
    assert market_constants.LIMIT_PCT_ST == 0.05


def test_auction_cap_threshold_not_merged_with_stats_threshold() -> None:
    """统计口径（25 亿）与选股门槛（2026-09-21 起是 20/150 亿）必须保持不同。

    历史上这两者被混用过一次，`market_constants` 里专门写了「不要合并」。
    这里用一个显式断言把"它们本来就不同"变成可执行事实。

    ⚠️ 选股门槛的**具体数值**不在这里写死（它随用户口径变：25→15→20 亿、
    150→110→150 亿）。这条测试守的是"两者没被合并"，所以只断言
    "统计口径不等于任一选股边界" + "区间本身自洽"。
    """
    # `src/auction_select/` 是 .gitignore 里的私有核心资产，公开 checkout 里不存在；
    # 缺它就 skip 而不是失败（与 `test_auction_golden.py` 同一处置）。
    pytest.importorskip("src.auction_select.config")
    from src.auction_select.config import load_config

    assert market_constants.STATS_LIMIT_UP_MIN_CIRC_MV == 25e8

    universe = load_config().universe
    # 两者都以「元」为单位，数值上必须能区分开：25 亿既不等于下限也不等于上限
    assert market_constants.STATS_LIMIT_UP_MIN_CIRC_MV not in (
        universe.min_market_cap,
        universe.max_market_cap,
    )
    assert universe.min_market_cap < universe.max_market_cap


def test_magnitude_helpers() -> None:
    assert market_constants.YI == 1e8
    assert market_constants.WAN == 1e4
    assert market_constants.PCT == 100.0
