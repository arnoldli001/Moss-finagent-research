"""全市场成交额预测量能（`market_amount.py`）单测。

覆盖三件事：
  1. **同时间同比**的正确性（含"昨日同时刻"而不是"按时间线性外推"）；
  2. 用户阈值的边界：「预测 < 2 万亿」与「缩量 > 1000 亿」；
  3. 数据缺口时的行为：京市曲线无成交额、市场缺失、曲线为空。
"""

from __future__ import annotations

import pytest

from src.intraday.market_amount import (
    CONTRACTION_DELTA,
    CONTRACTION_FLOOR,
    MarketTurnoverProvider,
    build_forecast,
    classify_turnover,
    parse_curve,
    wan_yi,
    yi,
)


def test_parse_curve_keeps_amount_and_volume():
    rows = [
        "0930 3951.37 3982082 6255472603.00",
        "0931 3947.48 20897821 35942263364.60",
    ]
    amounts, volumes = parse_curve(rows)
    assert amounts["0930"] == pytest.approx(6255472603.00)
    assert volumes["0930"] == pytest.approx(3982082)
    assert amounts["0931"] == pytest.approx(35942263364.60)


def test_parse_curve_tolerates_missing_amount_field():
    """京市（bj899050）的点只有 3 个字段 —— 不能因此丢掉这条曲线。"""
    amounts, volumes = parse_curve(["0930 1049.11 103907", "0931 1049.20 210000"])
    assert amounts == {}
    assert volumes["0930"] == pytest.approx(103907)


def test_parse_curve_skips_malformed_rows():
    amounts, volumes = parse_curve(["", "0930", "abc def ghi", "0931 1.0 5 9"])
    assert volumes == {"0931": 5.0}
    assert amounts == {"0931": 9.0}


# ---------------------------------------------------------------- 核心公式

def test_projection_uses_same_time_yesterday_not_elapsed_time():
    """口径必须是「昨日同时刻」，不是「累计 ÷ 已交易时间占比」。

    构造一个**U 型**的昨日曲线（0930 那个点就吃掉全天一半）：

    - 昨日：0930 = 1.1 万亿，1030 = 1.1 万亿，1500（全天）= 2.2 万亿；
    - 今日 1030 = 0.99 万亿（正好是昨日同时刻的 90%）。

    同时间同比 → 全天预测 = 2.2 万亿 × 0.9 = 1.98 万亿（缩量 2,200 亿）。
    若误用时间线性外推（1030 已过 1/4 时间）→ 0.99/0.25 = 3.96 万亿，方向都反了。
    """
    forecast = build_forecast(
        today_curves={"sh": {"0930": 0.18e12, "1030": 0.99e12}},
        prev_curves={"sh": {"0930": 1.1e12, "1030": 1.1e12, "1500": 2.2e12}},
    )
    assert forecast.moment == "1030"
    assert forecast.projected_amount == pytest.approx(1.98e12)
    assert forecast.delta_amount == pytest.approx(-0.22e12)
    assert forecast.verdict == "缩量"


def test_projection_is_stable_across_intraday_moments():
    """同一"量能节奏"下，不同时刻推出的全天预测应当一致。

    这是这个口径最重要的性质（也是"开盘就能推理出来"的依据）：
    构造昨日曲线的累计占比 10%/30%/50%/80%，今日全程是昨日的 0.9 倍
    → 四个时刻都应推出 1.98 万亿。若用时间线性外推，四个时刻会给出四个不同的值。

    ⚠️ 量级必须高于用户设的 2 万亿地板，否则地板条件会无条件命中，
    断言验不出公式本身（实测踩过：两个错误互相抵消，测试反而是绿的）。
    """
    prev = {"sh": {"0930": 0.22e12, "1030": 0.66e12, "1130": 1.1e12,
                   "1400": 1.76e12, "1500": 2.2e12}}
    for moment, ratio in (("0930", 0.1), ("1030", 0.3), ("1130", 0.5), ("1400", 0.8)):
        today = {"sh": {moment: 2.2e12 * ratio * 0.9}}
        forecast = build_forecast(today_curves=today, prev_curves=prev)
        assert forecast.projected_amount == pytest.approx(1.98e12), moment
        assert forecast.verdict == "缩量"


def test_three_markets_are_summed():
    """三市口径：合计 = 各市场预测量之和，且三个市场都进 `markets_used`。"""
    today = {
        "sh": {"1030": 0.88e12},
        "sz": {"1030": 0.88e12},
    }
    prev = {
        "sh": {"1030": 1.1e12, "1500": 2.2e12},
        "sz": {"1030": 1.1e12, "1500": 2.2e12},
    }
    # 京市：曲线只有量、没有额 → 走 bse_prev_total + 它自己的量比
    forecast = build_forecast(
        today_curves=today, prev_curves=prev,
        today_volumes={**today, "bj": {"1030": 400.0}},
        prev_volumes={**prev, "bj": {"1030": 500.0, "1500": 1000.0}},
        bse_prev_total=200e8,
    )
    assert forecast.markets_used == ["沪市", "深市", "京市"]
    # 沪深各 2.2 万亿 × 0.88/1.1 = 1.76 万亿；京市 200 亿 × (400/500) = 160 亿
    assert forecast.projected_amount == pytest.approx(1.76e12 + 1.76e12 + 160e8)
    assert forecast.prev_total_amount == pytest.approx(2.2e12 + 2.2e12 + 200e8)
    # 今日累计额 / 昨日同时刻额只有沪深有（京市没有成交额曲线）
    assert forecast.today_amount == pytest.approx(1.76e12)
    assert forecast.prev_same_time_amount == pytest.approx(2.2e12)

def test_bse_folded_by_its_own_volume_ratio_and_reported_in_notes():
    """京市没有成交额曲线时必须**如实记 note**，且用它自己的量比（不是沪深额比）。"""
    forecast = build_forecast(
        today_curves={"sh": {"1030": 1.76e12}},
        prev_curves={"sh": {"1030": 2.2e12, "1500": 4.4e12}},
        today_volumes={"sh": {"1030": 1.76e12}, "bj": {"1030": 400.0}},
        prev_volumes={"sh": {"1030": 2.2e12, "1500": 4.4e12},
                      "bj": {"1030": 500.0, "1500": 1000.0}},
        bse_prev_total=200e8,
    )
    assert forecast.markets_used == ["沪市", "京市"]
    # 京市 = 200 亿 × (400/500) = 160 亿；**不**再乘沪深的额比（1.76/2.2 = 0.8），
    # 否则就变成 128 亿 —— 那等于把沪深的价格效应算到京市头上。
    assert forecast.projected_amount == pytest.approx(3.52e12 + 160e8)
    assert any("京市" in note and "量比" in note for note in forecast.notes)


def test_bse_omitted_when_prev_total_unknown():
    """京市昨日全天额拿不到时：少算一个市场，并明确写出未计入。

    ⚠️ 京市那条说明由**取数层**写成（"未计入同时间同比：腾讯只给当日成交额…"），
    所以纯函数这里只保证"不进合计"，不在 `notes` 里重复一遍。
    """
    forecast = build_forecast(
        today_curves={"sh": {"1030": 1.76e12}},
        prev_curves={"sh": {"1030": 2.2e12, "1500": 4.4e12}},
    )
    assert forecast.markets_used == ["沪市"]
    assert "京市" not in forecast.markets_used
    # 沪深的其它市场（若有）仍要如实标出
    forecast2 = build_forecast(
        today_curves={"sh": {"1030": 1.76e12}},
        prev_curves={"sh": {"1030": 2.2e12, "1500": 4.4e12},
                     "sz": {"1030": 2.2e12, "1500": 4.4e12}},
    )
    assert "深市" not in forecast2.markets_used
    assert any("深市" in note for note in forecast2.notes)


def test_moment_falls_back_to_a_shared_timestamp():
    """今日最后一点昨日没有时，往前退到最近一个两边都有的时刻。"""
    forecast = build_forecast(
        today_curves={"sh": {"0930": 0.22e12, "1030": 0.55e12, "1031": 0.57e12}},
        prev_curves={"sh": {"0930": 0.44e12, "1030": 1.1e12, "1500": 2.2e12}},
    )
    assert forecast.moment == "1030"
    assert forecast.projected_amount == pytest.approx(1.1e12)   # 2.2 × 0.55/1.1


def test_empty_curves_record_gap():
    forecast = build_forecast(today_curves={}, prev_curves={})
    assert forecast.available is False
    assert forecast.gap
    assert forecast.projected_amount is None


# ---------------------------------------------------------------- 阈值边界

def test_classify_uses_floor_of_two_trillion():
    """预测低于 2 万亿 → 缩量。"""
    verdict, delta, allowed, reasons = classify_turnover(
        projected_amount=1.95e12, prev_total_amount=1.85e12)
    assert verdict == "缩量"
    assert allowed is False
    assert any("2 万亿" in reason for reason in reasons)
    # 低于地板、但比昨日**放量**：仍然判缩量（地板是独立的一条）
    verdict, delta, allowed, reasons = classify_turnover(
        projected_amount=1.95e12, prev_total_amount=1.80e12)
    assert verdict == "缩量"
    assert delta == pytest.approx(0.15e12)
    assert any("2 万亿" in reason for reason in reasons)


def test_classify_uses_contraction_delta_of_1000yi():
    """缩量未到 1000 亿 → 不算缩量（用户口径是"缩量 1000 亿以上"）。"""
    verdict, _delta, allowed, _ = classify_turnover(
        projected_amount=2.30e12, prev_total_amount=2.39e12)
    assert verdict == "放量"
    assert allowed is True


def test_classify_flags_contraction_over_1000yi():
    verdict, delta, allowed, reasons = classify_turnover(
        projected_amount=2.30e12, prev_total_amount=2.41e12)
    assert verdict == "缩量"
    assert allowed is False
    assert delta == pytest.approx(-1100e8)
    assert any("1000 亿" in reason for reason in reasons)


def test_classify_exactly_at_thresholds_is_not_contraction():
    """边界值取"严格小于/大于"：正好 2 万亿、正好缩 1000 亿都不算缩量。

    ⚠️ 两条边界要**分别**验：若第一条用 2.6 万亿当昨日，缩量差就有 6000 亿，
    命中的是"缩量 > 1000 亿"那条（不是地板那条），等于没验到地板。
    所以昨日取 2.1 万亿 —— 缩量差正好 1000 亿，两条条件都不触发。
    """
    verdict, delta, allowed, _ = classify_turnover(
        projected_amount=CONTRACTION_FLOOR, prev_total_amount=2.1e12)
    assert verdict == "放量" and allowed is True
    assert delta == pytest.approx(-1000e8)     # 缩量差 < 1000 亿 → 不算缩量
    verdict, delta, allowed, _ = classify_turnover(
        projected_amount=2.3e12, prev_total_amount=2.3e12 + CONTRACTION_DELTA)
    assert verdict == "放量" and allowed is True
    assert delta == pytest.approx(-CONTRACTION_DELTA)


def test_classify_without_prev_total_does_not_fabricate():
    verdict, delta, allowed, reasons = classify_turnover(
        projected_amount=2.5e12, prev_total_amount=None)
    assert verdict == "平量"
    assert delta is None
    assert allowed is True
    assert reasons


def test_classify_without_projection_returns_empty():
    assert classify_turnover(projected_amount=None, prev_total_amount=1e12) == (
        "", None, True, [])


# ---------------------------------------------------------------- verdict_text

def test_verdict_text_wording_matches_user_request():
    """用户口径原文：缩量XX亿不追高 / 放量XX亿可做T。

    ⚠️ 用 2.2 万亿（高于地板）做基准，否则命中的是"低于 2 万亿"那条，
    放量用例会被地板遮成"缩量"（见本文件核心公式段的说明）。
    """
    contraction = build_forecast(
        today_curves={"sh": {"1030": 0.55e12}},
        prev_curves={"sh": {"1030": 1.1e12, "1500": 2.2e12}},
    )
    # 0.55/1.1 = 0.5 → 预测 1.1 万亿，比昨日 2.2 万亿缩 1.1 万亿
    assert contraction.projected_amount == pytest.approx(1.1e12)
    assert contraction.verdict == "缩量"
    assert contraction.verdict_text == "缩量 11,000 亿 不追高"
    assert contraction.chase_allowed is False
    assert contraction.t_allowed is False

    expansion = build_forecast(
        today_curves={"sh": {"1030": 1.32e12}},
        prev_curves={"sh": {"1030": 1.1e12, "1500": 2.2e12}},
    )
    assert expansion.verdict == "放量"
    assert expansion.verdict_text == "放量 4,400 亿 可做T"
    assert expansion.chase_allowed is True


def test_verdict_text_contraction_wording_uses_floor_too():
    """命中"低于 2 万亿"这条时给出的也是「缩量 XX 亿 不追高」。"""
    forecast = build_forecast(
        today_curves={"sh": {"1030": 0.9e12}},
        prev_curves={"sh": {"1030": 1.0e12, "1500": 1.8e12}},
    )
    assert forecast.projected_amount == pytest.approx(1.62e12)   # 1.8 × 0.9/1.0
    assert forecast.projected_amount < CONTRACTION_FLOOR
    assert forecast.verdict == "缩量"
    assert forecast.verdict_text == "缩量 1,800 亿 不追高"
    assert any("2 万亿" in reason for reason in forecast.reasons)


def test_helpers_format_units():
    assert yi(1.2345e10) == "123"
    assert yi(None) == "—"
    assert wan_yi(2.3456e12) == "2.35"
    assert wan_yi(None) == "—"


# ---------------------------------------------------------------- 取数层（无网络）

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """按 code 返回预置的 day/query 响应；同时覆盖 fqkline（京市昨日额）。"""

    def __init__(self, day_payloads, fqkline_payload=None):
        self._days = day_payloads
        self._fq = fqkline_payload
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, params=None):
        self.calls.append((url, params or {}))
        if "day/query" in url:
            return _FakeResponse(self._days[params["code"]])
        return _FakeResponse(self._fq or {})


def _day_payload(symbol, days):
    return {"data": {symbol: {"data": [
        {"date": date, "data": rows} for date, rows in days]}}}


#: 三市两天的真实量级曲线（元）：今日是昨日的 80%（缩量 20%）。
#: 昨日全天沪/深各 1 万亿、京市 200 亿；同时刻（0930）累计占比 50% / 10%。
_AMOUNT_SH = [("20260922", ["0930 1 100 5000e8", "1500 1 500 10000e8"]),
              ("20260923", ["0930 1 80 4000e8"])]
_AMOUNT_SZ = [("20260922", ["0930 1 100 5000e8", "1500 1 500 10000e8"]),
              ("20260923", ["0930 1 80 4000e8"])]
#: 京市：点只有 3 个字段（无成交额）—— 今日量是昨日的 80%。
_VOLUME_BJ = [("20260922", ["0930 1 10", "1500 1 100"]),
              ("20260923", ["0930 1 8"])]

_FQKLINE_BJ = {"data": {"bj899050": {
    "day": [["2026-09-22", "1000", "1000", "1000", "1000", "100"],
            ["2026-09-23", "1000", "1000", "1000", "1000", "80"]],
    # `qt[37]` 是**今日成交额（万元）**。这里取 160 亿 = 1,600,000 万元，
    # 于是取数层按成交量之比反推的"昨日全天额" = 160 亿 × 100/80 = 200 亿，
    # 正好与 `_VOLUME_BJ` 的 0.8 量比自洽（不这样对齐的话，测试断言会变成
    # "两个错误相互抵消"，验不出东西）。
    "qt": {"bj899050": [""] * 37 + ["1600000"]},
}}}


@pytest.mark.asyncio
async def test_provider_fetches_all_three_markets():
    client = _FakeClient({
        "sh000001": _day_payload("sh000001", _AMOUNT_SH),
        "sz399001": _day_payload("sz399001", _AMOUNT_SZ),
        "bj899050": _day_payload("bj899050", _VOLUME_BJ),
    }, fqkline_payload=_FQKLINE_BJ)
    provider = MarketTurnoverProvider(client=client)
    forecast = await provider.snapshot()
    assert forecast.available is True
    assert forecast.trade_date == "20260923"
    assert forecast.prev_date == "20260922"
    # 沪深各 10000 亿 × 4000/5000 = 8000 亿；京市昨日额 200 亿 × (8/10) = 160 亿
    assert forecast.projected_amount == pytest.approx(8000e8 * 2 + 160e8)
    assert "京市" in "".join(forecast.notes)


@pytest.mark.asyncio
async def test_provider_gap_when_all_markets_fail():
    client = _FakeClient({"sh000001": {}, "sz399001": {}, "bj899050": {}})
    provider = MarketTurnoverProvider(client=client)
    forecast = await provider.snapshot()
    assert forecast.available is False
    assert forecast.gap


@pytest.mark.asyncio
async def test_provider_caches_within_ttl():
    client = _FakeClient({
        "sh000001": _day_payload("sh000001", _AMOUNT_SH),
        "sz399001": _day_payload("sz399001", _AMOUNT_SZ),
        "bj899050": _day_payload("bj899050", _VOLUME_BJ),
    }, fqkline_payload=_FQKLINE_BJ)
    provider = MarketTurnoverProvider(client=client, cache_ttl=60.0)
    first = await provider.snapshot()
    calls_after_first = len(client.calls)
    second = await provider.snapshot()
    assert second is first
    assert len(client.calls) == calls_after_first      # 没再打网络
    third = await provider.snapshot(force=True)
    assert len(client.calls) > calls_after_first
    assert third is not first


@pytest.mark.asyncio
async def test_snapshot_cached_never_triggers_network():
    client = _FakeClient({})
    provider = MarketTurnoverProvider(client=client)
    assert provider.snapshot_cached() is None
    assert client.calls == []
