"""新增三因子测试：指数量能 / 板块涨幅排行 / 海外映射。

这三个维度都是**市场环境类**因子（日内恒定），因此：
  - 打分核与实时/重放共用（`day_level_constants`）；
  - 数据源是腾讯指数快照 + 指数日线（量能）、同花顺板块快照（排名）、
    腾讯海外行情（美股 us* / 韩股 kr*）。

关键口径锁定：
  - 指数量能：score = 方向 × (0.5 + 0.5×量能强度) —— 量能只放大/衰减方向，
    **不单独给方向**（避免把"放量"本身当利好/利空）；
  - 板块排行："10/390" → 分位 = 1-(名次-1)/(总数-1)；
  - 海外映射：美股隔夜 0.75 + 韩股盘中同步 0.25（韩股与A股同时开市，非隔夜）。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.core.exceptions import ConfigError
from src.intraday.config import (
    BoardRankParams,
    IndexVolumeParams,
    IntradayConfig,
    OverseasParams,
    WatchConfig,
    Weights,
)
from src.intraday.factors import (
    FactorContext,
    board_rank_score_from,
    index_volume_score_from,
    overseas_score_from,
    run_factor,
)
from src.intraday.features import day_level_constants
from src.intraday.market import (
    IndexVolume,
    OverseasQuote,
    _parse_rank,
    board_index_for,
    elapsed_session_ratio,
    parse_tencent_overseas_line,
    score_index_volume,
    score_overseas,
)

# ==================== 权重：14因子合计100 ====================

def test_fourteen_factors_sum_to_100() -> None:
    """分时做T的十四个因子权重合计必须=100（总分刻度与±20/±30阈值依赖它）。"""
    weights = IntradayConfig().weights.as_dict()
    assert len(weights) == 14
    assert sum(weights.values()) == pytest.approx(100.0)
    # 箱体仍为最高权重（原始设计意图不变）
    assert max(weights, key=lambda k: weights[k]) == "box"
    # 第二批（市场环境三因子）合计 14
    market = weights["index_volume"] + weights["board_rank"] + weights["overseas"]
    assert market == pytest.approx(14.0)
    # 第三批（技能库四因子）合计 29
    skill = weights["chan"] + weights["chip"] + weights["cycle"] + weights["character"]
    assert skill == pytest.approx(29.0)
    # 原十因子被等比缩放到 71，刻度不变
    assert sum(weights.values()) - market - skill == pytest.approx(57.0)


def test_daily_weights_sum_to_100() -> None:
    """日线做T的七个因子是**另一套**权重，同样必须合计=100。"""
    weights = IntradayConfig().daily_weights.as_dict()
    assert len(weights) == 7
    assert sum(weights.values()) == pytest.approx(100.0)
    assert set(weights) == {"trend", "chan_daily", "volume", "position",
                            "signal_rule", "cycle", "character"}


def test_legacy_template_restores_old_ten_factor_scale() -> None:
    """`legacy` 模板必须能一键回到本次改动前的十因子口径（技能因子置0）。"""
    from src.intraday.weight_profiles import template

    legacy = template("legacy", "intraday")
    assert legacy is not None
    weights = legacy.normalized()
    assert weights["box"] == pytest.approx(24.0)
    assert weights["vwap"] == pytest.approx(16.0)
    for key in ("chan", "chip", "cycle", "character"):
        assert weights[key] == 0.0
    assert sum(weights.values()) == pytest.approx(100.0)


def test_weights_still_validated() -> None:
    with pytest.raises(ConfigError, match="权重合计"):
        Weights(box=30, vwap=20, boll=15, macd=10, kdj_rsi=10,
                sentiment=10, news=5, index_volume=8, board_rank=6,
                overseas=6)  # 缺新增四项，且合计不是100


# ==================== ① 指数量能 ====================

def test_board_index_mapping() -> None:
    """个股代码 → 所属指数（上证/创业板/科创50/深证成指）。"""
    assert board_index_for("600036") == ("000001", "上证指数")
    assert board_index_for("601088") == ("000001", "上证指数")
    assert board_index_for("300308") == ("399006", "创业板指")
    assert board_index_for("301150") == ("399006", "创业板指")
    assert board_index_for("688702") == ("000688", "科创50")
    assert board_index_for("000001") == ("399001", "深证成指")
    assert board_index_for("002463") == ("399001", "深证成指")


@pytest.mark.parametrize("moment,expected", [
    (datetime(2026, 9, 15, 9, 0), 0.0),      # 盘前
    (datetime(2026, 9, 15, 9, 30), 0.0),     # 开盘瞬间
    (datetime(2026, 9, 15, 10, 30), 0.25),   # 上午走了一半
    (datetime(2026, 9, 15, 11, 30), 0.5),    # 上午收盘
    (datetime(2026, 9, 15, 12, 30), 0.5),    # 午休
    (datetime(2026, 9, 15, 14, 0), 0.75),    # 下午走了一半
    (datetime(2026, 9, 15, 15, 0), 1.0),     # 收盘
    (datetime(2026, 9, 15, 16, 0), 1.0),     # 盘后
    (datetime(2026, 9, 12, 10, 30), 0.0),    # 周六
])
def test_elapsed_session_ratio(moment: datetime, expected: float) -> None:
    """已交易时间占比：用于把盘中累计量折算成全天预测量。"""
    assert elapsed_session_ratio(moment) == pytest.approx(expected, abs=0.01)


def test_index_volume_scoring_quadrants() -> None:
    """量价配合四象限：放量涨/缩量涨/放量跌/缩量跌。"""
    params = IndexVolumeParams(change_scale_pct=1.0, volume_scale=0.5)
    # 放量上涨（+1% 且量能 +50%）→ +1
    assert index_volume_score_from(1.0, 1.5, params) == pytest.approx(1.0)
    # 缩量上涨 → 0（上涨无量=见顶）
    assert index_volume_score_from(1.0, 0.5, params) == pytest.approx(0.0)
    # 放量下跌 → -1（放量杀跌）
    assert index_volume_score_from(-1.0, 1.5, params) == pytest.approx(-1.0)
    # 缩量下跌 → 0（卖压不足）
    assert index_volume_score_from(-1.0, 0.5, params) == pytest.approx(0.0)
    # 方向中性 → 无论量能都接近 0
    assert abs(index_volume_score_from(0.0, 2.0, params)) < 1e-6


def test_index_volume_score_degrades_without_ratio() -> None:
    """量能比缺失时按方向打折（×0.75），仍可参与打分。"""
    params = IndexVolumeParams()
    assert index_volume_score_from(1.0, None, params) == pytest.approx(0.75)
    assert index_volume_score_from(None, 1.5, params) is None


def test_score_index_volume_reports_gap_when_index_missing() -> None:
    outcome = run_factor(
        "index_volume",
        FactorContext(price=10.0, index_change_pct=None, index_gap="指数缺失"),
        IntradayConfig().factors)
    assert outcome.available is False
    assert outcome.gap == "指数缺失"


def test_score_index_volume_detail_shows_ratio() -> None:
    outcome = run_factor(
        "index_volume",
        FactorContext(price=10.0, index_change_pct=-0.54,
                      index_volume_ratio=0.96, index_name="上证指数",
                      index_code="000001"),
        IntradayConfig().factors)
    assert outcome.available is True
    assert "上证指数" in outcome.detail
    assert "0.96" in outcome.detail
    assert outcome.inputs["volume_ratio"] == pytest.approx(0.96)


def test_index_volume_snapshot_carries_verdict_text() -> None:
    """快照里的指数量能必须带结论文案（前端顶栏直接用，不能只给一个倍数）。"""
    _, verdict = score_index_volume(change_pct=-0.54, volume_ratio=0.96)
    assert "缩量下跌" in verdict
    _, rising = score_index_volume(change_pct=1.2, volume_ratio=1.6)
    assert "放量上涨" in rising
    snapshot = IndexVolume(
        code="000001", name="上证指数", available=True, change_pct=-0.54,
        volume_ratio=0.96, verdict=verdict)
    assert snapshot.to_dict()["verdict"] == verdict


# ==================== ② 板块涨幅排行 ====================

def test_parse_rank() -> None:
    assert _parse_rank("10/390") == (10, 390)
    assert _parse_rank("1/390") == (1, 390)
    assert _parse_rank("") is None
    assert _parse_rank("abc") is None
    assert _parse_rank("0/390") is None
    assert _parse_rank("5/1") is None


def test_board_rank_scoring_top_and_bottom() -> None:
    params = BoardRankParams(rank_weight=0.6, change_weight=0.4)
    # 排名第1（分位1.0 → rank_score +1）+ 板块涨2%（满分）→ 接近 +1
    top = board_rank_score_from("1/390", 2.0, params)
    assert top is not None and top > 0.9
    # 排名最后（分位0 → -1）+ 板块跌2% → 接近 -1
    bottom = board_rank_score_from("390/390", -2.0, params)
    assert bottom is not None and bottom < -0.9
    # 排名居中 → 接近 0
    middle = board_rank_score_from("195/390", 0.0, params)
    assert middle is not None and abs(middle) < 0.15


def test_board_rank_rank_only_and_change_only() -> None:
    params = BoardRankParams()
    # 只有排名
    only_rank = board_rank_score_from("1/390", None, params)
    assert only_rank is not None and only_rank > 0
    # 只有涨跌幅
    only_change = board_rank_score_from(None, 2.0, params)
    assert only_change is not None and only_change > 0
    # 都没有
    assert board_rank_score_from(None, None, params) is None


def test_score_board_rank_reports_percentile() -> None:
    outcome = run_factor(
        "board_rank",
        FactorContext(price=10.0, board_name="PCB概念", board_rank="10/390",
                      board_change_pct=0.58),
        IntradayConfig().factors)
    assert outcome.available is True
    # (10-1)/(390-1) = 0.0231 → 分位 ≈ 0.977
    assert outcome.inputs["rank_percentile"] == pytest.approx(0.977, abs=0.005)
    assert "PCB概念" in outcome.detail
    assert "排名居前" in outcome.detail


def test_score_board_rank_gap_when_both_missing() -> None:
    outcome = run_factor(
        "board_rank",
        FactorContext(price=10.0, board_rank_gap="该板块快照未提供涨幅排名"),
        IntradayConfig().factors)
    assert outcome.available is False
    assert "涨幅排名" in (outcome.gap or "")


# ==================== ③ 海外映射 ====================

def _quote(symbol: str, name: str, market: str, pct: float,
           available: bool = True) -> OverseasQuote:
    return OverseasQuote(symbol=symbol, name=name, market=market,
                         change_pct=pct, available=available)


def test_overseas_scoring_weights_overnight_and_intraday() -> None:
    """美股隔夜权重0.75、韩股盘中权重0.25：同向时加权平均，反向时对冲。"""
    params = OverseasParams(scale_pct=3.0, overnight_weight=0.75,
                            intraday_weight=0.25)
    # 美股 -3%，韩股 -3% → 都到满分 -1
    assert overseas_score_from(-3.0, -3.0, params) == pytest.approx(-1.0)
    # 美股 +3%（+1），韩股 -3%（-1）→ 0.75-0.25 = +0.5
    assert overseas_score_from(3.0, -3.0, params) == pytest.approx(0.5)
    # 只有美股
    assert overseas_score_from(1.5, None, params) == pytest.approx(0.5)
    # 都没有
    assert overseas_score_from(None, None, params) is None


def test_score_overseas_aggregates_by_market() -> None:
    quotes = [
        _quote("usNVDA", "英伟达", "us", -3.36),
        _quote("usMU", "美光科技", "us", -5.25),
        _quote("kr000660", "SK hynix Inc.", "kr", -0.41),
    ]
    snapshot = score_overseas(quotes, OverseasParams())
    assert snapshot.available is True
    # 美股加权均值（NVDA 1.5 / MU 1.0）应为负
    assert snapshot.overnight_avg_pct is not None
    assert snapshot.overnight_avg_pct < 0
    assert snapshot.intraday_avg_pct == pytest.approx(-0.41)
    assert snapshot.score is not None and snapshot.score < 0
    assert "英伟达" not in snapshot.verdict  # 汇总结论不含逐条名称（清单里给）
    assert "SK hynix" in snapshot.verdict


def test_score_overseas_marks_unavailable_quotes() -> None:
    quotes = [
        _quote("usNVDA", "英伟达", "us", -3.36),
        _quote("usGLW", "康宁", "us", 0.0, available=False),
    ]
    snapshot = score_overseas(quotes, OverseasParams())
    assert snapshot.available is True
    assert any("康宁" in gap for gap in (snapshot.gaps or []))


def test_score_overseas_unavailable_when_none_usable() -> None:
    snapshot = score_overseas(
        [_quote("usNVDA", "英伟达", "us", 0.0, available=False)],
        OverseasParams())
    assert snapshot.available is False
    assert "不可用" in snapshot.verdict


def test_parse_tencent_overseas_line_us() -> None:
    """腾讯美股行情行解析（字段布局与A股不同：时间[30]、涨跌幅[32]）。"""
    parts = ["200", "英伟达", "NVDA.OQ", "210.96", "218.29", "211.24",
             "132267244"] + [""] * 23
    parts += ["2026-09-14 16:00:01", "-7.33", "-3.36", "212.77", "208.93", "USD"]
    line = f'v_usNVDA="{"~".join(parts)}"'
    quote = parse_tencent_overseas_line(line, "us")
    assert quote is not None
    assert quote.symbol == "usNVDA"
    assert quote.name == "英伟达"
    assert quote.market == "us"
    assert quote.change_pct == pytest.approx(-3.36)
    assert quote.price == pytest.approx(210.96)
    assert quote.available is True


def test_parse_tencent_overseas_line_kr() -> None:
    parts = ["352", "SK hynix Inc.", "000660.KS", "1690000", "1697000",
             "1690000", "2693899"] + [""] * 23
    parts += ["2026-09-15 14:30:22", "-7000", "-0.41249", "1729000",
              "1671000"]
    line = f'v_kr000660="{"~".join(parts)}"'
    quote = parse_tencent_overseas_line(line, "kr")
    assert quote is not None
    assert quote.symbol == "kr000660"
    assert quote.market == "kr"
    assert quote.change_pct == pytest.approx(-0.41249, abs=1e-4)


def test_parse_tencent_overseas_line_rejects_bad() -> None:
    assert parse_tencent_overseas_line("", "us") is None
    assert parse_tencent_overseas_line('v_usX="1~2"', "us") is None


def test_score_overseas_gap_when_no_mapping_configured() -> None:
    outcome = run_factor(
        "overseas",
        FactorContext(price=10.0, overseas_available=None,
                      overseas_gap="该标的未配置海外映射"),
        IntradayConfig().factors)
    assert outcome.available is False
    assert "海外映射" in (outcome.gap or "")


# ==================== 配置：海外映射代码校验 ====================

def test_watch_overseas_codes_validated() -> None:
    item = WatchConfig(code="300308", overseas=["usNVDA", "kr000660"])
    assert item.overseas == ["usNVDA", "kr000660"]
    with pytest.raises(ConfigError, match="us\\*/kr\\*"):
        WatchConfig(code="300308", overseas=["NVDA"])
    with pytest.raises(ConfigError, match="us\\*/kr\\*"):
        WatchConfig(code="300308", overseas=["600110"])


def test_board_overseas_codes_validated() -> None:
    from src.intraday.config import BoardConfig

    board = BoardConfig(name="PCB概念", overseas=["usNVDA"])
    assert board.overseas == ["usNVDA"]
    with pytest.raises(ConfigError, match="us\\*/kr\\*"):
        BoardConfig(name="PCB概念", overseas=["300308"])


def test_overseas_for_prefers_stock_then_board_then_gap() -> None:
    """个股配置 → 板块默认映射 → 空（不得随便套用别的板块）。

    实测踩过的坑：不在自选池的股票曾回落到「全部配置板块」的第一块映射，
    于是招商银行被映射到英伟达 —— 毫无意义的组合静默进了打分。
    """
    from src.intraday.config import BoardConfig, IntradayConfig

    config = IntradayConfig(
        boards=[BoardConfig(name="PCB概念",
                            overseas=["usNVDA", "usTSM"])],
        watchlist=[
            WatchConfig(code="300308", boards=["PCB概念"]),          # 无个股映射
            WatchConfig(code="600110", boards=["PCB概念"],
                        overseas=["usGLW"]),                          # 有个股映射
        ])
    # ① 个股配置优先
    assert config.overseas_for("600110") == ["usGLW"]
    # ② 回落到已声明板块的默认映射
    assert config.overseas_for("300308") == ["usNVDA", "usTSM"]
    # ③ 不在自选池 → 不猜，返回空（前端显示缺口）
    assert config.overseas_for("600036") == []
    assert config.overseas_for("999999") == []


def test_exchange_symbol_handles_etf_ranges() -> None:
    """ETF 代码段必须正确归属（588xxx 沪市科创50ETF，159xxx 深市）。

    实测踩过的坑：早期只判断 6/9 开头，588170 被判成深市 → 取数全空。
    """
    from src.intraday.sources import exchange_symbol

    assert exchange_symbol("588170") == "sh588170"
    assert exchange_symbol("510300") == "sh510300"
    assert exchange_symbol("159915") == "sz159915"
    assert exchange_symbol("300308") == "sz300308"
    assert exchange_symbol("688702") == "sh688702"
    assert exchange_symbol("600036") == "sh600036"
    assert exchange_symbol("000001") == "sz000001"
    assert exchange_symbol("002463") == "sz002463"
    assert exchange_symbol("588170", style="qmt") == "588170.SH"


def test_board_index_etf_mapping() -> None:
    """ETF 指数量能归属：588科创板→科创50；15/16深市ETF→深证成指；51沪市→上证指数。"""
    assert board_index_for("588170") == ("000688", "科创50")
    assert board_index_for("588000") == ("000688", "科创50")
    assert board_index_for("159915") == ("399001", "深证成指")
    assert board_index_for("161725") == ("399001", "深证成指")
    assert board_index_for("510300") == ("000001", "上证指数")
    assert board_index_for("512760") == ("000001", "上证指数")
    assert board_index_for("562500") == ("000001", "上证指数")
    # 个股板块口径不受ETF规则影响
    assert board_index_for("300308") == ("399006", "创业板指")
    assert board_index_for("600036") == ("000001", "上证指数")


def test_is_etf_code_and_close_indicator() -> None:
    """ETF代码识别与日线指标前缀分流（做T日K必须走etf_close）。"""
    from src.intraday.sources import close_indicator, is_etf_code

    assert is_etf_code("588170")
    assert is_etf_code("510300")
    assert is_etf_code("562500")
    assert is_etf_code("159915")
    assert is_etf_code("161725")
    assert not is_etf_code("600519")
    assert not is_etf_code("300308")
    assert not is_etf_code("000001")
    assert close_indicator("588170") == "etf_close:588170"
    assert close_indicator("562500") == "etf_close:562500"
    assert close_indicator("159915") == "etf_close:159915"
    assert close_indicator("600519") == "stock_close:600519"


def test_repo_config_overseas_capability() -> None:
    """海外映射的**能力**必须在配置里就绪（默认权重表 + 字段支持）。

    注意：不断言某只自选股具体映射了谁 —— 自选池由用户在前端维护，
    断言用户数据会让测试随用户操作而失败（实测踩过）。
    """
    from src.intraday.config import load_intraday_config, reset_config_cache

    reset_config_cache()
    config = load_intraday_config("configs/intraday.yaml", force=True)
    assert config.load_error is None
    # 默认权重表覆盖需求里点名的海外标的
    weights = config.factors.overseas.weights
    for symbol in ("usNVDA", "usMU", "usTSM", "usASX", "usGOOGL", "usINTC",
                   "usORCL", "usGLW", "kr000660"):
        assert symbol in weights, symbol
    # 英伟达权重最高（AI 算力链总龙头）
    assert weights["usNVDA"] == max(weights.values())
    # 任何已配置的映射都必须是合法的 us*/kr* 代码
    for item in config.watchlist:
        for symbol in item.overseas:
            assert symbol.startswith(("us", "kr")), symbol


# ==================== 与重放/实时同源 ====================

def test_day_level_constants_include_new_factors() -> None:
    """新增三因子必须进入 day_level_constants（重放与实时共用同一批打分核）。"""
    config = IntradayConfig()
    constants = day_level_constants(
        box_position=0.3, breadth=0.6, relative_strength_pct=1.0,
        news_llm_score=0.5, news_positive=3, news_negative=1, news_count=4,
        config=config, index_change_pct=-1.0, index_volume_ratio=1.5,
        board_rank="10/390", board_change_pct=0.58,
        overseas_overnight_pct=-3.0, overseas_intraday_pct=-0.4,
        overseas_available=True)
    for key in ("box", "sentiment", "news", "index_volume", "board_rank",
                "overseas"):
        assert key in constants, key
    # 指数放量下跌 → -1（放量杀跌）
    assert constants["index_volume"] == pytest.approx(-1.0)
    # 板块排名居前 → 正分
    assert constants["board_rank"] is not None and constants["board_rank"] > 0
    # 海外普跌 → 负分
    assert constants["overseas"] is not None and constants["overseas"] < 0


def test_day_level_constants_new_factors_none_when_data_missing() -> None:
    config = IntradayConfig()
    constants = day_level_constants(
        box_position=0.3, breadth=None, relative_strength_pct=None,
        news_llm_score=None, news_positive=0, news_negative=0, news_count=0,
        config=config)
    assert constants["index_volume"] is None
    assert constants["board_rank"] is None
    assert constants["overseas"] is None


def test_scorecard_lists_fourteen_factors() -> None:
    """打分卡必须输出14行（前端表格逐行相加=总分）。"""
    from src.intraday.engine import compose_scorecard

    card = compose_scorecard(FactorContext(price=10.0), IntradayConfig())
    assert len(card.factors) == 14
    labels = [f.label for f in card.factors]
    for label in ("指数量能", "板块涨幅排行", "海外映射",
                  "缠论结构", "筹码量能结构", "市场情绪周期", "股性适配"):
        assert label in labels
    assert card.weights_sum == pytest.approx(100.0)


def test_scorecard_sums_exactly_with_fourteen_factors() -> None:
    """14因子下「逐行贡献分相加 == 总分」仍然精确成立。"""
    from src.intraday.engine import compose_scorecard

    ctx = FactorContext(
        price=10.0, vwap=10.2, dev_z=-1.0, dev_pct=-0.2,
        box_high=11.0, box_low=9.0, box_position=0.3, box_span_days=20,
        pct_b=0.3, bandwidth=0.02, bandwidth_pctl=0.5,
        breadth=0.6, up_count=18, down_count=12,
        stock_change_pct=0.5, board_change_pct=0.2,
        news_positive=3, news_negative=1, news_count=4, news_llm_score=0.5,
        index_change_pct=-0.54, index_volume_ratio=0.96, index_name="上证指数",
        board_rank="10/390",
        overseas_overnight_pct=-3.36, overseas_intraday_pct=-0.4,
        overseas_available=True)
    card = compose_scorecard(ctx, IntradayConfig())
    assert round(sum(f.contribution for f in card.factors), 2) == card.total
    # 本用例未提供分钟级 MACD/KDJ 序列与四项技能因子 → 那六个因子按缺口扣除
    # （100 - macd6 - kdj_rsi6 - chan12 - chip8 - cycle6 - character3 = 59）
    assert card.available_weight == pytest.approx(59.0)
    assert len(card.gaps) == 6


# ==================== 服务层装配（回归：二元组解包） ====================
#
# 实测踩过的坑：_fetch_index_volume / _fetch_overseas 返回 (对象, 尝试记录) 二元组，
# 服务层却直接把元组当对象用（hasattr(tuple, "to_dict") 恒为假），
# 导致指数量能与海外映射**静默变成 None**、两个因子永远报缺口；
# 而单元测试只测因子本身，漏掉了这个装配错误。下面固定该接口形状。


def test_service_unpacks_index_volume_and_overseas(monkeypatch) -> None:
    import asyncio

    from src.intraday.market import IndexVolume, OverseasQuote, score_overseas
    from src.intraday.models import SourceAttempt
    from src.intraday.service import IntradayService

    service = IntradayService(backend=None, gateway=None, news_fetcher=None)

    async def fake_index_volume(code, config):
        return (IndexVolume(
            code="399006", name="创业板指", available=True,
            change_pct=-1.15, volume=137216832.0, yesterday_volume=137734087.0,
            projected_volume=137216832.0, volume_ratio=0.996,
            elapsed_ratio=1.0), [SourceAttempt(source="腾讯行情", ok=True)])

    async def fake_overseas(watch, config, code=""):
        quotes = [
            OverseasQuote(symbol="usNVDA", name="英伟达", market="us",
                          change_pct=-3.36, available=True),
            OverseasQuote(symbol="kr000660", name="SK hynix Inc.", market="kr",
                          change_pct=-0.41, available=True),
        ]
        return score_overseas(quotes, config.factors.overseas), [
            SourceAttempt(source="腾讯行情", ok=True)]

    monkeypatch.setattr(service, "_fetch_index_volume", fake_index_volume)
    monkeypatch.setattr(service, "_fetch_overseas", fake_overseas)

    async def empty_frame(*args, **kwargs):
        from src.core.exceptions import DataFetchError
        raise DataFetchError("测试：无分钟数据")

    monkeypatch.setattr(service._data, "fetch_bars", empty_frame)  # noqa: SLF001
    monkeypatch.setattr(service._data, "fetch_trend", empty_frame)  # noqa: SLF001
    monkeypatch.setattr(service._data, "fetch_quote", empty_frame)  # noqa: SLF001

    # 其余子链路也必须打桩：本用例只验证**装配层解包**，不该触网。
    # 尤其同花顺板块快照底层走 py_mini_racer 算 JS 加密 cookie，
    # 在 Windows + Python 3.12 上会直接崩掉整个进程（实测 0x80000003），
    # 绝不能出现在单元测试里。
    async def no_board_snapshot(*args, **kwargs):
        return None

    async def no_board_series(*args, **kwargs):
        return None

    monkeypatch.setattr(service._board, "fetch_snapshot", no_board_snapshot)
    monkeypatch.setattr(service._board, "fetch_series", no_board_series)
    monkeypatch.setattr(service._board, "fetch_index", no_board_series)

    async def no_daily(*args, **kwargs):
        from src.core.exceptions import DataFetchError
        raise DataFetchError("测试：无日线")

    monkeypatch.setattr(service, "_fetch_daily_bars", no_daily)

    async def no_news(**kwargs):
        from src.intraday.models import NewsSentiment
        return NewsSentiment(available=False, gap="测试：不打桩新闻")

    monkeypatch.setattr(service._sentiment, "analyze", no_news)  # noqa: SLF001

    async def no_valuation(*args, **kwargs):
        from src.intraday.models import ValuationSpace
        return ValuationSpace(available=False, code="300308")

    monkeypatch.setattr(service._valuation, "fetch", no_valuation)  # noqa: SLF001

    snapshot = asyncio.run(service.snapshot("300308"))

    # 关键断言：必须是解包后的对象/字典，而不是元组
    assert snapshot.index_volume is not None, "指数量能被静默丢弃"
    assert isinstance(snapshot.index_volume, dict)
    assert snapshot.index_volume["name"] == "创业板指"
    assert snapshot.index_volume["volume_ratio"] == pytest.approx(0.996)

    assert snapshot.overseas is not None, "海外映射被静默丢弃"
    assert isinstance(snapshot.overseas, dict)
    assert snapshot.overseas["available"] is True
    assert snapshot.overseas["score"] is not None
    assert len(snapshot.overseas["quotes"]) == 2
    assert {q["symbol"] for q in snapshot.overseas["quotes"]} == {
        "usNVDA", "kr000660"}

    # 子链路的尝试记录应并入健康度（便于排查数据源）
    sources = {a.source for a in snapshot.health.attempts}
    assert "腾讯行情" in sources


def test_unpack_helper_handles_all_shapes() -> None:
    from src.intraday.models import SourceAttempt
    from src.intraday.service import _unpack

    attempt = SourceAttempt(source="x", ok=True)
    assert _unpack(("value", [attempt])) == ("value", [attempt])
    assert _unpack(("value", None)) == ("value", [])
    assert _unpack(None) == (None, [])
    assert _unpack("scalar") == ("scalar", [])
    # 长度不为2的元组不应被误当作二元组
    assert _unpack(("a", "b", "c")) == (("a", "b", "c"), [])
