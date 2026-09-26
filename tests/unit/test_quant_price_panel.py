"""价格面板单测（离线，注入假取数器，不触 QMT）。

重点锁「增量更新」的语义：判断依据必须是**已覆盖的日期区间**，
而不是"文件存在与否" —— 否则"上次只拉了 2025、这次要 2020 起"会被误判命中，
静默少掉 5 年数据（这类缺陷在回测里表现为"样本量莫名偏小"）。
"""
from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from src.quant.price_panel import (
    BAR_PRICE_FIELDS,
    PriceStore,
    _points_to_frame,
    price_panel_summary,
)


def _frame(start: str, end: str) -> pd.DataFrame:
    dates = pd.bdate_range(start, end).strftime("%Y%m%d").tolist()
    return pd.DataFrame({
        "date": dates,
        "open": [10.0] * len(dates), "high": [10.5] * len(dates),
        "low": [9.8] * len(dates), "close": [10.2] * len(dates),
        "volume": [1.0e6] * len(dates), "amount": [1.0e7] * len(dates),
    })


class _FakeFetcher:
    """记录每次取数调用，返回确定性日线。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fail: set[str] = set()
        self.empty: set[str] = set()

    async def __call__(self, code: str, start: str, end: str) -> pd.DataFrame:
        self.calls.append((code, start, end))
        if code in self.fail:
            raise RuntimeError("QMT 挂了")
        if code in self.empty:
            return pd.DataFrame(columns=["date", *BAR_PRICE_FIELDS])
        return _frame(start, end)


# ==================== DataPoint → DataFrame ====================


class _Point:
    def __init__(self, date: str, close: float, **extra) -> None:
        self.period_date = date
        self.value = close
        self.extra = extra


def test_points_to_frame_builds_ohlcv() -> None:
    points = [
        _Point("2026-09-14", 10.2, open=10.0, high=10.5, low=9.8,
               volume=1.0e6, amount=1.0e7),
        _Point("2026-09-15", 10.4, open=10.2, high=10.6, low=10.1,
               volume=1.2e6, amount=1.2e7),
    ]
    frame = _points_to_frame(points)
    assert frame["date"].tolist() == ["20260914", "20260915"]
    assert frame["close"].tolist() == [10.2, 10.4]
    assert frame["amount"].tolist() == [1.0e7, 1.2e7]
    assert list(frame.columns) == ["date", *BAR_PRICE_FIELDS]


def test_points_to_frame_dedupes_and_sorts() -> None:
    points = [_Point("2026-09-15", 1.0), _Point("2026-09-14", 2.0),
              _Point("2026-09-15", 3.0)]
    frame = _points_to_frame(points)
    assert frame["date"].tolist() == ["20260914", "20260915"]
    assert frame["close"].tolist() == [2.0, 3.0]


def test_points_to_frame_empty() -> None:
    assert _points_to_frame([]).empty


# ==================== 同步 / 增量 ====================


def test_sync_fetches_then_skips_covered_range(tmp_dir) -> None:
    fetcher = _FakeFetcher()
    store = PriceStore(tmp_dir, fetcher=fetcher)
    first = asyncio.run(store.sync(["600519", "300750"], "20260901", "20260915"))
    assert sorted(first.fetched) == ["300750", "600519"]
    assert len(fetcher.calls) == 2

    second = asyncio.run(store.sync(["600519", "300750"], "20260901", "20260915"))
    assert second.skipped == ["600519", "300750"]
    assert second.fetched == []
    assert len(fetcher.calls) == 2, "已覆盖区间不应再取数"


def test_sync_refetches_when_range_extends(tmp_dir) -> None:
    """区间变长（要更早的历史）必须重新取数，不能因"文件存在"而跳过。"""
    fetcher = _FakeFetcher()
    store = PriceStore(tmp_dir, fetcher=fetcher)
    asyncio.run(store.sync(["600519"], "20260901", "20260915"))
    assert store.covers("600519", "20260901", "20260915") is True
    assert store.covers("600519", "20200101", "20260915") is False

    info = asyncio.run(store.sync(["600519"], "20200101", "20260915"))
    assert info.fetched == ["600519"]
    frame = store.read("600519")
    assert frame["date"].min() >= "20200101"
    assert frame["date"].is_monotonic_increasing
    assert not frame["date"].duplicated().any(), "合并后不应出现重复交易日"


def test_sync_force_refetches(tmp_dir) -> None:
    fetcher = _FakeFetcher()
    store = PriceStore(tmp_dir, fetcher=fetcher)
    asyncio.run(store.sync(["600519"], "20260901", "20260915"))
    forced = asyncio.run(store.sync(["600519"], "20260901", "20260915", force=True))
    assert forced.fetched == ["600519"]
    assert len(fetcher.calls) == 2


def test_sync_records_failures_without_raising(tmp_dir) -> None:
    fetcher = _FakeFetcher()
    fetcher.fail.add("300750")
    fetcher.empty.add("000001")
    store = PriceStore(tmp_dir, fetcher=fetcher)
    info = asyncio.run(store.sync(["600519", "300750", "000001"],
                                  "20260901", "20260915"))
    assert info.fetched == ["600519"]
    assert "300750" in info.failed and "QMT 挂了" in info.failed["300750"]
    assert "000001" in info.failed and "空数据" in info.failed["000001"]
    assert store.cached_codes() == ["600519"]


def test_manifest_survives_corruption(tmp_dir) -> None:
    fetcher = _FakeFetcher()
    store = PriceStore(tmp_dir, fetcher=fetcher)
    asyncio.run(store.sync(["600519"], "20260901", "20260915"))
    (store.root / "_manifest.json").write_text("{ 这不是 json", encoding="utf-8")
    assert store.manifest() == {}
    info = asyncio.run(store.sync(["600519"], "20260901", "20260915"))
    assert info.fetched == ["600519"], "manifest 坏了应重新取数，而不是假装命中"


# ==================== 面板组装 ====================


def test_load_panel_aligns_codes_on_date_index(tmp_dir) -> None:
    fetcher = _FakeFetcher()
    store = PriceStore(tmp_dir, fetcher=fetcher)
    asyncio.run(store.sync(["600519"], "20260901", "20260915"))
    asyncio.run(store.sync(["300750"], "20260908", "20260915"))

    panel = store.load_panel()
    assert list(panel.columns) == ["300750", "600519"] or \
        sorted(panel.columns) == ["300750", "600519"]
    assert panel.index.is_monotonic_increasing
    # 早期日期上 300750 缺数据 → NaN，不做任何填充
    assert panel.loc["20260901", "300750"] != panel.loc["20260901", "300750"] \
        or pd.isna(panel.loc["20260901", "300750"])


def test_load_panel_selects_field(tmp_dir) -> None:
    fetcher = _FakeFetcher()
    store = PriceStore(tmp_dir, fetcher=fetcher)
    asyncio.run(store.sync(["600519"], "20260901", "20260915"))
    volume = store.load_panel(field="volume")
    assert volume["600519"].dropna().iloc[0] == pytest.approx(1.0e6)
    with pytest.raises(ValueError, match="未知字段"):
        store.load_panel(field="pe")


def test_load_panel_respects_date_window(tmp_dir) -> None:
    fetcher = _FakeFetcher()
    store = PriceStore(tmp_dir, fetcher=fetcher)
    asyncio.run(store.sync(["600519"], "20260901", "20260930"))
    panel = store.load_panel(start="20260908", end="20260910")
    assert panel.index.min() >= "20260908"
    assert panel.index.max() <= "20260910"


def test_load_panel_empty_when_no_cache(tmp_dir) -> None:
    store = PriceStore(tmp_dir, fetcher=_FakeFetcher())
    assert store.load_panel().empty
    assert store.cached_codes() == []


def test_price_panel_summary_reports_missing_ratio(tmp_dir) -> None:
    fetcher = _FakeFetcher()
    store = PriceStore(tmp_dir, fetcher=fetcher)
    asyncio.run(store.sync(["600519"], "20260901", "20260915"))
    asyncio.run(store.sync(["300750"], "20260908", "20260915"))
    panel = store.load_panel()
    summary = price_panel_summary(store, panel)
    assert summary["codes"] == 2
    assert summary["missing"] > 0
    assert 0 < summary["missing_ratio"] < 1
    assert summary["cached_codes"] == 2
    assert price_panel_summary(store, pd.DataFrame())["missing_ratio"] == 1.0
