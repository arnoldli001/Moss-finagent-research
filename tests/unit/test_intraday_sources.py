"""做T模块 · 数据源解析单元测试（离线，不触网）。

本文件的核心价值是**锁住把最深兜底路径打断的那个 bug**：
2026-09-15 实测中「QMT未启动 + 腾讯网络抖动」时，新浪逐笔兜底抛
`KeyError: 'ts'` —— 逐笔原始帧的列是 ticktime/price/volume，没有 ts 列，
却被直接喂给了要求 ts 列的聚合函数，导致唯一能兜住的来源也失效。
"""

from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from src.core.exceptions import DataFetchError
from src.intraday import indicators as ind
from src.intraday.sources import (
    BARS_COLUMNS,
    SinaSource,
    TencentSource,
    _normalize_bars,
    _parse_quote_line,
    exchange_symbol,
)


def _tick_frame(day: str = "2026-09-15") -> pd.DataFrame:
    """新浪逐笔原始帧（列名与 akshare stock_intraday_sina 一致，无 ts 列）。"""
    frame = pd.DataFrame({
        "symbol": ["sz300308"] * 6,
        "name": ["中际旭创"] * 6,
        "ticktime": ["09:30:00", "09:31:00", "09:32:00",
                     "09:35:00", "09:36:00", "09:40:00"],
        "price": [100.0, 100.5, 100.2, 100.8, 101.0, 100.6],
        "volume": [1000.0, 500.0, 800.0, 1200.0, 300.0, 600.0],
        "prev_price": [0.0, 100.0, 100.5, 100.2, 100.8, 101.0],
        "kind": ["U"] * 6,
    })
    frame.attrs["date"] = day
    return frame


# ==================== 新浪逐笔兜底（回归：KeyError 'ts'） ====================


def test_sina_ticks_to_points_derives_ts_and_amount() -> None:
    points = SinaSource._ticks_to_points(_tick_frame())
    assert list(points.columns) == ["ts", "price", "volume", "amount"]
    assert points["ts"].iloc[0] == "2026-09-15 09:30"
    assert points["amount"].iloc[0] == pytest.approx(100.0 * 1000)


def test_sina_ticks_to_points_skips_invalid_price() -> None:
    frame = _tick_frame()
    frame.loc[0, "price"] = 0.0
    points = SinaSource._ticks_to_points(frame)
    assert len(points) == 5
    assert (points["price"] > 0).all()


def test_sina_ticks_to_points_empty_frame() -> None:
    assert SinaSource._ticks_to_points(_tick_frame().iloc[0:0]).empty


def test_sina_aggregate_to_bars_produces_ohlcv(monkeypatch) -> None:
    """修复回归：逐笔帧必须先归一化 ts 再聚合，输出完整 OHLCV。"""
    source = SinaSource()
    monkeypatch.setattr(source, "_load_ticks", staticmethod(
        lambda code: _tick_frame()))
    bars = asyncio_run(source.fetch_bars("300308", "5m", 1))
    assert not bars.empty
    assert list(bars.columns) == BARS_COLUMNS
    # 结束时刻命名：{09:30,09:31,09:32}→09:35；{09:35,09:36}→09:40；{09:40}→09:45
    assert list(bars["ts"]) == ["2026-09-15 09:35", "2026-09-15 09:40",
                                "2026-09-15 09:45"]
    first = bars.iloc[0]
    assert first["open"] == pytest.approx(100.0)
    assert first["close"] == pytest.approx(100.2)
    assert first["high"] == pytest.approx(100.5)
    assert first["low"] == pytest.approx(100.0)
    assert first["volume"] == pytest.approx(2300.0)
    assert bars["volume"].sum() == pytest.approx(4400.0)


def test_sina_bars_vwap_is_price_level_not_100x(monkeypatch) -> None:
    """兜底路径的VWAP必须与价格同量纲（曾出现均价飘到100倍价位的量纲坑）。"""
    source = SinaSource()
    monkeypatch.setattr(source, "_load_ticks", staticmethod(
        lambda code: _tick_frame()))
    bars = asyncio_run(source.fetch_bars("300308", "5m", 1))
    vwap = ind.last_value(ind.vwap_series(bars))
    assert vwap is not None
    assert 99.0 < vwap < 102.0


def test_sina_trend_aggregates_by_minute(monkeypatch) -> None:
    source = SinaSource()
    monkeypatch.setattr(source, "_load_ticks", staticmethod(
        lambda code: _tick_frame()))
    trend = asyncio_run(source.fetch_trend("300308"))
    assert list(trend.columns) == ["ts", "price", "avg_price", "volume", "amount"]
    assert len(trend) == 6  # 6笔逐笔落在6个不同分钟
    assert trend["avg_price"].iloc[-1] == pytest.approx(
        (100.0 * 1000 + 100.5 * 500 + 100.2 * 800 + 100.8 * 1200
         + 101.0 * 300 + 100.6 * 600) / 4400, abs=0.01)


def test_sina_raises_when_no_trading_day(monkeypatch) -> None:
    def _boom(code: str):
        raise DataFetchError("新浪逐笔无可取交易日")

    monkeypatch.setattr(SinaSource, "_load_ticks", staticmethod(_boom))
    with pytest.raises(DataFetchError):
        asyncio_run(SinaSource().fetch_bars("300308", "5m", 1))


def test_sina_rejects_unsupported_period(monkeypatch) -> None:
    monkeypatch.setattr(SinaSource, "_load_ticks", staticmethod(
        lambda code: _tick_frame()))
    with pytest.raises(DataFetchError, match="不支持周期"):
        asyncio_run(SinaSource().fetch_bars("300308", "3m", 1))


# ==================== 多日聚合（跨日不得共用日期标签） ====================


def test_aggregate_to_bars_handles_multiple_days() -> None:
    points = pd.DataFrame({
        "ts": ["2026-09-14 09:30", "2026-09-14 09:31",
               "2026-09-15 09:30", "2026-09-15 09:31"],
        "price": [10.0, 10.1, 20.0, 20.1],
        "volume": [1.0, 1.0, 1.0, 1.0],
        "amount": [10.0, 10.1, 20.0, 20.1],
    })
    bars = ind.aggregate_to_bars(points, minutes=5)
    assert list(bars["ts"]) == ["2026-09-14 09:35", "2026-09-15 09:35"]
    assert bars["close"].iloc[0] == pytest.approx(10.1)
    assert bars["close"].iloc[1] == pytest.approx(20.1)


# ==================== bar 表归一与快照解析 ====================


def test_normalize_bars_dedupes_and_sorts() -> None:
    frame = pd.DataFrame({
        "ts": ["2026-09-15 09:40", "2026-09-15 09:35", "2026-09-15 09:40"],
        "open": [1.0, 2.0, 9.0], "high": [1.0, 2.0, 9.0],
        "low": [1.0, 2.0, 9.0], "close": [1.0, 2.0, 9.0],
        "volume": [1.0, 1.0, 1.0], "amount": [1.0, 1.0, 1.0],
    })
    out = _normalize_bars(frame)
    assert list(out["ts"]) == ["2026-09-15 09:35", "2026-09-15 09:40"]
    # 重复时间戳保留最后一条
    assert out["close"].iloc[-1] == pytest.approx(9.0)


def test_normalize_bars_fills_missing_columns() -> None:
    out = _normalize_bars(pd.DataFrame({"ts": ["2026-09-15 09:35"],
                                        "close": [10.0]}))
    assert list(out.columns) == BARS_COLUMNS
    assert out["volume"].iloc[0] == 0.0


def test_normalize_bars_empty() -> None:
    assert _normalize_bars(pd.DataFrame()).empty
    assert _normalize_bars(None).empty


def test_parse_quote_line_extracts_fields() -> None:
    parts = ["51", "中际旭创", "300308", "864.01", "873.00", "873.38"]
    parts += [""] * (88 - len(parts))
    parts[31], parts[32] = "-8.99", "-1.03"
    parts[36], parts[37] = "179554", "1561359"
    parts[38], parts[39] = "1.62", "49.76"
    parts[41], parts[42] = "883.73", "858.00"
    parts[46], parts[47] = "25.53", "1047.60"
    line = f'v_sz300308="{"~".join(parts)}";'
    quote = _parse_quote_line(line)
    assert quote is not None
    assert quote.code == "300308"
    assert quote.name == "中际旭创"
    assert quote.price == pytest.approx(864.01)
    assert quote.pe_ttm == pytest.approx(49.76)
    assert quote.pb == pytest.approx(25.53)
    assert quote.change_pct == pytest.approx(-1.03)


def test_parse_quote_line_rejects_bad_input() -> None:
    assert _parse_quote_line("") is None
    assert _parse_quote_line('v_sz1="1~2"') is None  # 字段不足
    parts = ["51", "X", "000001", "0"] + [""] * 84
    assert _parse_quote_line(f'v_x="{"~".join(parts)}";') is None  # 价格无效


def test_tencent_period_map_covers_configured_periods() -> None:
    assert TencentSource.period_map["5m"] == "m5"
    assert TencentSource.period_map["30m"] == "m30"


def test_source_failure_message_lists_every_attempt() -> None:
    """全源失败时的错误信息必须列出每个源的失败原因（便于定位是哪一层挂了）。

    2026-09 起 `IntradayConfig` 默认 `qmt_enabled=False`，所以这里显式打开，
    把一个**完整的四源池**都放进错误信息 —— 排查时最怕"少了一个源却看不出来"。
    """
    from src.intraday.config import IntradayConfig
    from src.intraday.sources import IntradayDataProvider

    config = IntradayConfig()
    config.data.qmt_enabled = True
    provider = IntradayDataProvider(config)

    async def _fail(source, method, code, period, days, min_date=""):
        from src.intraday.models import SourceAttempt
        return None, SourceAttempt(source=f"src-{source}", ok=False,
                                   detail=f"{source} 挂了")

    provider._try_source = _fail  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(DataFetchError) as excinfo:
        asyncio_run(provider.fetch_bars("300308", days=1))
    message = str(excinfo.value)
    for source in ("qmt", "tencent", "eastmoney", "sina"):
        assert f"src-{source}" in message


def test_default_config_excludes_qmt_from_failure_message() -> None:
    """QMT 默认关闭：它的失败原因不该再出现在错误信息里（它根本没被调用）。"""
    from src.intraday.config import IntradayConfig
    from src.intraday.sources import IntradayDataProvider

    provider = IntradayDataProvider(IntradayConfig())

    async def _fail(source, method, code, period, days, min_date=""):
        from src.intraday.models import SourceAttempt
        return None, SourceAttempt(source=f"src-{source}", ok=False,
                                   detail=f"{source} 挂了")

    provider._try_source = _fail  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(DataFetchError) as excinfo:
        asyncio_run(provider.fetch_bars("300308", days=1))
    message = str(excinfo.value)
    assert "src-qmt" not in message
    for source in ("tencent", "eastmoney", "sina"):
        assert f"src-{source}" in message


def test_exchange_symbol_used_by_sina_source() -> None:
    assert exchange_symbol("300308") == "sz300308"
    assert exchange_symbol("600110") == "sh600110"


# ==================== QMT 时间口径（实测踩坑：整体早 8 小时） ====================

def test_qmt_frame_to_bars_uses_local_index_not_utc_epoch() -> None:
    """QMT 的 `time` 列是 UTC 基准 epoch 毫秒，索引才是本地时间。

    实测：当日最后一根 1 分钟 bar 的 time=1789455600000 直接按 unit="ms" 解释
    得到 07:00（真实 15:00），分时图 X 轴整体早 8 小时。必须优先用索引。
    """
    from src.intraday.sources import QmtMinuteSource

    frame = pd.DataFrame(
        {"time": [1789455480000, 1789455600000],
         "open": [38.12, 38.16], "high": [38.12, 38.16],
         "low": [38.12, 38.16], "close": [38.12, 38.16],
         "volume": [16.0, 17467.0], "amount": [60992.0, 66654072.0]},
        index=["20260915145800", "20260915150000"])
    bars = QmtMinuteSource._frame_to_bars(frame)
    assert bars["ts"].tolist() == ["2026-09-15 14:58", "2026-09-15 15:00"]
    assert "07:00" not in bars["ts"].tolist()


def test_qmt_frame_to_bars_falls_back_to_utc_ms_conversion() -> None:
    """索引不可用时也必须做 UTC→北京换算，而不是把 UTC 当本地时间。"""
    from src.intraday.sources import QmtMinuteSource

    frame = pd.DataFrame(
        {"time": [1789455600000], "open": [38.16], "high": [38.16],
         "low": [38.16], "close": [38.16], "volume": [17467.0],
         "amount": [66654072.0]}, index=[0])
    bars = QmtMinuteSource._frame_to_bars(frame)
    assert bars["ts"].tolist() == ["2026-09-15 15:00"]


@pytest.mark.parametrize(("raw", "expected"), [
    ("20260915150000", "2026-09-15 15:00"),
    (20260915150000, "2026-09-15 15:00"),
    ("202609151500", "2026-09-15 15:00"),
    ("20260915", "2026-09-15"),
    ("2026-09-15 15:00:00", "2026-09-15 15:00"),
    (None, None),
    ("nat", None),
    ("abc", None),
])
def test_qmt_ts_text_parses_supported_shapes(raw, expected) -> None:
    from src.intraday.sources import _qmt_ts_text

    assert _qmt_ts_text(raw) == expected


def test_ms_to_beijing_is_fixed_offset_not_system_tz() -> None:
    """换算用固定 +8：服务器时区若是 UTC 也必须得到北京时间。"""
    from src.intraday.sources import _ms_to_beijing

    assert _ms_to_beijing(1789455600000) == "2026-09-15 15:00"
    assert _ms_to_beijing(0) == "1970-01-01 08:00"


def test_timezone_fix_changes_labels_only_not_values() -> None:
    """回归护栏：ts 整体平移 8 小时只换标签，不改变任何指标数值。

    这条不变式说明「QMT 时间口径修复」不会偷偷改分：实测把真实 bars 平移 -8h 后，
    5分钟特征（VWAP/布林/带宽/MACD）与 30分钟 KDJ 的数值逐行完全相等
    （最大差异 0.0，NaN 位置一致），只有 ts 标签不同。
    """
    from src.intraday.config import IntradayConfig
    from src.intraday.features import build_intraday_features, resample_bars
    from src.intraday.service import _kdj_frame

    config = IntradayConfig()
    # 两个交易日 × 48 根 5分钟 bar（09:35~15:00 口径，含午休缺口）
    stamps: list[str] = []
    for day in ("2026-09-14", "2026-09-15"):
        for minute in list(range(575, 691, 5)) + list(range(785, 901, 5)):
            stamps.append(f"{day} {minute // 60:02d}:{minute % 60:02d}")
    closes = [10.0 + (index % 17) * 0.03 for index in range(len(stamps))]
    frame = pd.DataFrame({
        "ts": stamps, "open": closes, "high": [c + 0.02 for c in closes],
        "low": [c - 0.02 for c in closes], "close": closes,
        "volume": [1000.0 + index for index in range(len(stamps))],
        "amount": [1.0e7 + index for index in range(len(stamps))],
    })

    correct = build_intraday_features(frame, config)
    shifted = frame.copy()
    shifted["ts"] = (pd.to_datetime(shifted["ts"]) - pd.Timedelta(hours=8)
                     ).dt.strftime("%Y-%m-%d %H:%M")
    wrong = build_intraday_features(shifted, config)

    columns = ["vwap", "dev_z", "dev_pct", "pct_b", "bandwidth", "bw_pctl"]
    left = correct[columns].to_numpy(dtype=float)
    right = wrong[columns].to_numpy(dtype=float)
    assert left.shape == right.shape
    assert (correct["ts"] != wrong["ts"]).all(), "标签必须不同（否则没测到东西）"
    import numpy as np

    assert np.array_equal(np.isnan(left), np.isnan(right))
    valid = ~np.isnan(left)
    assert float(abs(left[valid] - right[valid]).max()) < 1e-9

    kdj_left = _kdj_frame(resample_bars(correct, 30), config)
    kdj_right = _kdj_frame(resample_bars(wrong, 30), config)
    assert len(kdj_left) == len(kdj_right)
    assert float((kdj_left["k"] - kdj_right["k"]).abs().max()) < 1e-9


def asyncio_run(coro):
    """轻量 asyncio.run 包装（这些用例是一次性协程，无需 pytest-asyncio 的 loop 装置）。"""
    return asyncio.run(coro)
