"""主线挖掘：ETF 资金异动的单元测试。

对应一次真实需求：2026-06~07「农业ETF易方达」562900 多次出现历史级成交额、
场外资金持续申购，但主线模块对农业毫无反应 —— 三个层里没有任何一项在看 ETF。

每条测试钉住一个**具体的失效方式**：

1. **金额单位**：`fund_daily.amount` 是**千元**，落库必须换算成元。不换算的话
   按元设的成交额门槛会高出 1000 倍（实测把 37 只 ETF 里的 34 只全过滤掉），
   而现象只是"没有 ETF 信号"。
2. **份额映射的键**：必须是 `(ts_code, trade_date)` 二元组。早先全市场快照按
   `ts_code` 存、单只回填按 `trade_date` 存，查表一律按 `ts_code` ——
   逐只回填那条路径永远取不到份额，`shares` 全为 NULL，"净申购"整体失效。
3. **两个突破口径**：板块聚合 + 单只。只用聚合口径时，规模最大的 ETF 份额不动
   就会把整个板块摊薄到 0（农业 4 只 ETF 聚合后 +0.0%，而 562900 自己 +6.31%）。
4. **异动分两级**：一级 = 历史级放量，二级 = 一级 + 份额净申购。
   原实现要求"两者同时成立"，把 562900 在 2026-06-29 / 07-01 的放量
   （成交额 2.0~2.2 倍，但份额是**净赎回**）整体丢掉 —— 而那是启动第一周。
5. **映射取最长命中**：`现代农业` 必须优先于 `农业`，否则现代农业主题 ETF
   会被映射到 530 只成分股的申万一级"农林牧渔"，信号被稀释。
6. **迷你 ETF 不算信号**：3000 万规模放量 5 倍也只有 1500 万，噪声大于信息。
7. **加分不占权重**：324 个概念板块里只有 22~35 个能映射到 ETF，
   15% 的权重等于从其余三维身上偷权重。ETF 改为最终分上的加分，
   且**一级就放行**（召回优先），分值由 `etf.bonus_*` 参数标定。
"""

from __future__ import annotations

import pytest

from src.mainline.config import load_config
from src.mainline.etf import (
    EtfMapping,
    board_etf_signals,
    breakout_bonus,
    load_mapping,
    score_etf,
)


def _bars(code: str, name: str, *, days: int = 80, amount: float = 1.0e7,
          shares_series: list[float] | None = None,
          last_amount: float | None = None) -> list[dict]:
    """造一段 ETF 日线（默认成交额平稳、份额不变）。"""
    out: list[dict] = []
    for index in range(days):
        shares = 1000.0
        if shares_series is not None and index < len(shares_series):
            shares = shares_series[index]
        amt = amount
        if last_amount is not None and index == days - 1:
            amt = last_amount
        out.append({"trade_date": f"2026{index + 1:04d}", "close": 1.0,
                    "amount": amt, "shares": shares, "name": name})
    return out


def _mapping() -> EtfMapping:
    # ⚠️ `overrides` 的值必须是 **list**：`EtfMapping` 支持一只 ETF 归多个板块
    # （主题族共用一组 ETF）。写成裸字符串不会报错，而是被逐字符迭代成
    # `农`/`业`/`种`/`植` 四个假板块 —— `matches()` 里另有一层容错挡住它。
    return EtfMapping(
        keywords=[("现代农业", "农业种植"), ("农业", "农林牧渔"),
                  ("养殖", "养殖业")],
        overrides={"562900": ["农业种植"]},
        min_amount=5_000_000.0, min_history=30, loaded=True)


# ==================================================================
# 一、映射
# ==================================================================


def test_mapping_prefers_longest_keyword() -> None:
    """`现代农业` 必须优先于 `农业`（否则信号被 530 只成分股的宽板块稀释）。"""
    mapping = _mapping()
    board, keyword = mapping.match("159999.SZ", "易方达中证现代农业主题ETF")
    assert board == "农业种植"
    assert keyword == "现代农业"
    board2, _ = mapping.match("159888.SZ", "某某中证农业主题ETF")
    assert board2 == "农林牧渔"


def test_mapping_override_wins() -> None:
    mapping = _mapping()
    board, keyword = mapping.match("562900.SH", "易方达中证现代农业主题ETF")
    assert board == "农业种植" and keyword == "override"


def test_mapping_supports_one_etf_to_many_boards() -> None:
    """一只 ETF 可以归**多个**板块（主题族共用一组 ETF）。

    实测用户的 `关联etf.csv`：14 个电池子概念共用同一组 4 只电池 ETF。
    这不是填表错误，所以 `overrides` 的值是 list 而不是 str。

    ⚠️ 但值写成裸字符串时**不会报错** —— 会被逐字符迭代成假板块。
    这个失败模式静默且致命，所以两种写法都要在这里钉住。
    """
    mapping = EtfMapping(
        overrides={"561910": ["BC电池", "HJT电池", "固态电池"]}, loaded=True)
    found = mapping.matches("561910.SH", "招商中证电池主题ETF")
    assert [board for board, _ in found] == ["BC电池", "HJT电池", "固态电池"]
    # 单数接口取第一个（兼容旧调用）
    assert mapping.match("561910.SH", "招商中证电池主题ETF")[0] == "BC电池"
    # 裸字符串也要能正确解析，而不是拆成单字
    sloppy = EtfMapping(overrides={"561910": "BC电池"}, loaded=True)  # type: ignore[dict-item]
    assert sloppy.matches("561910.SH", "x") == [("BC电池", "override")]


def test_shipped_mapping_file_loads() -> None:
    """随包映射表要能加载，且至少给出一种匹配依据（keywords 或 overrides）。

    V2 起改成**显式代码清单**（`overrides`）为主：需求是"每板块最多留 N 只"，
    这是全局约束，关键字匹配表达不了。所以这里不再要求 keywords 非空。
    """
    mapping = load_mapping(load_config(force=True))
    assert mapping.loaded, mapping.gap
    assert mapping.keywords or mapping.overrides, "映射表既无 keywords 也无 overrides"
    assert mapping.min_amount > 0


# ==================================================================
# 二、信号计算
# ==================================================================


def test_breakout_has_two_levels() -> None:
    """一级 = 历史级放量（不要求净申购）；二级 = 一级 + 份额净申购。

    钉住的是一个**逻辑耦合**：旧实现要求"放量 + 净申购同时成立"，等于用
    "份额没净申购"去否定一个真实的放量。两者不等价 —— 放量可能来自折价
    套利承接，也可能份额几天没更新（`fund_share` 在小额申赎时会不动）。

    ⚠️ 不要把这个改动说成"修好了农业 06-29 的漏报"：实测 562900 那天
    成交额只有中位的 1.98 倍，**没到 2.0 门槛**，卡住它的是倍数而不是份额。
    见 `src/mainline/etf.py` 模块头。
    """
    mapping = _mapping()
    byname = {"农业种植": "885812.TI"}
    # 只有放量、份额不变 → 一级
    only_amount = board_etf_signals(
        {"562900.SH": _bars("562900.SH", "现代农业主题ETF",
                            last_amount=5.0e7)},
        mapping, board_by_name=byname)
    level1 = only_amount["885812.TI"]
    assert level1.breakout is True, "放量本身就该触发（一级）"
    assert level1.breakout_level == 1
    assert "一级" in level1.reasons[0]

    # 放量 + 份额增加 → 二级
    shares = [1000.0] * 79 + [1100.0]
    both = board_etf_signals(
        {"562900.SH": _bars("562900.SH", "现代农业主题ETF",
                            last_amount=5.0e7, shares_series=shares)},
        mapping, board_by_name=byname)
    signal = both["885812.TI"]
    assert signal.breakout_level == 2
    assert signal.share_change == pytest.approx(0.1, rel=1e-6)
    assert "二级" in signal.reasons[0]


def test_breakout_requires_amount_too() -> None:
    """只有净申购、没有放量 → 不算异动（避免把日常小额申购当信号）。"""
    mapping = _mapping()
    byname = {"农业种植": "885812.TI"}
    shares = [1000.0] * 79 + [1100.0]
    bars = {"562900.SH": _bars("562900.SH", "现代农业主题ETF",
                               shares_series=shares)}
    signal = board_etf_signals(bars, mapping, board_by_name=byname)["885812.TI"]
    assert signal.share_change == pytest.approx(0.1, rel=1e-6)
    assert signal.breakout_level == 0
    assert signal.breakout is False


def test_single_etf_breakout_survives_aggregation_dilution() -> None:
    """规模大的 ETF 份额不动，会把板块聚合摊薄到 0 —— 单只口径必须仍然报出来。

    2026-07 农业的真实情形：4 只农业 ETF 聚合后份额 +0.0%，
    而易方达 562900 自己份额 +6.31%、成交额 2.17 倍。
    """
    mapping = _mapping()
    byname = {"农业种植": "885812.TI"}
    bars = {
        # 小规模，突破
        "562900.SH": _bars("562900.SH", "现代农业主题ETF",
                           last_amount=2.6e7,
                           shares_series=[1000.0] * 79 + [1063.0]),
        # 大规模，份额不动、成交额平稳 → 把聚合摊薄
        "510000.SH": _bars("510000.SH", "现代农业主题ETF",
                           amount=5.0e8),
        "510001.SH": _bars("510001.SH", "现代农业主题ETF", amount=5.0e8),
    }
    signals = board_etf_signals(bars, mapping, board_by_name=byname)
    signal = signals["885812.TI"]
    assert signal.any_breakout is True, "单只突破必须被识别"
    assert signal.max_etf_level == 2
    assert signal.level_of() == 2, "聚合被摊薄时靠单只口径拿到级别"
    assert signal.breakout_etfs[0][0] == "562900.SH"
    assert "单只 ETF" in " ".join(signal.reasons)


def test_mini_etf_below_amount_floor_is_ignored() -> None:
    """成交额低于门槛的迷你 ETF 不参与（3000 万放量 5 倍也只有 1500 万）。

    门槛是**元**：`ml_etf.amount` 落库时已从 Tushare 的千元换算过来。
    这里用 400 万元，低于默认 500 万元的下限。
    """
    mapping = _mapping()
    byname = {"农业种植": "885812.TI"}
    bars = {"562900.SH": _bars("562900.SH", "现代农业主题ETF",
                               amount=1.0e6, last_amount=4.0e6)}
    assert board_etf_signals(bars, mapping, board_by_name=byname) == {}


def test_short_history_is_ignored() -> None:
    mapping = _mapping()
    byname = {"农业种植": "885812.TI"}
    bars = {"562900.SH": _bars("562900.SH", "现代农业主题ETF", days=10)}
    assert board_etf_signals(bars, mapping, board_by_name=byname) == {}


def test_unmatched_board_name_is_skipped_not_crashed() -> None:
    """映射到的板块名在本地名录里不存在时跳过（而不是抛错或造一个假板块）。"""
    mapping = _mapping()
    bars = {"562900.SH": _bars("562900.SH", "现代农业主题ETF",
                               last_amount=5.0e7)}
    assert board_etf_signals(bars, mapping, board_by_name={}) == {}


def test_share_streak_counts_consecutive_creation() -> None:
    mapping = _mapping()
    byname = {"农业种植": "885812.TI"}
    shares = [1000.0] * 76 + [1010.0, 1020.0, 1030.0, 1040.0]
    bars = {"562900.SH": _bars("562900.SH", "现代农业主题ETF",
                               shares_series=shares)}
    signal = board_etf_signals(bars, mapping, board_by_name=byname)["885812.TI"]
    assert signal.share_streak == 4


# ==================================================================
# 三、打分
# ==================================================================


def test_score_uses_cross_section_and_breakout_floor() -> None:
    """横截面分位打分；突破确认给 75 分下限（绝对口径不该被相对分位压掉）。"""
    mapping = _mapping()
    byname = {"农业种植": "885812.TI", "农林牧渔": "801010.SI",
              "养殖业": "881102.TI"}
    bars: dict[str, list[dict]] = {}
    # 农业种植：突破
    bars["562900.SH"] = _bars("562900.SH", "现代农业主题ETF",
                              last_amount=5.0e7,
                              shares_series=[1000.0] * 79 + [1100.0])
    # 另两只平淡
    bars["510000.SH"] = _bars("510000.SH", "农业主题ETF", amount=2.0e7)
    bars["510001.SH"] = _bars("510001.SH", "养殖产业ETF", amount=1.0e7)
    signals = board_etf_signals(bars, mapping, board_by_name=byname)
    scores = score_etf(signals)
    assert scores["885812.TI"] >= 75.0
    assert scores["801010.SI"] < 75.0
    assert scores["881102.TI"] < 75.0


def test_missing_share_data_is_marked_unavailable_not_zero() -> None:
    """份额缺失时按**一级**算，不按 0 算 —— "没数据"不等于"没申购"。

    这是"无数据 ≠ 0 分"原则在异动级别上的体现：把缺失当反证，就等于用
    `fund_share` 没同步这件事去否定一个真实的放量信号。
    """
    mapping = _mapping()
    byname = {"农业种植": "885812.TI"}
    bars = {"562900.SH": _bars("562900.SH", "现代农业主题ETF",
                               shares_series=[None] * 80,  # type: ignore[list-item]
                               last_amount=5.0e7)}
    signal = board_etf_signals(bars, mapping, board_by_name=byname)["885812.TI"]
    assert signal.share_change is None
    assert signal.breakout_level == 1, "份额缺失只能降级到一级，不能否定放量"
    assert any("份额" in gap for gap in signal.gaps)


# ==================================================================
# 三之二、加分（不占权重）
# ==================================================================


def test_breakout_bonus_by_level_and_cap() -> None:
    """加分按级别给：一级 < 二级；多只二级再加共振；整体封顶。

    分值本身由 `etf.bonus_*` 配置决定（要能调参优化召回/精度），
    这里只钉住**单调性与封顶**这两个结构性性质。
    """
    cfg = load_config(force=True)
    mapping = _mapping()
    byname = {"农业种植": "885812.TI"}

    def bonus_of(bars: dict) -> float:
        signal = board_etf_signals(bars, mapping, board_by_name=byname)["885812.TI"]
        return breakout_bonus(signal, config=cfg)

    # 无异动（放量不动、份额不动）
    calm = bonus_of({"562900.SH": _bars("562900.SH", "现代农业主题ETF")})
    # 一级：只要放量
    level1 = bonus_of({"562900.SH": _bars("562900.SH", "现代农业主题ETF",
                                        last_amount=5.0e7)})
    # 二级：放量 + 净申购
    level2 = bonus_of({"562900.SH": _bars("562900.SH", "现代农业主题ETF",
                                        last_amount=5.0e7,
                                        shares_series=[1000.0] * 79 + [1100.0])})
    assert calm == 0.0
    assert 0.0 < level1 < level2
    assert level2 == pytest.approx(float(cfg.etf.bonus_level2), rel=1e-6)
    assert level2 <= float(cfg.etf.bonus_cap)

    # 无信号时加分为 0（而不是报错）—— 324 个板块里只有 36 个有 ETF 映射
    assert breakout_bonus(None, config=cfg) == 0.0


def test_strength_takes_max_of_aggregate_and_single_etf() -> None:
    """异动强度必须**两个口径取较大者**，与 `level_of` 同一套逻辑。

    钉住一个真实 bug：提名通道原先只用聚合值排序，而聚合会被大 ETF 摊薄 ——
    农业 2026-07-06 的聚合是 1.38 倍 / 65 分位（强度 0.90），
    而 562900 自己 2.17 倍 / 100 分位（强度 2.17）。
    于是"靠单只口径才触发"的板块在提名排序里被排到后面、拿不到名额 ——
    而单只口径存在的理由正是它们。
    """
    mapping = _mapping()
    byname = {"农业种植": "885812.TI"}
    bars = {
        # 小规模，自己放量
        "562900.SH": _bars("562900.SH", "现代农业主题ETF",
                           last_amount=2.6e7,
                           shares_series=[1000.0] * 79 + [1063.0]),
        # 大规模，份额不动、成交额平稳 → 把聚合摊薄
        "510000.SH": _bars("510000.SH", "现代农业主题ETF", amount=5.0e8),
        "510001.SH": _bars("510001.SH", "现代农业主题ETF", amount=5.0e8),
    }
    signal = board_etf_signals(bars, mapping, board_by_name=byname)["885812.TI"]
    aggregate_only = ((signal.amount_ratio or 0.0)
                      * (signal.amount_percentile or 0.0))
    assert signal.best_etf_strength > aggregate_only, "单只口径更强"
    assert signal.strength() == pytest.approx(signal.best_etf_strength)


def test_nomination_channels_have_independent_budgets() -> None:
    """两条提名通道各自独立限流，不能互相饿死。

    钉住 2026-07-06 的实测：共用 `nominate_limit` 时共振当天就占满 8 个名额，
    ETF 通道拿到 0 个 —— 农业种植加了 15 分却一条告警都没有。
    """
    cfg = load_config(force=True)
    assert cfg.alert.nominate_etf_limit > 0, "ETF 通道必须有独立预算"
    # 两条通道各自的预算都取自自己的配置项（不共用 nominate_limit）
    assert cfg.alert.nominate_limit >= cfg.alert.nominate_etf_limit
