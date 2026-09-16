"""做T模块 · 七因子打分单元测试。

核心是 golden 用例：用需求方给定的目标截图（箱体70%位 / VWAP偏离-0.337% z=-0.85 /
布林%B≈0.405 / MACD偏多 / KDJ-RSI微空 / 板块19-33家上涨且跑输1.02% / 偏多5偏空10）
逐项校验打分口径，并复核总分落在截图所示 2.6 的同一区间（震荡、不动手）。
"""

from __future__ import annotations

import pytest

from src.core.exceptions import ConfigError
from src.intraday.config import (
    BollParams,
    BoxParams,
    IntradayConfig,
    KdjRsiParams,
    MacdParams,
    NewsParams,
    SentimentParams,
    VwapParams,
    Weights,
)
from src.intraday.factors import (
    FactorContext,
    blend_timeframes,
    boll_score_from_pct_b,
    box_score_from_position,
    news_count_score,
    news_score_from,
    osc_score_from_jr,
    score_boll,
    score_box,
    score_news,
    score_sentiment,
    score_vwap,
    sentiment_score_from,
    vwap_score_from_z,
)

# ==================== 打分核（公式级） ====================


@pytest.mark.parametrize("position,expected", [
    (0.5, 0.0),      # 箱体中枢 → 方向中性
    (0.7, -0.101),   # 目标截图口径：贴近上沿 → 约 -0.10
    (0.3, 0.101),    # 对称：贴近下沿 → +0.10
    (0.0, 1.0),      # 触及下沿 → +1
    (1.0, -1.0),     # 触及上沿 → -1
])
def test_box_score_matches_reference(position: float, expected: float) -> None:
    """箱体位置→得分：中枢中性、边缘饱和，且70%位复现截图的 -0.10。"""
    assert box_score_from_position(position, exponent=2.5) == pytest.approx(
        expected, abs=0.002)


def test_box_score_is_symmetric() -> None:
    """箱体分对中枢对称（做T多空对称）。"""
    for offset in (0.05, 0.2, 0.4):
        up = box_score_from_position(0.5 + offset)
        down = box_score_from_position(0.5 - offset)
        assert up == pytest.approx(-down, abs=1e-9)


def test_vwap_score_reproduces_reference() -> None:
    """VWAP偏离口径：z=-0.85 → +0.28（复现截图）。"""
    assert vwap_score_from_z(-0.85, z_scale=3.0) == pytest.approx(0.2833, abs=0.001)
    # 符号方向：低于均价为正（超跌反弹），高于均价为负（冲高回落）
    assert vwap_score_from_z(-3.0) == pytest.approx(1.0)
    assert vwap_score_from_z(3.0) == pytest.approx(-1.0)
    assert vwap_score_from_z(6.0) == pytest.approx(-1.0)  # 饱和裁剪


def test_boll_score_reproduces_reference() -> None:
    """布林口径：%B=0.405 → +0.19（复现截图）。"""
    assert boll_score_from_pct_b(0.405) == pytest.approx(0.19, abs=0.001)
    assert boll_score_from_pct_b(0.0) == pytest.approx(1.0)
    assert boll_score_from_pct_b(1.0) == pytest.approx(-1.0)


def test_boll_score_damped_when_squeeze() -> None:
    """布林收口时均值回归信号打 squeeze_damp 折。"""
    normal = boll_score_from_pct_b(0.1, squeeze=False)
    squeezed = boll_score_from_pct_b(0.1, squeeze=True, squeeze_damp=0.6)
    assert squeezed == pytest.approx(normal * 0.6, abs=1e-9)
    assert abs(squeezed) < abs(normal)


def test_sentiment_score_reproduces_reference() -> None:
    """情绪口径：19/33家上涨 + 跑输板块1.02% → -0.18（复现截图）。"""
    params = SentimentParams()
    breadth = 19 / 33
    score = sentiment_score_from(breadth, -1.02, params)
    assert score == pytest.approx(-0.18, abs=0.01)
    # 只用涨跌家数（无相对强度）时也应有定义
    assert sentiment_score_from(0.9, None, params) == pytest.approx(0.8, abs=1e-6)
    # 两者皆缺 → None（由引擎按缺口处理）
    assert sentiment_score_from(None, None, params) is None


def test_news_count_score_reproduces_reference() -> None:
    """消息面计数口径：偏多5 / 偏空10 → -1.00（复现截图）。"""
    assert news_count_score(5, 10) == pytest.approx(-1.0)
    assert news_count_score(10, 5) == pytest.approx(1.0)
    assert news_count_score(0, 0) == pytest.approx(0.0)
    # 少量新闻不应给出与大量新闻同等的极端分
    assert abs(news_count_score(1, 2)) == pytest.approx(1.0)  # 3×(-1)/3 = -1
    assert abs(news_count_score(5, 6)) < abs(news_count_score(0, 5))


def test_news_score_blends_llm_and_count() -> None:
    """LLM 语义分与计数分加权融合；LLM缺失时退化为纯计数。"""
    params = NewsParams(llm_weight=0.6, count_weight=0.4)
    blended = news_score_from(-1.0, 5, 10, params)
    assert blended == pytest.approx(-1.0, abs=1e-6)
    count_only = news_score_from(None, 5, 10, params)
    assert count_only == pytest.approx(-1.0, abs=1e-6)
    # LLM与计数分歧时被拉向中间
    mixed = news_score_from(1.0, 10, 0, params)
    assert mixed == pytest.approx(1.0, abs=1e-6)
    neutral = news_score_from(0.0, 5, 10, params)
    assert -1.0 < neutral < 0.0


def test_osc_score_averages_j_and_rsi() -> None:
    """KDJ-J 与 RSI 各占一半；单一缺失仍可算。"""
    assert osc_score_from_jr(50.0, 50.0, rsi_scale=40.0) == pytest.approx(0.0)
    assert osc_score_from_jr(0.0, 10.0, rsi_scale=40.0) == pytest.approx(
        (1.0 + 1.0) / 2)
    assert osc_score_from_jr(70.0, None, rsi_scale=40.0) == pytest.approx(-0.4)
    assert osc_score_from_jr(None, None) is None


def test_blend_timeframes_renormalizes_when_one_side_missing() -> None:
    """缺一周期时由另一周期独担，避免被0分拖到中枢。"""
    assert blend_timeframes(0.8, 0.2, 0.6, 0.4) == pytest.approx(0.56)
    assert blend_timeframes(0.8, None, 0.6, 0.4) == pytest.approx(0.8)
    assert blend_timeframes(None, -0.5, 0.6, 0.4) == pytest.approx(-0.5)
    assert blend_timeframes(None, None, 0.6, 0.4) is None


# ==================== golden：复现目标截图的总分 ====================


def test_golden_reference_screenshot_total() -> None:
    """用截图各项输入复现总分 2.6（允许±0.2，截图本身有四舍五入）。

    重要：截图对应的是**原始7因子权重(30/20/15/10/10/10/5)**，因此本用例
    显式使用那套权重来校验**公式**（公式没变）；随后再验证当前10因子配置下的
    基线（原7项按0.8缩放 → 总分也按0.8缩放）。

    分项（贡献分，按截图权重）：
      箱体 -0.10×30 = -3.03 ｜ VWAP +0.283×20 = +5.67 ｜ 布林 +0.19×15 = +2.85
      MACD +0.48×10 = +4.80 ｜ KDJ-RSI -0.08×10 = -0.80 ｜ 情绪 -0.179×10 = -1.79
      消息面 -1.00×5 = -5.00
      合计 ≈ +2.70 → 落在提示线±20 以内 → 震荡区间不动手
    """
    config = IntradayConfig()
    screenshot_weights = {
        "box": 30, "vwap": 20, "boll": 15, "macd": 10,
        "kdj_rsi": 10, "sentiment": 10, "news": 5,
    }

    box = box_score_from_position(0.7, config.factors.box.exponent)
    vwap = vwap_score_from_z(-0.85, config.factors.vwap.z_scale)
    boll = boll_score_from_pct_b(0.405)
    macd = 0.48
    kdj_rsi = -0.08
    sentiment = sentiment_score_from(19 / 33, -1.02, config.factors.sentiment)
    news = news_score_from(-1.0, 5, 10, config.factors.news)

    # 1) 逐项公式必须与截图一致（与权重无关，这是回归的核心）
    assert box == pytest.approx(-0.10, abs=0.005)
    assert vwap == pytest.approx(0.28, abs=0.005)
    assert boll == pytest.approx(0.19, abs=0.005)
    assert sentiment == pytest.approx(-0.18, abs=0.01)
    assert news == pytest.approx(-1.00, abs=0.005)

    # 2) 按截图权重合成 → 复现 2.6
    screenshot_total = (
        box * screenshot_weights["box"] + vwap * screenshot_weights["vwap"]
        + boll * screenshot_weights["boll"] + macd * screenshot_weights["macd"]
        + kdj_rsi * screenshot_weights["kdj_rsi"]
        + sentiment * screenshot_weights["sentiment"]
        + news * screenshot_weights["news"]
    )
    assert screenshot_total == pytest.approx(2.6, abs=0.2)
    assert abs(screenshot_total) < config.thresholds.hint

    # 3) 现在必须用 `legacy` 模板才能复现"改动前的口径"：
    #    当前默认已是14因子（技能库四因子另占29），原十因子被缩放到 0.71；
    #    legacy 模板把四因子置0、恢复十因子的 24/16/12/8/8/8/4/8/6/6。
    #    注意它与**截图**的 v1 七因子（箱体30/VWAP20/…）还差一个 0.8 缩放 ——
    #    截图是七因子版本，legacy 是十因子版本，两者差 0.8 是设计内的。
    from src.intraday.weight_profiles import template

    legacy = template("legacy", "intraday")
    assert legacy is not None
    weights = legacy.normalized()
    assert weights["box"] / screenshot_weights["box"] == pytest.approx(0.8)
    current_total = (
        box * weights["box"] + vwap * weights["vwap"] + boll * weights["boll"]
        + macd * weights["macd"] + kdj_rsi * weights["kdj_rsi"]
        + sentiment * weights["sentiment"] + news * weights["news"]
    )
    assert current_total == pytest.approx(screenshot_total * 0.8, abs=0.05)
    assert abs(current_total) < config.thresholds.hint  # 仍是震荡区间
    # 4) 默认口径（14因子）下，技能库四因子必须真的参与打分（不是摆设）
    default_weights = config.weights.as_dict()
    assert default_weights["chan"] > 0 and default_weights["chip"] > 0
    assert default_weights["cycle"] > 0 and default_weights["character"] > 0
    assert sum(default_weights.values()) == pytest.approx(100.0)


# ==================== 打分器（上下文级，含缺口处理） ====================


def test_score_box_reports_gap_without_daily_sample() -> None:
    """日线样本不足时该因子不计入并给出缺口原因，而不是给0分蒙混。"""
    outcome = score_box(
        FactorContext(price=10.0, box_high=11.0, box_low=9.0,
                      box_position=None, box_span_days=2),
        BoxParams())
    assert outcome.available is False
    assert outcome.score == 0.0
    assert "日线样本不足" in (outcome.gap or "")


def test_score_vwap_requires_zscore() -> None:
    outcome = score_vwap(FactorContext(price=10.0, vwap=10.0, dev_z=None),
                         VwapParams())
    assert outcome.available is False
    assert outcome.gap


def test_score_boll_squeeze_flag_in_inputs() -> None:
    """带宽分位处于收口区时，inputs 中留下 squeeze=True 的溯源痕迹。"""
    ctx = FactorContext(price=10.0, pct_b=0.1, bandwidth=0.01,
                        bandwidth_pctl=0.05)
    outcome = score_boll(ctx, BollParams(squeeze_percentile=0.2, squeeze_damp=0.6))
    assert outcome.inputs["squeeze"] is True
    assert outcome.score == pytest.approx(0.8 * 0.6, abs=1e-6)


def test_score_sentiment_uses_relative_strength() -> None:
    ctx = FactorContext(
        price=10.0, breadth=0.9, up_count=18, down_count=2,
        stock_change_pct=2.0, board_change_pct=0.0)
    outcome = score_sentiment(ctx, SentimentParams())
    assert outcome.available is True
    assert outcome.score > 0.5  # 情绪暖 + 大幅跑赢 → 强正分
    assert "跑赢" in outcome.detail


def test_score_news_falls_back_to_counts_without_llm() -> None:
    ctx = FactorContext(price=10.0, news_positive=1, news_negative=4,
                        news_count=5, news_llm_score=None)
    outcome = score_news(ctx, NewsParams())
    assert outcome.available is True
    assert outcome.score < 0
    assert "纯计数口径" in outcome.detail


# ==================== 权重与配置校验 ====================


def test_weights_must_sum_to_100() -> None:
    """权重合计偏离100 → 立即拒绝（否则±20/±30阈值刻度失效）。"""
    with pytest.raises(ConfigError, match="权重合计"):
        Weights(box=30, vwap=20, boll=15, macd=10, kdj_rsi=10, sentiment=10, news=10)


def test_default_weights_sum_to_100() -> None:
    assert sum(IntradayConfig().weights.as_dict().values()) == pytest.approx(100.0)
    assert sum(Weights().as_dict().values()) == pytest.approx(100.0)


def test_threshold_ordering_validated() -> None:
    from src.intraday.config import Thresholds

    with pytest.raises(ConfigError):
        Thresholds(action=20, hint=30)
    with pytest.raises(ConfigError):
        Thresholds(action=30, hint=0)


def test_macd_fast_must_be_less_than_slow() -> None:
    with pytest.raises(ConfigError):
        MacdParams(fast=26, slow=12)


def test_kdj_rsi_defaults_ok() -> None:
    params = KdjRsiParams()
    assert params.tf_weights.normalized() == pytest.approx((0.5, 0.5))
