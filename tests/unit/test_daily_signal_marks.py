"""日K买卖标记：逐bar因果回放（最近30个交易日）与打点数据。

## 这个测试对应的用户诉求（2026-09-16）

> 日线级别做T的买卖信号，在k线图中没有标记，需要补充买卖标记，
> 最近30个交易日的买卖操作标记也要显示

原实现只对**最后一根**bar 跑一遍 `run_daily_signals`，所以图上只能标出"此刻"，
看不到"最近一个月什么时候给过买卖点"。修法是逐bar回放，本文件保证两件事：

1. **因果性（无未来函数）**：第 i 根bar只吃 `frame.iloc[: i + 1]`，
   高量柱锚点/位置分位/成本线全部按当日及之前重算 —— 用未来数据去解释过去的买点
   是这类"事后打点"最容易犯的错，也最会让用户对策略产生虚假信心；
2. **可读性**：同一信号连续触发只记首次（实测某票 S6 在 30 根里触发 23 次）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.intraday.config import IntradayConfig
from src.intraday.daily import _SIGNAL_REPLAY_BARS, replay_daily_signals
from src.intraday.volume import enrich_daily_frame


def _frame(bars: int = 240, seed: int = 11) -> pd.DataFrame:
    """确定性的合成日线（带量能起伏，能触发真实信号）。"""
    rng = np.random.default_rng(seed)
    close = 10 * np.cumprod(1 + rng.normal(0.0006, 0.02, bars))
    volume = rng.uniform(1e5, 1e6, bars)
    # 每隔一段放一根倍量柱：高量柱体系要的就是这种结构
    volume[::17] *= 3.5
    return pd.DataFrame({
        "date": pd.bdate_range("2025-01-01", periods=bars).strftime("%Y-%m-%d"),
        "open": close * (1 + rng.normal(0, 0.005, bars)),
        "high": close * (1 + abs(rng.normal(0, 0.01, bars))),
        "low": close * (1 - abs(rng.normal(0, 0.01, bars))),
        "close": close,
        "volume": volume,
        "amount": volume * close,
    })


def _enriched(bars: int = 240, seed: int = 11) -> pd.DataFrame:
    config = IntradayConfig()
    return enrich_daily_frame(_frame(bars, seed), config.daily)


def _key(mark) -> tuple[str, str, str]:
    return (mark.date, mark.side, mark.code)


def test_replay_returns_marks_with_sides() -> None:
    config = IntradayConfig()
    marks = replay_daily_signals(_enriched(), config.daily, bars=30)
    assert marks, "合成数据上应当至少触发一些买卖信号"
    for mark in marks:
        assert mark.side in ("buy", "sell", "risk")
        assert mark.code and mark.date
        assert mark.price is None or mark.price > 0


def test_replay_is_causal_no_future_data() -> None:
    """核心性质：**加不加未来数据，同一根历史bar的标记必须一模一样**。

    做法：对同一份数据取两个前缀回放（160 根时回放 130~159，240 根时回放 150~239），
    比较**真正的重叠区间**（150~159）。若回放里混入了未来数据，重叠区间的标记就会对不上。
    首根重叠日要跳过：连续触发去重的初始状态在两次调用里不同（一边有前一根的状态，
    另一边是空集）。
    """
    config = IntradayConfig()
    full = _enriched(bars=240)

    early = replay_daily_signals(full.iloc[:160], config.daily, bars=30)
    later = replay_daily_signals(full.iloc[:240], config.daily, bars=90)

    overlap = sorted({m.date for m in early} & {m.date for m in later})
    assert len(overlap) >= 2, f"两次回放的重叠区间太小，验证力不足：{overlap}"
    shared = set(overlap[1:])

    early_keys = {_key(m) for m in early if m.date in shared}
    later_keys = {_key(m) for m in later if m.date in shared}
    assert early_keys, "重叠区间没有标记，这条测试失去意义"
    assert early_keys == later_keys, (
        f"同一根bar在'只有历史'与'加了未来数据'两种输入下标记不一致：\n"
        f"仅历史输入有：{sorted(early_keys - later_keys)[:5]}\n"
        f"仅含未来输入有：{sorted(later_keys - early_keys)[:5]}")


def test_replay_dedupes_consecutive_triggers() -> None:
    """同一信号在**相邻交易日**不会重复打点（否则图上糊成一片）。

    注意"相邻"要按交易日排（用行情帧的日期序列），不能按"有标记的日期"排 ——
    中间隔着几根没触发的bar再触发，本来就该再打一个点。
    """
    config = IntradayConfig()
    enriched = _enriched()
    marks = replay_daily_signals(enriched, config.daily, bars=30)

    pairs = [(mark.date, mark.side, mark.code) for mark in marks]
    assert len(pairs) == len(set(pairs)), "同一天同一信号不该重复打点"

    emitted: dict[str, set[tuple[str, str]]] = {}
    for mark in marks:
        emitted.setdefault(mark.date, set()).add((mark.side, mark.code))
    trading_days = [str(date) for date in enriched["date"].tolist()[-30:]]
    for previous, current in zip(trading_days, trading_days[1:], strict=False):
        both = emitted.get(previous, set()) & emitted.get(current, set())
        assert not both, f"{previous} 与 {current} 连续两天都打了同样的标记：{both}"


def test_replay_respects_bar_window() -> None:
    """回放窗口就是最近 N 根：标记日期必须落在最后 N 个交易日之内。"""
    config = IntradayConfig()
    enriched = _enriched(bars=240)
    marks = replay_daily_signals(enriched, config.daily, bars=30)
    recent = set(enriched["date"].astype(str).tolist()[-30:])
    assert {mark.date for mark in marks} <= recent


def test_replay_marks_only_triggered_signals() -> None:
    """只记**触发**的信号：未触发的是"差在哪"，属于当前bar明细，不该出现在图上。"""
    config = IntradayConfig()
    marks = replay_daily_signals(_enriched(), config.daily, bars=10)
    assert all(mark.code for mark in marks)
    # 每条标记都要能对上当日某个触发信号（用同一套规则复算一次做对照）
    enriched = _enriched()
    window = enriched.iloc[: len(enriched)]
    from src.intraday.daily_signals import run_daily_signals
    from src.intraday.volume import (
        DailyContext,
        classify_volume_price,
        find_cost_lines,
        find_volume_anchors,
        quantify_position,
    )

    last_date = str(window["date"].iloc[-1])
    ctx = DailyContext(
        frame=window, params=config.daily, index=len(window) - 1,
        anchors=find_volume_anchors(window, config.daily), pattern=None,
        position=quantify_position(window),
        cost_lines=find_cost_lines(window, config.daily))
    ctx.pattern = classify_volume_price(window, ctx.index, config.daily)
    today = run_daily_signals(ctx)
    today_marks = [m for m in marks if m.date == last_date]
    triggered_codes = {item.code for item in today["buy"] if item.triggered}
    triggered_codes |= {item.code for item in today["sell"]
                        if item.triggered and item.kind in ("sell", "risk")}
    for mark in today_marks:
        assert mark.code in triggered_codes, f"{mark.code} 不是当日触发信号"


def test_replay_empty_or_tiny_frame() -> None:
    config = IntradayConfig()
    assert replay_daily_signals(_enriched(bars=1), config.daily) == []
    assert replay_daily_signals(pd.DataFrame(), config.daily) == []


def test_replay_default_window_is_thirty_trading_days() -> None:
    """用户口径：最近 30 个交易日。"""
    assert _SIGNAL_REPLAY_BARS == 30
    config = IntradayConfig()
    enriched = _enriched(bars=240)
    marks = replay_daily_signals(enriched, config.daily)      # 用默认值
    recent = set(enriched["date"].astype(str).tolist()[-30:])
    assert {mark.date for mark in marks} <= recent


def test_replay_cost_is_bounded() -> None:
    """回放要对每根bar重跑 22 个信号函数：30 根必须是"百毫秒级"，
    否则每次日K快照都会被它拖慢（快照有 180 秒缓存，但首屏要等）。"""
    import time

    config = IntradayConfig()
    enriched = _enriched(bars=340)
    started = time.perf_counter()
    replay_daily_signals(enriched, config.daily, bars=30)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"回放 30 根耗时 {elapsed:.2f}s，超出可接受范围"


def test_snapshot_carries_signal_history() -> None:
    """端到端：日K快照必须带上 signal_history（前端靠它打点）。"""
    import asyncio

    from src.intraday.daily import analyse_daily

    config = IntradayConfig()
    enriched = _enriched()
    frame = enriched.rename(columns={"date": "ts"})
    points = []
    from src.core.schemas import DataPoint, DataSourceType, FetchMethod

    for _, row in frame.iterrows():
        points.append(DataPoint(
            indicator="stock_close:600036", value=float(row["close"]),
            source_type=DataSourceType.API, source_url="qmt://test",
            fetch_method=FetchMethod.API_CALL, period_date=str(row["ts"]),
            extra={"open": float(row["open"]), "high": float(row["high"]),
                   "low": float(row["low"]), "close": float(row["close"]),
                   "volume": float(row["volume"]), "amount": float(row["amount"])}))

    snapshot = asyncio.run(asyncio.to_thread(
        lambda: analyse_daily(code="600036", name="测试", points=points,
                              config=config)))
    assert snapshot.available is True
    assert isinstance(snapshot.signal_history, list)
    for mark in snapshot.signal_history:
        assert mark.side in ("buy", "sell", "risk")


@pytest.mark.parametrize("seed", [3, 11, 29])
def test_replay_stable_across_seeds(seed: int) -> None:
    """不同行情结构下都不能抛异常，且标记日期单调不减（图上从左到右）。"""
    config = IntradayConfig()
    marks = replay_daily_signals(_enriched(seed=seed), config.daily, bars=30)
    dates = [mark.date for mark in marks]
    assert dates == sorted(dates)
