"""做T模块 · 编排层单元测试（会话时段、板块解析、数据源映射、推送门控、日线归一）。"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from src.core.exceptions import ConfigError, DataFetchError
from src.core.schemas import DataPoint
from src.intraday.config import IntradayConfig, load_intraday_config, reset_config_cache
from src.intraday.sentiment import (
    build_news_block,
    parse_sentiment_payload,
    polarities_from_payload,
)
from src.intraday.service import (
    IntradayService,
    _finite,
    _quote_from_bars,
    _relative_strength,
    session_state,
)
from src.intraday.sources import (
    daily_bars_from_points,
    exchange_symbol,
    index_symbol,
)

# ==================== 交易时段 ====================


@pytest.mark.parametrize("moment,expected", [
    (datetime(2026, 9, 15, 8, 0), "pre_open"),
    (datetime(2026, 9, 15, 9, 20), "call_auction"),
    (datetime(2026, 9, 15, 9, 30), "trading"),
    (datetime(2026, 9, 15, 11, 30), "trading"),
    (datetime(2026, 9, 15, 12, 0), "lunch_break"),
    (datetime(2026, 9, 15, 14, 0), "trading"),
    (datetime(2026, 9, 15, 15, 0), "trading"),
    (datetime(2026, 9, 15, 16, 0), "closed"),
])
def test_session_state_boundaries(moment: datetime, expected: str) -> None:
    state, label = session_state(moment)
    assert state == expected
    assert label


def test_session_state_weekend_is_closed() -> None:
    # 2026-09-12 是周六
    state, label = session_state(datetime(2026, 9, 12, 10, 0))
    assert state == "closed"
    assert "周末" in label


# ==================== 数据源代码映射 ====================


@pytest.mark.parametrize("code,expected", [
    ("600519", "sh600519"), ("601088", "sh601088"), ("688981", "sh688981"),
    ("000001", "sz000001"), ("300308", "sz300308"), ("002463", "sz002463"),
])
def test_exchange_symbol_mapping(code: str, expected: str) -> None:
    assert exchange_symbol(code) == expected


def test_exchange_symbol_qmt_style() -> None:
    assert exchange_symbol("600519", style="qmt") == "600519.SH"
    assert exchange_symbol("300308", style="qmt") == "300308.SZ"


def test_exchange_symbol_rejects_invalid() -> None:
    with pytest.raises(DataFetchError):
        exchange_symbol("12345")
    with pytest.raises(DataFetchError):
        exchange_symbol("830799")  # 北交所


@pytest.mark.parametrize("code,expected", [
    ("000001", "sh000001"), ("000300", "sh000300"), ("399006", "sz399006"),
])
def test_index_symbol_mapping(code: str, expected: str) -> None:
    """指数代码单独映射：避免 000001 在指数与平安银行之间歧义。"""
    assert index_symbol(code) == expected


# ==================== 日线归一（复用项目采集链的两种 extra 口径） ====================


def test_daily_bars_from_points_qmt_schema() -> None:
    """QMT/本地CSV 口径：extra 用英文键。"""
    points = [
        DataPoint(indicator="stock_close:300308", value=10.2,
                  period_date="2026-09-14",
                  extra={"open": 10.0, "high": 10.5, "low": 9.9,
                         "close": 10.2, "volume": 1000, "amount": 10200}),
        DataPoint(indicator="stock_close:300308", value=10.5,
                  period_date="2026-09-15",
                  extra={"open": 10.2, "high": 10.8, "low": 10.1,
                         "close": 10.5, "volume": 1200, "amount": 12600}),
    ]
    frame = daily_bars_from_points(points)
    assert len(frame) == 2
    assert list(frame["ts"]) == ["2026-09-14", "2026-09-15"]
    assert frame["high"].iloc[-1] == pytest.approx(10.8)


def test_daily_bars_from_points_akshare_schema() -> None:
    """AkShare 口径：extra 用中文列名，value 兜底为收盘价。"""
    points = [
        DataPoint(indicator="stock_close:300308", value=10.2,
                  period_date="2026-09-15",
                  extra={"开盘": 10.0, "最高": 10.5, "最低": 9.9,
                         "收盘": 10.2, "成交量": 1000, "成交额": 10200}),
    ]
    frame = daily_bars_from_points(points)
    assert len(frame) == 1
    assert frame["open"].iloc[0] == pytest.approx(10.0)
    assert frame["close"].iloc[0] == pytest.approx(10.2)


def test_daily_bars_from_points_missing_ohlc_falls_back_to_close() -> None:
    points = [DataPoint(indicator="stock_close:300308", value=10.2,
                        period_date="2026-09-15", extra={})]
    frame = daily_bars_from_points(points)
    assert frame["open"].iloc[0] == pytest.approx(10.2)
    assert frame["low"].iloc[0] == pytest.approx(10.2)


def test_daily_bars_from_points_empty() -> None:
    assert daily_bars_from_points([]).empty
    assert daily_bars_from_points(None).empty


# ==================== 行情兜底与相对强度 ====================


def test_quote_from_bars_marks_previous_close() -> None:
    bars = pd.DataFrame({
        "ts": ["2026-09-14 15:00", "2026-09-15 09:35", "2026-09-15 15:00"],
        "open": [9.8, 10.0, 10.2], "high": [10.0, 10.3, 10.6],
        "low": [9.7, 9.9, 10.1], "close": [9.9, 10.1, 10.5],
        "volume": [100.0, 100.0, 100.0], "amount": [990.0, 1010.0, 1050.0],
    })
    quote = _quote_from_bars("300308", bars)
    assert quote is not None
    assert quote.price == pytest.approx(10.5)
    assert quote.prev_close == pytest.approx(9.9)
    assert quote.change_pct == pytest.approx((10.5 / 9.9 - 1) * 100, abs=0.01)


def test_quote_from_bars_empty() -> None:
    assert _quote_from_bars("300308", pd.DataFrame()) is None


def test_relative_strength_requires_both_sides() -> None:
    class _Q:
        change_pct = 1.2

    class _B:
        available = True
        change_pct = 0.5

    assert _relative_strength(_Q(), [_B()]) == pytest.approx(0.7)
    assert _relative_strength(None, [_B()]) is None
    assert _relative_strength(_Q(), []) is None


def test_finite_helper() -> None:
    assert _finite(None) is None
    assert _finite(float("nan")) is None
    assert _finite(float("inf")) is None
    assert _finite("3.5") == pytest.approx(3.5)
    assert _finite("abc") is None


# ==================== 板块解析 ====================


def test_parse_concept_info_reads_breadth_and_change() -> None:
    from src.intraday.board import parse_concept_info

    frame = pd.DataFrame({
        "项目": ["今开", "昨收", "最低", "最高", "成交量(万手)", "板块涨幅",
                 "涨幅排名", "涨跌家数", "资金净流入(亿)", "成交额(亿)"],
        "值": ["2664.21", "2673.50", "2664.21", "2735.44", "7428.70", "0.58%",
               "10/390", "119/115", "-40.87", "2627.10"],
    })
    parsed = parse_concept_info(frame)
    assert parsed["change_pct"] == pytest.approx(0.58)
    assert parsed["up_count"] == 119
    assert parsed["down_count"] == 115
    assert parsed["breadth"] == pytest.approx(119 / 234)
    assert parsed["rank"] == "10/390"
    assert parsed["amount"] == pytest.approx(2627.10)
    assert parsed["net_inflow"] == pytest.approx(-40.87)


def test_parse_concept_info_tolerates_bad_shape() -> None:
    from src.intraday.board import parse_concept_info

    assert parse_concept_info(None) == {}
    assert parse_concept_info(pd.DataFrame({"a": [1]})) == {}


# ==================== 板块名解析（用户写法 → 数据源官方名） ====================

# 同花顺官方概念板块名（真实子集，取自 stock_board_concept_name_ths）
THS_NAMES = [
    "PCB概念", "PET铜箔", "共封装光学(CPO)", "先进封装", "AI PC", "AI手机",
    "光伏概念", "锂电池", "存储芯片", "光刻机", "液冷服务器",
]


@pytest.mark.parametrize(("query", "expected"), [
    ("PCB概念", "PCB概念"),                    # 完全一致
    ("PCB", "PCB概念"),                        # 去后缀后一致
    ("CPO概念", "共封装光学(CPO)"),            # ASCII 片段唯一命中（实测场景）
    ("cpo", "共封装光学(CPO)"),                # 大小写不敏感
    ("PET铜箔", "PET铜箔"),
    ("铜箔", "PET铜箔"),                       # 唯一包含候选 → 采纳（实测全表仅1个）
    ("封装", None),                            # 两个候选（共封装光学/先进封装）→ 不猜
    ("量子计算", None),                        # 完全不存在
])
def test_match_board_name_resolves_vendor_naming(query, expected) -> None:
    """用户写的板块名要能对上数据源官方名（实测：CPO概念 → 共封装光学(CPO)）。

    直接拿用户写法去查同花顺会 IndexError，退到新浪（仅175个粗板块）也必然失败，
    于是板块情绪/板块排行两个维度静默变成不可用。
    """
    from src.intraday.board import match_board_name

    assert match_board_name(query, THS_NAMES) == expected


def test_match_board_name_never_guesses_when_ambiguous() -> None:
    """多个候选同样匹配时必须返回 None —— 宁可不给分，也不能给错板块。"""
    from src.intraday.board import match_board_name

    names = ["AI概念", "AI手机", "AI PC"]
    assert match_board_name("AI", names) is None
    assert match_board_name("AI手机", names) == "AI手机"
    # 用真实同花顺全表口径复核：「封装」有 2 个候选 → 不猜
    assert match_board_name("封装", THS_NAMES) is None


def test_match_board_name_handles_empty_input() -> None:
    from src.intraday.board import match_board_name

    assert match_board_name("CPO概念", []) is None
    assert match_board_name("", THS_NAMES) is None


# ==================== 消息面解析 ====================


def test_parse_sentiment_payload_accepts_valid() -> None:
    payload = {"score": -0.6, "positive": 1, "negative": 4, "neutral": 5,
               "summary": "利空主导"}
    score, pos, neg, neu, summary, note = parse_sentiment_payload(payload)
    assert score == pytest.approx(-0.6)
    assert (pos, neg, neu) == (1, 4, 5)
    assert summary == "利空主导"
    assert note is None


def test_parse_sentiment_payload_rejects_out_of_range() -> None:
    """越界分数不静默钳制，而是丢弃并降级（与项目既有 ScoreOutOfRange 约定一致）。"""
    score, _, _, _, _, note = parse_sentiment_payload({"score": 3.5})
    assert score is None
    assert note and "越界" in note


def test_parse_sentiment_payload_derives_counts_from_items() -> None:
    payload = {"score": 0.2, "items": [
        {"i": 0, "polarity": "positive"}, {"i": 1, "polarity": "negative"},
        {"i": 2, "polarity": "neutral"}]}
    _, pos, neg, neu, _, _ = parse_sentiment_payload(payload)
    assert (pos, neg, neu) == (1, 1, 1)


def test_polarities_from_payload_defaults_neutral() -> None:
    payload = {"items": [{"i": 1, "polarity": "positive"}]}
    assert polarities_from_payload(payload, 3) == ["neutral", "positive", "neutral"]


def test_build_news_block_is_bounded() -> None:
    items = [{"title": "标题" * 200, "text": "正文" * 200,
              "publish_time": "2026-09-15 10:00", "source_name": "东财"}] * 3
    block = build_news_block(items, limit=2)
    assert block.count("[") == 2
    assert len(block) < 2000


# ==================== 推送门控 ====================


def test_should_push_only_solid_by_default() -> None:
    from src.intraday.models import TradeSignal
    from src.intraday.notifier import SignalNotifier

    notifier = SignalNotifier(IntradayConfig())
    notifier._pending_code = "300308"  # noqa: SLF001 测试直接设定冷却键前缀

    hollow = TradeSignal(kind="low_buy", strength="hollow", triggered=True,
                         price=10.0, ts="2026-09-15 10:00", total_score=25.0,
                         reason="")
    ok, reason = notifier.should_push(hollow)
    assert ok is False
    assert "仅站内展示" in reason

    solid = TradeSignal(kind="low_buy", strength="solid", triggered=True,
                        price=10.0, ts="2026-09-15 10:00", total_score=35.0,
                        reason="")
    ok, _ = notifier.should_push(solid)
    assert ok is True


def test_should_push_respects_cooldown() -> None:
    from src.intraday.models import TradeSignal
    from src.intraday.notifier import SignalNotifier

    config = IntradayConfig()
    config.notify.cooldown_minutes = 30
    notifier = SignalNotifier(config)
    notifier._pending_code = "300308"  # noqa: SLF001
    signal = TradeSignal(kind="low_buy", strength="solid", triggered=True,
                         price=10.0, ts="2026-09-15 10:00", total_score=35.0,
                         reason="")
    assert notifier.should_push(signal, now=1000.0)[0] is True
    notifier.mark_pushed(signal, now=1000.0)
    ok, reason = notifier.should_push(signal, now=1100.0)
    assert ok is False
    assert "冷却" in reason
    # 冷却窗口过后可再次推送
    assert notifier.should_push(signal, now=1000.0 + 30 * 60 + 1)[0] is True


def test_should_push_ignores_untriggered() -> None:
    from src.intraday.models import TradeSignal
    from src.intraday.notifier import SignalNotifier

    notifier = SignalNotifier(IntradayConfig())
    notifier._pending_code = "300308"  # noqa: SLF001
    none_signal = TradeSignal(kind="none", strength="none", triggered=False,
                              price=10.0, ts="", total_score=0.0, reason="")
    ok, reason = notifier.should_push(none_signal)
    assert ok is False
    assert "无信号" in reason


def test_channel_status_never_leaks_webhook() -> None:
    from src.intraday.notifier import SignalNotifier

    status = SignalNotifier(IntradayConfig()).channel_status()
    serialized = str(status)
    assert "https://" not in serialized
    assert {c["name"] for c in status["channels"]} == {"飞书", "钉钉", "企业微信"}


def test_render_includes_levels_and_disclaimer() -> None:
    from src.intraday.models import (
        IntradaySnapshot,
        LevelSet,
        ScoreCard,
        TradeSignal,
    )
    from src.intraday.notifier import SignalNotifier

    snapshot = IntradaySnapshot(
        code="300308", name="中际旭创",
        levels=LevelSet(price=10.0, box_high=11.0, box_low=9.0,
                        box_position=0.5, box_span_days=20, low_buy=9.5,
                        high_sell=11.0, stop_loss=9.4, stop_loss_pct=1.0),
        scorecard=ScoreCard(
            total=35.0, threshold_action=30.0, threshold_hint=20.0,
            zone="strong_buy_zone", verdict="", factors=[], weights_sum=100.0),
        disclaimer="仅供技术研究")
    signal = TradeSignal(kind="low_buy", strength="solid", triggered=True,
                         price=9.5, ts="2026-09-15 10:00", total_score=35.0,
                         reason="价格触及低吸线")
    title, text = SignalNotifier.render(snapshot, signal)
    assert "中际旭创" in title
    assert "低吸" in title
    assert "止损 9.40" in text
    assert "仅供技术研究" in text


# ==================== 配置加载 ====================


def test_load_config_from_yaml_and_hot_reload(tmp_dir) -> None:
    import os

    path = os.path.join(tmp_dir, "intraday.yaml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "weights: {box: 21, vwap: 11, boll: 8, macd: 6, kdj_rsi: 6,"
            " sentiment: 6, news: 3, index_volume: 6, board_rank: 4,"
            " overseas: 4, chan: 12, chip: 8, cycle: 3, character: 2}\n"
            "thresholds: {action: 25, hint: 15}\n")
    reset_config_cache()
    config = load_intraday_config(path)
    assert config.weights.box == 21
    assert config.thresholds.action == 25
    # 改写文件后应按 mtime 热重载
    import time
    time.sleep(0.01)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "weights: {box: 17, vwap: 11, boll: 8, macd: 6, kdj_rsi: 6,"
            " sentiment: 6, news: 3, index_volume: 6, board_rank: 4,"
            " overseas: 4, chan: 12, chip: 8, cycle: 6, character: 3}\n"
            "thresholds: {action: 30, hint: 20}\n")
    os.utime(path, (time.time() + 1, time.time() + 1))
    reloaded = load_intraday_config(path)
    assert reloaded.weights.box == 17
    assert reloaded.thresholds.action == 30


def test_partial_weights_report_actionable_error(tmp_dir) -> None:
    """只写原有10个权重（漏掉技能库新增4个维度）时，报错必须直接指出原因。"""
    import os

    path = os.path.join(tmp_dir, "legacy.yaml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "weights: {box: 24, vwap: 16, boll: 12, macd: 8, kdj_rsi: 8,"
            " sentiment: 8, news: 4, index_volume: 8, board_rank: 6,"
            " overseas: 6}\n"
            "thresholds: {action: 30, hint: 20}\n")
    reset_config_cache()
    with pytest.raises(ConfigError) as excinfo:
        load_intraday_config(path, force=True)
    message = str(excinfo.value)
    assert "权重合计" in message
    assert "chan" in message and "chip" in message
    assert "cycle" in message and "character" in message


def test_load_config_falls_back_when_missing(tmp_dir) -> None:
    import os

    reset_config_cache()
    config = load_intraday_config(os.path.join(tmp_dir, "absent.yaml"))
    assert sum(config.weights.as_dict().values()) == pytest.approx(100.0)
    assert config.thresholds.action == 30


def test_repo_config_file_is_valid() -> None:
    """仓库内的 configs/intraday.yaml 必须能**完整加载**（不是回退默认值）。

    只断言配置的**结构与校验**，不断言具体自选股 ——
    自选池由用户在前端维护，断言用户数据会让测试随用户操作而失败。
    """
    reset_config_cache()
    config = load_intraday_config("configs/intraday.yaml", force=True)
    assert config.load_error is None, f"仓库配置应可解析：{config.load_error}"
    weights = config.weights.as_dict()
    assert sum(weights.values()) == pytest.approx(100.0)
    assert len(weights) == 14, "应为14个因子维度（十因子 + 技能库四因子）"
    daily = config.daily_weights.as_dict()
    assert sum(daily.values()) == pytest.approx(100.0)
    assert len(daily) == 7, "日线做T应为7个因子维度"
    assert config.thresholds.action > config.thresholds.hint > 0
    assert config.daily_thresholds.action > config.daily_thresholds.hint > 0
    # 技能库四因子必须真的在仓库配置里生效（否则等于"接了但没开"）
    assert config.weights.chan > 0 and config.weights.chip > 0
    assert config.factors.cycle.enabled is True
    assert config.boards, "仓库配置应带关联板块"
    for item in config.watchlist:
        assert item.code.isdigit() and len(item.code) == 6
        for peer in item.peers:
            assert peer.isdigit() and len(peer) == 6
    # 板块必须显式绑定才参与打分：未在自选里声明的代码拿到空列表，
    # 参考板块只用于展示（避免「银行股按 PCB 板块排名打分」）。
    assert config.reference_board_names(), "仓库配置应带关联板块"
    for name in config.reference_board_names():
        assert isinstance(name, str) and name
    for item in config.watchlist:
        if item.boards:
            assert config.boards_bound(item.code) is True
            assert config.board_names(item.code) == item.boards


def test_scoreable_boards_ignore_reference_boards() -> None:
    """未绑定板块时，板块类维度必须记不可用，而不是拿参考板块凑分。

    复现原缺陷：随口查 600036，面板把配置里第一个板块（PCB概念）当它的板块，
    板块情绪(8) + 板块排行(6) 共 14 分权重被无关板块污染。
    """
    from src.intraday.models import BoardSnapshot, Quote

    def pcb() -> BoardSnapshot:
        return BoardSnapshot(
            name="PCB概念", available=True, change_pct=1.8,
            up_count=119, down_count=115, breadth=119 / 234, rank="10/390")

    config = IntradayConfig()
    quote = Quote(code="600036", name="招商银行", price=40.0, prev_close=40.0,
                  change_pct=-0.4)
    kwargs = dict(quote=quote, series=[], index_info={}, config=config)

    # 未绑定：展示参考板块，但情绪得分为 None（本维度不计入总分）
    panel = IntradayService._build_sentiment_panel(
        boards=[pcb()], scored_boards=[], boards_bound=False, **kwargs)
    assert panel.boards and panel.boards[0].name == "PCB概念"
    assert panel.boards_bound is False
    assert panel.score is None
    assert panel.board_change_pct is None
    assert panel.relative_strength_pct is None
    assert any("未绑定" in gap for gap in panel.gaps)

    # 已绑定：同一个板块快照进入打分
    bound = IntradayService._build_sentiment_panel(
        boards=[pcb()], scored_boards=[pcb()], boards_bound=True, **kwargs)
    assert bound.boards_bound is True
    assert bound.score is not None
    assert bound.board_change_pct == pytest.approx(1.8)
    assert bound.relative_strength_pct == pytest.approx(-2.2)


def test_unbound_code_marks_board_factors_unavailable() -> None:
    """端到端：未绑定板块的代码，板块情绪/板块排行因子应为不可用（权重不计入）。"""
    from src.intraday.factors import (
        FactorContext,
        score_board_rank,
        score_sentiment,
    )

    config = IntradayConfig()
    assert config.boards_bound("600036") is False
    assert config.board_names("600036") == []
    ctx = FactorContext(
        price=40.0, stock_change_pct=-0.4,
        board_change_pct=None, breadth=None, board_rank=None,
        board_rank_percentile=None)
    sentiment = score_sentiment(ctx, config.factors.sentiment)
    rank = score_board_rank(ctx, config.factors.board_rank)
    assert sentiment.available is False
    assert rank.available is False
    assert sentiment.score == 0.0 and rank.score == 0.0
    assert rank.gap, "不可用必须给出缺口说明，而不是静默计 0 分"


def test_service_construction_without_backend() -> None:
    """无后端依赖时也应可构造（各子链路降级为缺口，不抛错）。"""
    service = IntradayService(backend=None, gateway=None, news_fetcher=None)
    assert service.config.thresholds.action == 30
    assert service.notifier.channel_status()["enabled"] is True
