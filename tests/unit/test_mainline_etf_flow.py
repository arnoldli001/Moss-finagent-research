"""ETF 份额监控（`src/mainline/etf_flow.py`）单元测试。

重点覆盖**市场环境门控** —— 它是这个模块唯一一处主动收窄信号的地方，
也是回测里唯一把"看起来无效的信号"变成"达标信号"的机制。门控一旦失效，
面板上会重新出现 T+34 胜率 49% 的机会信号，而且不会有任何报错，
所以这里对边界条件测得比较细。

不覆盖 `etf_flow_backtest`：它依赖本地仓库与全量数据，
属于数据验证脚本（见 `docs/ETF_FLOW_BACKTEST.md`），不做单元测试。
"""

from __future__ import annotations

import pytest

from src.mainline.etf_flow import (
    KIND_INDUSTRY_REVERSAL,
    KIND_OPPORTUNITY,
    KIND_RISK,
    LEVEL_MEDIUM,
    LEVEL_STRONG,
    LEVEL_WEAK,
    REGIME_BEAR,
    REGIME_BULL,
    REGIME_RANGE,
    EtfIndicator,
    EtfSpec,
    FlowConfig,
    IndexPosition,
    MarketRegime,
    WatchGroup,
    adjust_share_splits,
    apply_regime_policy,
    build_indicators,
    build_regime,
    build_resonance,
    classify_regime,
    evaluate,
    index_percentile,
)

# ==================================================================
# 夹具
# ==================================================================


def _config(*, level: str = "core", index: str = "000300.SH",
            thresholds: dict | None = None) -> FlowConfig:
    """沪深300 系列 4 只 ETF（≥3 只才可能共振）。"""
    group = WatchGroup(key="hs300", label="沪深300系列", index=index,
                       index_name="沪深300", level=level)
    for code, name in (("510300.SH", "华泰柏瑞沪深300ETF"),
                       ("510310.SH", "易方达沪深300ETF"),
                       ("510330.SH", "华夏沪深300ETF"),
                       ("159919.SZ", "嘉实沪深300ETF")):
        group.etfs.append(EtfSpec(code=code, name=name, group="hs300",
                                  index=index, index_name="沪深300",
                                  level=level))
    return FlowConfig(groups=[group], thresholds=dict(thresholds or {}),
                      loaded=True)


def _indicator(code: str, change_1d: float, change_5d: float = 0.0,
               trade_date: str = "20260918") -> EtfIndicator:
    return EtfIndicator(code=code, name=code, group="hs300", level="core",
                        trade_date=trade_date, shares=100.0, close=1.0,
                        change_1d=change_1d, change_5d=change_5d)


def _regime(key: str, change: float = -0.20) -> MarketRegime:
    out = MarketRegime(key=key, change=change)
    return out


# ==================================================================
# 份额拆分还原
# ==================================================================


def _split_rows(*, ratio: float = 1.9629, price_ratio: float = 0.5133,
                days: int = 8) -> list[dict]:
    """造一段序列：第 5 天发生份额拆分（份额 ×ratio，单位净值 ×price_ratio）。

    真实数据（`159516.SZ` 2026-03-30 的 1 拆 2）：份额 1,200,859 → 2,357,118，
    收盘 1.660 → 0.852 —— 市值只差 0.74%，也就是说**没有发生任何申赎**。
    """
    rows = []
    for index in range(days):
        split = index >= 5
        shares = 1_200_859.0 * (ratio if split else 1.0)
        close = 1.660 * (price_ratio if split else 1.0)
        rows.append({"trade_date": f"202603{20 + index:02d}",
                     "shares": shares, "close": close, "amount": 1.0e8})
    return rows


def _consolidation_rows() -> list[dict]:
    """份额**合并**（512100.SH 2022-09-05 的形状）：合并后永久生效。

    ⚠️ 夹具必须让变化**持续到序列末尾**：早先写成"只有中间一天是 35.55、
    后面又回到 100"，那在真实数据里不可能出现（份额合并不可能第二天就恢复），
    而且会被识别成两次拆分。夹具不真实 → 测试测的是夹具自己的毛病。
    """
    rows = [{"trade_date": f"2022090{index}", "shares": 100.0,
             "close": 1.0, "amount": 1.0e8} for index in range(1, 7)]
    for row in rows[3:]:
        row.update({"shares": 35.55, "close": 2.7627})    # -64% / +176%
    return rows


class TestAdjustShareSplits:
    """份额拆分/合并的识别与还原（3 个真实案例：见 `adjust_share_splits`）。"""

    def test_split_day_change_becomes_flat(self):
        """拆分日的份额变化率必须≈0 —— 机械换股不是申赎。

        ⚠️ 这条测试同时钉住两个曾经写错的方向：
        * 改的是**拆分日之前**的行（拆分日及之后是真实存量，不能动）；
        * 缩放因子是 `1/份额比`（缩到当前基准），不是份额比本身。
        写错任一个，这里都会得到 +285% 而不是 0。
        """
        rows = _split_rows()
        adjusted, splits = adjust_share_splits(rows)

        assert splits == ["20260325"]
        # 拆分日及之后保持原始存量；之前的历史**乘**份额比折算到新基准
        assert adjusted[5]["shares"] == pytest.approx(1_200_859.0 * 1.9629)
        assert adjusted[4]["shares"] == pytest.approx(1_200_859.0 * 1.9629)
        assert adjusted[5]["shares"] / adjusted[4]["shares"] - 1.0 \
            == pytest.approx(0.0, abs=1e-4)

    def test_history_before_split_is_rebased_not_future_rows(self):
        """只有拆分日**之前**的行被折算；拆分日及之后的原始存量一个数不动。"""
        rows = _split_rows()
        adjusted, _ = adjust_share_splits(rows)

        for index in range(5, len(adjusted)):
            assert adjusted[index]["shares"] == pytest.approx(rows[index]["shares"])
        assert adjusted[4]["shares"] == pytest.approx(1_200_859.0 * 1.9629)

    def test_input_rows_are_not_mutated(self):
        """纯函数：不能就地改调用方的数据（`etf_bars` 的返回值会被别处复用）。"""
        rows = _split_rows()
        original = [dict(row) for row in rows]

        adjust_share_splits(rows)

        assert rows == original

    def test_plain_inflow_is_not_treated_as_split(self):
        """真实大额申购**不会**被误判：缺少"价格反向跳变"这个必要条件。

        159516 在 2026-04-03 份额 +1.91%、价格 -0.25% —— 份额动了但没有
        同比例的价格反向变动，是真实申赎，必须原样保留。
        """
        rows = [{"trade_date": f"2026040{index}", "shares": 100.0,
                 "close": 1.0, "amount": 1.0e8} for index in range(1, 7)]
        rows[3]["shares"] = 140.0          # +40% 申购
        adjusted, splits = adjust_share_splits(rows)

        assert splits == []
        assert adjusted[3]["shares"] == pytest.approx(140.0)

    def test_same_direction_move_is_not_a_split(self):
        """份额与价格**同向**变动（都在跌）不是拆分 —— 拆分必然一涨一跌。"""
        rows = [{"trade_date": f"2026040{index}", "shares": 100.0,
                 "close": 1.0, "amount": 1.0e8} for index in range(1, 7)]
        rows[3].update({"shares": 60.0, "close": 0.6})   # 同向下跌
        adjusted, splits = adjust_share_splits(rows)

        assert splits == []
        assert adjusted[3]["shares"] == pytest.approx(60.0)

    def test_reverse_split_consolidation(self):
        """份额**合并**（512100.SH 2022-09-05 的形状）同样要还原。

        那次是份额 -64%、价格 +176%，系统原本把它读成"巨额赎回"。
        """
        rows = _consolidation_rows()
        adjusted, splits = adjust_share_splits(rows)

        assert splits == ["20220904"]
        # 合并后基准变小（新基准 35.55），合并前的 100 折算过去应是 35.55
        assert adjusted[2]["shares"] == pytest.approx(100.0 * 0.3555)
        assert adjusted[3]["shares"] == pytest.approx(35.55)
        assert adjusted[3]["shares"] / adjusted[2]["shares"] - 1.0 \
            == pytest.approx(0.0, abs=1e-3)

    def test_build_indicators_uses_adjusted_series(self):
        """`build_indicators` 必须用还原后的序列算变化率，并如实标注拆分。"""
        config = _config(thresholds={})
        rows = _split_rows(days=30)          # 尾部连续 5 日都在拆分之后
        item = build_indicators({"510300.SH": rows}, config,
                                trade_date="20260330")["510300.SH"]

        assert item.change_1d == pytest.approx(0.0, abs=1e-4)
        assert any("拆分" in gap for gap in item.gaps), "拆分必须如实标注"
        # 绝对份额按**原始**存量展示（拆分后的 2,357,118），不做还原
        assert item.shares == pytest.approx(1_200_859.0 * 1.9629)

    def test_no_split_keeps_original_numbers(self):
        """没有拆分时一个数都不该变（避免"为了修 bug 改坏正常路径"）。"""
        config = _config(thresholds={})
        rows = [{"trade_date": f"202609{10 + index:02d}", "shares": 100.0 + index,
                 "close": 1.0, "amount": 1.0e8} for index in range(8)]
        item = build_indicators({"510300.SH": rows}, config,
                                trade_date="20260917")["510300.SH"]

        assert item.shares == pytest.approx(107.0)
        assert item.change_1d == pytest.approx(1.0 / 106.0)
        assert item.gaps == []


# ==================================================================
# 环境判定
# ==================================================================


class TestClassifyRegime:
    def test_bull_when_trailing_return_above_threshold(self):
        closes = [100.0] * 120 + [130.0]      # +30% 近 120 日
        key, change = classify_regime(closes, window=120, bull=0.15, bear=-0.15)
        assert key == REGIME_BULL
        assert change == pytest.approx(0.30)

    def test_bear_when_trailing_return_below_threshold(self):
        closes = [100.0] * 120 + [70.0]       # -30%
        key, _ = classify_regime(closes, window=120, bull=0.15, bear=-0.15)
        assert key == REGIME_BEAR

    def test_range_when_between_thresholds(self):
        closes = [100.0] * 120 + [105.0]      # +5%
        key, _ = classify_regime(closes, window=120, bull=0.15, bear=-0.15)
        assert key == REGIME_RANGE

    def test_exactly_on_threshold_is_range(self):
        """阈值判定用严格不等号 —— 恰好 +15% 不算牛市。"""
        closes = [100.0] * 120 + [115.0]
        key, _ = classify_regime(closes, window=120, bull=0.15, bear=-0.15)
        assert key == REGIME_RANGE

    def test_insufficient_history_returns_range_and_none(self):
        """历史不足时返回震荡市 + None，而不是抛错或猜一个方向。"""
        key, change = classify_regime([100.0] * 10, window=120)
        assert key == REGIME_RANGE
        assert change is None

    def test_ignores_none_values_when_counting_window(self):
        """收盘序列里的 None 被跳过，不影响窗口判定。"""
        closes = [None] * 5 + [100.0] * 120 + [130.0]
        key, _ = classify_regime(closes, window=120, bull=0.15, bear=-0.15)
        assert key == REGIME_BULL


class TestApplyRegimePolicy:
    def test_default_allows_opportunity_only_in_bear(self):
        config = _config()
        for key, expected in ((REGIME_BEAR, True), (REGIME_BULL, False),
                              (REGIME_RANGE, False)):
            regime = _regime(key)
            apply_regime_policy(regime, config)
            assert regime.opportunity_allowed is expected, key

    def test_default_excludes_risk_in_bear(self):
        config = _config()
        for key, expected in ((REGIME_BULL, True), (REGIME_RANGE, True),
                              (REGIME_BEAR, False)):
            regime = _regime(key)
            apply_regime_policy(regime, config)
            assert regime.risk_allowed is expected, key

    def test_config_override_widens_opportunity_window(self):
        config = _config(thresholds={
            "regime": {"opportunity_allowed": [REGIME_BEAR, REGIME_RANGE]}})
        regime = _regime(REGIME_RANGE)
        apply_regime_policy(regime, config)
        assert regime.opportunity_allowed is True

    def test_bare_string_is_accepted_as_single_regime(self):
        """`opportunity_allowed: bear` 写成字符串（不加方括号）也要能用。"""
        config = _config(thresholds={"regime": {"opportunity_allowed": "bear"}})
        bear = _regime(REGIME_BEAR)
        apply_regime_policy(bear, config)
        assert bear.opportunity_allowed is True

    def test_empty_allowlist_blocks_everything(self):
        """显式给空列表 = 关闭该信号的告警，不能被当成"缺省"而回退默认值。"""
        config = _config(thresholds={"regime": {"opportunity_allowed": []}})
        regime = _regime(REGIME_BEAR)
        apply_regime_policy(regime, config)
        assert regime.opportunity_allowed is False

    def test_wrong_type_falls_back_to_default(self):
        config = _config(thresholds={"regime": {"opportunity_allowed": 123}})
        regime = _regime(REGIME_BEAR)
        apply_regime_policy(regime, config)
        assert regime.opportunity_allowed is True

    def test_uppercase_regime_name_is_normalised(self):
        config = _config(thresholds={"regime": {"opportunity_allowed": ["BEAR"]}})
        regime = _regime(REGIME_BEAR)
        apply_regime_policy(regime, config)
        assert regime.opportunity_allowed is True


class TestBuildRegime:
    def test_prefers_core_group_index_over_industry(self):
        """参考指数取 `level: core` 的组，而不是清单里第一个有数据的指数。

        用行业 ETF 的指数判大盘环境会把"行业自己的行情"误当成市场环境。
        """
        core = _config()
        industry = WatchGroup(key="ind", label="行业", index="399999.SZ",
                              index_name="行业指数", level="industry")
        industry.etfs.append(EtfSpec(code="159516.SZ", group="ind",
                                     level="industry"))
        core.groups.insert(0, industry)          # 行业组排在前面
        bars = {"399999.SZ": [("20260918", 1.0)] * 200,
                "000300.SH": [("20260918", 1.0)] * 120 + [("20260918", 1.3)]}
        regime = build_regime(bars, config=core, trade_date="20260918")
        assert regime.index_code == "000300.SH"

    def test_missing_index_data_degrades_to_range_with_gap(self):
        config = _config()
        regime = build_regime({}, config=config, trade_date="20260918")
        assert regime.key == REGIME_RANGE
        assert regime.gaps

    def test_bear_market_index_marks_opportunity_allowed(self):
        config = _config()
        closes = [(f"d{i:03d}", 100.0) for i in range(120)]
        closes.append(("20260918", 70.0))
        regime = build_regime({"000300.SH": closes}, config=config,
                              trade_date="20260918")
        assert regime.key == REGIME_BEAR
        assert regime.opportunity_allowed is True


# ==================================================================
# 分位
# ==================================================================


class TestIndexPercentile:
    def test_rank_mode_at_top_is_one(self):
        closes = list(range(1, 40))
        assert index_percentile(closes, window=34, mode="rank") == pytest.approx(1.0)

    def test_rank_mode_at_bottom_is_small(self):
        closes = list(range(40, 1, -1))
        assert index_percentile(closes, window=34,
                                mode="rank") < 0.10

    def test_rank_mode_is_robust_to_a_single_spike(self):
        """区间法会被一根插针压扁，分位排名不会 —— 这是默认用 rank 的原因。

        插针本身不计入"≤当日"，所以 rank 会从 1.0 微降到 33/34，
        但仍留在高位；同一根插针让区间法直接掉到 0.1% 附近。
        """
        spiked = [10.0] * 32 + [1000.0, 11.0]
        rank = index_percentile(spiked, window=34, mode="rank")
        span = index_percentile(spiked, window=34, mode="range")
        assert rank == pytest.approx(33 / 34)
        assert span is not None
        assert rank > 0.95
        assert span < 0.05

    def test_insufficient_history_returns_none(self):
        assert index_percentile([1.0, 2.0], window=34) is None

    def test_flat_window_returns_none_in_range_mode(self):
        """区间法分母为 0 时返回 None，而不是除零或强行给 0.5。"""
        assert index_percentile([10.0] * 40, window=34, mode="range") is None


# ==================================================================
# 指标
# ==================================================================


class TestBuildIndicators:
    def test_computes_multi_window_changes(self):
        config = _config()
        rows = [{"trade_date": f"d{i:03d}", "shares": 100.0 + i, "close": 1.0}
                for i in range(30)]
        item = build_indicators({s.code: rows for s in config.all_etfs},
                                config)["510300.SH"]
        assert item.shares == pytest.approx(129.0)
        assert item.change_1d == pytest.approx(129.0 / 128.0 - 1.0)
        assert item.change_5d == pytest.approx(129.0 / 124.0 - 1.0)
        assert item.change_10d == pytest.approx(129.0 / 119.0 - 1.0)
        assert item.change_20d == pytest.approx(129.0 / 109.0 - 1.0)

    def test_missing_share_leaves_change_none_not_zero(self):
        """份额缺失 → None。补 0 会让"份额显著增加"的机会信号静默消失。"""
        config = _config()
        rows = [{"trade_date": "d0", "shares": None, "close": 1.0},
                {"trade_date": "d1", "shares": None, "close": 1.0}]
        item = build_indicators({s.code: rows for s in config.all_etfs},
                                config)["510300.SH"]
        assert item.shares is None
        assert item.change_1d is None
        assert item.gaps

    def test_no_data_records_gap(self):
        config = _config()
        item = build_indicators({}, config)["510300.SH"]
        assert item.gaps
        assert item.change_1d is None


# ==================================================================
# 门控后的信号
# ==================================================================


def _positions(percentile: float) -> dict[str, IndexPosition]:
    return {"000300.SH": IndexPosition(code="000300.SH", name="沪深300",
                                       trade_date="20260918",
                                       percentile=percentile)}


def _all_rising(config: FlowConfig, change: float = 0.05
                ) -> dict[str, EtfIndicator]:
    return {spec.code: _indicator(spec.code, change, change)
            for spec in config.all_etfs}


class TestEvaluateGating:
    def test_bear_market_promotes_opportunity_to_alert(self):
        config = _config()
        regime = _regime(REGIME_BEAR)
        apply_regime_policy(regime, config)
        signals = evaluate(_all_rising(config), _positions(0.10), config=config,
                           trade_date="20260918", regime=regime)
        assert signals and all(item.kind == KIND_OPPORTUNITY for item in signals)
        assert all(not item.gated for item in signals)
        assert all(item.level in (LEVEL_STRONG, LEVEL_MEDIUM) for item in signals)

    def test_bull_market_downgrades_opportunity_to_observation(self):
        config = _config()
        regime = _regime(REGIME_BULL, 0.30)
        apply_regime_policy(regime, config)
        signals = evaluate(_all_rising(config), _positions(0.10), config=config,
                           trade_date="20260918", regime=regime)
        assert signals
        for item in signals:
            assert item.gated is True
            assert item.level == LEVEL_WEAK          # 降级，不是丢弃
            assert item.regime == REGIME_BULL
            assert any("环境门控" in reason for reason in item.reasons)

    def test_gated_signal_is_kept_not_silently_dropped(self):
        """降级 ≠ 丢弃：信号仍在列表里，使用者能看到"有异动但不建议"。"""
        config = _config()
        regime = _regime(REGIME_RANGE)
        apply_regime_policy(regime, config)
        signals = evaluate(_all_rising(config), _positions(0.10), config=config,
                           trade_date="20260918", regime=regime)
        assert len(signals) == len(config.all_etfs)

    def test_gating_suppresses_resonance_upgrade(self):
        """被门控的信号不能因为共振又被提升回强信号。"""
        config = _config()
        regime = _regime(REGIME_BULL, 0.30)
        apply_regime_policy(regime, config)
        signals = evaluate(_all_rising(config), _positions(0.10), config=config,
                           trade_date="20260918", regime=regime)
        assert signals
        assert all(item.level == LEVEL_WEAK for item in signals)
        assert not any(item.level == LEVEL_STRONG for item in signals)

    def test_resonance_upgrade_still_works_when_allowed(self):
        """放行环境下共振仍能把中信号提升为强信号（需求 3.4 未被破坏）。"""
        config = _config()
        regime = _regime(REGIME_BEAR)
        apply_regime_policy(regime, config)
        signals = evaluate(_all_rising(config), _positions(0.10), config=config,
                           trade_date="20260918", regime=regime)
        assert all(item.resonance for item in signals)
        assert all(item.level == LEVEL_STRONG for item in signals)

    def test_risk_signal_is_gated_in_bear_market(self):
        config = _config()
        regime = _regime(REGIME_BEAR)
        apply_regime_policy(regime, config)
        falling = {spec.code: _indicator(spec.code, -0.05, -0.06)
                   for spec in config.all_etfs}
        signals = evaluate(falling, _positions(0.90), config=config,
                           trade_date="20260918", regime=regime)
        assert signals
        assert all(item.kind == KIND_RISK for item in signals)
        assert all(item.gated for item in signals)

    def test_risk_signal_alerts_in_bull_market(self):
        config = _config()
        regime = _regime(REGIME_BULL, 0.30)
        apply_regime_policy(regime, config)
        falling = {spec.code: _indicator(spec.code, -0.05, -0.06)
                   for spec in config.all_etfs}
        signals = evaluate(falling, _positions(0.90), config=config,
                           trade_date="20260918", regime=regime)
        assert signals
        assert all(not item.gated for item in signals)
        assert all(item.kind == KIND_RISK for item in signals)

    def test_industry_reversal_is_never_gated(self):
        """行业反转警示与大盘环境无关（衡量的是行业 ETF 的散户追涨）。"""
        config = _config(level="industry")
        regime = _regime(REGIME_BULL, 0.30)
        apply_regime_policy(regime, config)
        rising = {spec.code: _indicator(spec.code, 0.01, 0.20)
                  for spec in config.all_etfs}
        signals = evaluate(rising, _positions(0.10), config=config,
                           trade_date="20260918", regime=regime)
        assert signals
        assert all(item.kind == KIND_INDUSTRY_REVERSAL for item in signals)
        assert all(not item.gated for item in signals)

    def test_no_regime_leaves_behaviour_unchanged(self):
        """不传 regime（历史调用、冷启动）时不做任何门控。"""
        config = _config()
        signals = evaluate(_all_rising(config), _positions(0.10), config=config,
                           trade_date="20260918")
        assert signals
        assert all(not item.gated for item in signals)
        assert all(item.level in (LEVEL_STRONG, LEVEL_MEDIUM) for item in signals)

    def test_weak_signals_are_not_marked_gated(self):
        """走"分位不在极端区"那条路径的信号本来就是观察项，不打门控标记。"""
        config = _config()
        regime = _regime(REGIME_BULL, 0.30)
        apply_regime_policy(regime, config)
        signals = evaluate(_all_rising(config, 0.05), _positions(0.50),
                           config=config, trade_date="20260918", regime=regime)
        assert signals
        assert all(item.level == LEVEL_WEAK for item in signals)
        assert all(not item.gated for item in signals)


class TestSerialisation:
    def test_signal_dict_carries_regime_and_gated(self):
        config = _config()
        regime = _regime(REGIME_BULL, 0.30)
        apply_regime_policy(regime, config)
        signal = evaluate(_all_rising(config), _positions(0.10), config=config,
                          trade_date="20260918", regime=regime)[0]
        payload = signal.to_dict()
        assert payload["regime"] == REGIME_BULL
        assert payload["regime_label"] == "牛市"
        assert payload["gated"] is True

    def test_regime_dict_exposes_both_gates(self):
        payload = _regime(REGIME_BEAR).to_dict()
        assert "opportunity_allowed" in payload
        assert "risk_allowed" in payload


class TestBuildResonance:
    def test_counts_directions_and_flags_resonance(self):
        config = _config()
        indicators = _all_rising(config)
        rows = build_resonance(indicators, config)
        assert rows[0]["rising"] == 4
        assert rows[0]["falling"] == 0
        assert rows[0]["resonance"] is True

    def test_missing_data_does_not_count_as_a_direction(self):
        config = _config()
        indicators = {"510300.SH": _indicator("510300.SH", 0.05)}
        rows = build_resonance(indicators, config)
        assert rows[0]["rising"] == 1
        assert rows[0]["total"] == 4
        assert rows[0]["resonance"] is False
