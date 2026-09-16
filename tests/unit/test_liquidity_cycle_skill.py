"""流动性周期skill纯函数测试（合成数据，不联网）。"""

from __future__ import annotations

from src.domain.skills.liquidity_cycle import (
    assess_liquidity,
    render_liquidity_hint,
)


def _turnover_hist(values: list[float]) -> list[dict]:
    return [
        {"indicator": "mkt:turnover:hist",
         "period_date": f"2026-{i // 28 + 6:02d}-{i % 28 + 1:02d}", "value": v}
        for i, v in enumerate(values)
    ]


def test_empty_points_only_gaps():
    a = assess_liquidity([])
    assert a["liquidity_phase"] is None
    assert a["suggested_position_pct"] is None
    assert len(a["data_gaps"]) >= 5  # 成交额/换手/两融/北向/估值
    assert a["fedwatch"]["status"] == "unavailable"


def test_active_market_phase_and_position_band():
    pts = _turnover_hist([15000] * 60) + [
        {"indicator": "mkt:turnover:total", "period_date": "2026-09-14",
         "value": 32000.0, "extra": {}},
    ]
    a = assess_liquidity(pts)
    assert a["liquidity_phase"].startswith("活跃市")
    assert a["suggested_position_pct"] == [70, 80]


def test_bull_bear_guard_alert_below_14_trillion():
    pts = _turnover_hist([13000] * 60) + [
        {"indicator": "mkt:turnover:total", "period_date": "2026-09-14",
         "value": 12000.0, "extra": {}},
    ]
    a = assess_liquidity(pts)
    assert "1.4万亿" in " ".join(a["risk_alerts"])
    assert a["liquidity_phase"].startswith("地量")
    assert a["suggested_position_pct"] == [20, 30]


def test_ma10_cross_above_ma50_signal():
    # 59日15000亿横盘，最新一日放量至30000亿 → 当日MA10上穿MA50
    values = [15000.0] * 59 + [30000.0]
    pts = _turnover_hist(values) + [
        {"indicator": "mkt:turnover:total", "period_date": "2026-09-14",
         "value": 17500.0, "extra": {}},
    ]
    a = assess_liquidity(pts)
    assert any("MA10上穿MA50" in s for s in a["signals"])


def test_concentration_guard_alert():
    pts = [
        {"indicator": "mkt:turnover_rate:all_a", "period_date": "2026-09-14",
         "value": 3.1, "extra": {"top5pct_concentration_pct": 44.6}},
    ]
    a = assess_liquidity(pts)
    assert any("45%" in w for w in a["risk_alerts"])
    assert a["turnover_rate"]["all_a_weighted_pct"] == 3.1


def test_margin_5d_drop_alert():
    hist = [
        {"indicator": "mkt:margin_balance:hist", "period_date": f"2026-09-{d:02d}",
         "value": v}
        for d, v in zip(
            range(5, 11), [20000, 19900, 19800, 19700, 19600, 19000], strict=True)
    ]
    latest = [{"indicator": "mkt:margin_balance", "period_date": "2026-09-11",
               "value": 19000.0, "extra": {"coverage": "沪深两市"}}]
    a = assess_liquidity(hist + latest)
    # 最新19000 vs 6期前20000 = -5%
    assert any("两融" in w for w in a["risk_alerts"])
    assert a["margin"]["change_5d_pct"] == -5.0


def test_northbound_halt_gap_and_valuation_extremes():
    pts = [
        {"indicator": "mkt:north_flow", "period_date": "2024-08-16",
         "value": 42.3, "extra": {"status": "disclosure_halted",
                                  "halted_since": "2024-08-19"}},
        {"indicator": "idx_val:snapshot:all", "period_date": "2026-09-12",
         "value": 25.0, "extra": {"index_name": "创业板指", "pb": 4.2,
                                  "pe_pct_5y": 12.0, "pb_pct_5y": 30.0}},
        {"indicator": "idx_val:snapshot:all", "period_date": "2026-09-12",
         "value": 12.0, "extra": {"index_name": "沪深300", "pb": 1.3,
                                  "pe_pct_5y": 92.0, "pb_pct_5y": 88.0}},
    ]
    a = assess_liquidity(pts)
    assert a["northbound"]["status"] == "disclosure_halted"
    assert any("北向" in g for g in a["data_gaps"])
    assert any("创业板指" in s and "偏冷" in s for s in a["signals"])
    assert any("沪深300" in w and "偏热" in w for w in a["risk_alerts"])
    assert {v["index_name"] for v in a["index_valuation"]} == {
        "创业板指", "沪深300"}


def test_fedwatch_probabilities_rendered():
    common = {"meeting_date": "2026-10-28", "current_target": "3.50%-3.75%",
              "cut_prob": 65.0, "hold_prob": 35.0, "hike_prob": 0.0,
              "dominant_range": "3.25%-3.50%"}
    pts = [
        {"indicator": "fed:rate_prob:next", "period_date": "2026-10-28",
         "value": 65.0, "extra": {**common, "rate_range": "3.25%-3.50%"}},
        {"indicator": "fed:rate_prob:next", "period_date": "2026-10-28",
         "value": 35.0, "extra": {**common, "rate_range": "3.50%-3.75%"}},
    ]
    a = assess_liquidity(pts)
    assert a["fedwatch"]["cut_prob_pct"] == 65.0
    text = render_liquidity_hint(a)
    assert "降息概率65.0%" in text


# ---------- 中观三市分项成交额（skill 1.1） ----------

def _total_point(value: float, extra: dict | None = None) -> dict:
    return {"indicator": "mkt:turnover:total", "period_date": "2026-09-14",
            "value": value, "extra": extra or {}}


def test_board_turnover_growth_active_style_bias():
    # 两市16000亿：沪5000、创业板4000(25%)、科创全板2500(15.6%)→双创40.6%≥40%
    pts = [
        _total_point(16000.0, {"sh": 5000.0, "sz": 11000.0,
                               "cyb": 4000.0, "kcb": 700.0}),
        {"indicator": "mkt:cybkcb:turnover:cyb", "period_date": "2026-09-14",
         "value": 4000.0},
        {"indicator": "mkt:cybkcb:turnover:kcb", "period_date": "2026-09-14",
         "value": 700.0},
        {"indicator": "mkt:cybkcb:turnover:kcb_all", "period_date": "2026-09-14",
         "value": 2500.0},
    ]
    a = assess_liquidity(pts)
    bt = a["board_turnover"]
    assert bt["shanghai_yi"] == 5000.0
    assert bt["chinext_yi"] == 4000.0
    assert bt["star50_yi"] == 700.0          # 科创50仅作权重观察
    assert bt["star_all_yi"] == 2500.0       # 科创板全板口径
    assert bt["chinext_share_pct"] == 25.0
    assert bt["star_share_pct"] == 15.6
    assert bt["growth_share_pct"] == 40.6
    assert any("成长风格活跃" in s for s in a["signals"])


def test_board_turnover_defense_style_bias():
    # 双创合计3000/16000=18.8%≤25% → 资金偏权重/红利防御
    pts = [
        _total_point(16000.0, {"sh": 9000.0}),
        {"indicator": "mkt:cybkcb:turnover:cyb", "period_date": "2026-09-14",
         "value": 2000.0},
        {"indicator": "mkt:cybkcb:turnover:kcb_all", "period_date": "2026-09-14",
         "value": 1000.0},
    ]
    a = assess_liquidity(pts)
    assert a["board_turnover"]["growth_share_pct"] == 18.8
    assert any("权重/红利防御" in s for s in a["signals"])


def test_board_turnover_falls_back_to_total_extra():
    # 无cybkcb连接器数据点时，用total extra里的cyb/kcb（kcb为科创50口径，全板缺失）
    pts = [_total_point(16000.0, {"sh": 7000.0, "cyb": 3000.0, "kcb": 600.0})]
    a = assess_liquidity(pts)
    bt = a["board_turnover"]
    assert bt["chinext_yi"] == 3000.0
    assert bt["star_all_yi"] is None         # extra只有科创50，无全板口径
    assert bt["star_share_pct"] is None


# ---------- 双创市场宽度 ----------

def test_market_breadth_strong_breadth_signal():
    pts = [_total_point(16000.0),
           {"indicator": "mkt:cybkcb:spot_summary", "period_date": "2026-09-14",
            "value": 64.08,
            "extra": {"stock_count": 2024, "up_count": 1297, "down_count": 678,
                      "flat_count": 49, "median_chg_pct": 0.64,
                      "total_turnover_yi": 6165.96}}]
    a = assess_liquidity(pts)
    mb = a["market_breadth"]
    assert mb["up_ratio_pct"] == 64.08
    assert mb["up_count"] == 1297 and mb["stock_count"] == 2024
    assert any("普涨结构健康" in s for s in a["signals"])


def test_market_breadth_freeze_signal():
    pts = [_total_point(16000.0),
           {"indicator": "mkt:cybkcb:spot_summary", "period_date": "2026-09-14",
            "value": 25.0,
            "extra": {"stock_count": 2024, "up_count": 500, "down_count": 1500,
                      "flat_count": 24, "median_chg_pct": -1.2}}]
    a = assess_liquidity(pts)
    assert any("情绪接近冰点" in s for s in a["signals"])


def test_market_breadth_fake_rally_risk_alert():
    # 上涨占比35%+中位数为负 → 权重拉指数、赚钱效应差
    pts = [_total_point(16000.0),
           {"indicator": "mkt:cybkcb:spot_summary", "period_date": "2026-09-14",
            "value": 35.0,
            "extra": {"stock_count": 2024, "up_count": 700, "down_count": 1300,
                      "flat_count": 24, "median_chg_pct": -0.8}}]
    a = assess_liquidity(pts)
    assert any("虚涨" in w for w in a["risk_alerts"])


# ---------- 双创板块PE分位 ----------

def test_board_valuation_hot_and_cold():
    pts = [
        {"indicator": "mkt:cybkcb:val:cyb_pe", "period_date": "2026-09-14",
         "value": 43.78, "extra": {"source": "乐咕乐股",
                                   "pct_1y": 15.0, "pct_3y": 18.0,
                                   "pct_5y": 12.0, "pct_all": 20.0}},
        {"indicator": "mkt:cybkcb:val:kcb_pe", "period_date": "2026-09-14",
         "value": 117.6, "extra": {"source": "乐咕乐股",
                                   "pct_1y": 88.4, "pct_3y": 96.1,
                                   "pct_5y": 97.7, "pct_all": 98.3}},
    ]
    a = assess_liquidity(pts)
    by_board = {v["board"]: v for v in a["board_valuation"]}
    assert by_board["创业板"]["pe"] == 43.78
    assert by_board["科创板"]["pct_5y"] == 97.7
    assert any("创业板" in s and "偏冷" in s for s in a["signals"])
    assert any("科创板" in w and "偏热" in w for w in a["risk_alerts"])


def test_board_valuation_proxy_without_percentile_no_alert():
    # 中证官网代理口径：只有现值PE无分位 → 展示但不产生偏热/偏冷预警
    pts = [
        {"indicator": "mkt:cybkcb:val:kcb_pe", "period_date": "2026-09-14",
         "value": 98.5,
         "extra": {"source": "中证指数官网",
                   "note": "中证官网科创50指数现值PE（科创板全板代理口径）"}},
    ]
    a = assess_liquidity(pts)
    assert a["board_valuation"][0]["pct_5y"] is None
    assert not any("科创板" in w for w in a["risk_alerts"])
    text = render_liquidity_hint(a)
    assert "代理口径" in text


def test_summary_text_contains_three_market_sections():
    pts = [
        _total_point(16000.0, {"sh": 7000.0, "cyb": 4000.0, "kcb": 600.0}),
        {"indicator": "mkt:cybkcb:turnover:cyb", "period_date": "2026-09-14",
         "value": 4000.0},
        {"indicator": "mkt:cybkcb:turnover:kcb_all", "period_date": "2026-09-14",
         "value": 2200.0},
        {"indicator": "mkt:cybkcb:spot_summary", "period_date": "2026-09-14",
         "value": 64.08,
         "extra": {"stock_count": 2024, "up_count": 1297, "down_count": 678,
                   "flat_count": 49, "median_chg_pct": 0.64}},
        {"indicator": "mkt:cybkcb:val:cyb_pe", "period_date": "2026-09-14",
         "value": 43.78, "extra": {"pct_5y": 42.1}},
    ]
    a = assess_liquidity(pts)
    text = a["summary_text"]
    assert "三市分项成交额" in text
    assert "沪市7000.0亿" in text
    assert "创业板4000.0亿" in text
    assert "科创板全板2200.0亿" in text
    assert "双创市场宽度" in text
    assert "双创板块估值" in text
    # 中观优先：三市分项必须出现在两融/宽基等段落之前（这里紧随总量）
    assert text.index("三市分项") < text.index("双创市场宽度")
