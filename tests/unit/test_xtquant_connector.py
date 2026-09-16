"""XtQuantConnector测试：纯函数+Fake xtdata回退逻辑，不依赖真实QMT。"""

import pandas as pd
import pytest

from src.core.exceptions import DataFetchError
from src.core.schemas import DataSourceType, FetchMethod
from src.infrastructure.connectors.xtquant_connector import (
    XtQuantConnector,
    frames_to_points,
    normalize_date,
    to_qmt_code,
)


def test_to_qmt_code_market_mapping():
    assert to_qmt_code("600000") == "600000.SH"
    assert to_qmt_code("601088") == "601088.SH"
    assert to_qmt_code("688981") == "688981.SH"
    assert to_qmt_code("000001") == "000001.SZ"  # 平安银行（深），非上证指数
    assert to_qmt_code("300750") == "300750.SZ"
    assert to_qmt_code("002594") == "002594.SZ"


def test_to_qmt_code_invalid():
    with pytest.raises(DataFetchError):
        to_qmt_code("AAPL")
    with pytest.raises(DataFetchError):
        to_qmt_code("430047")  # 北交所
    with pytest.raises(DataFetchError):
        to_qmt_code("920002")


def test_normalize_date():
    assert normalize_date("2026-09-01") == "20260901"
    assert normalize_date("20260901") == "20260901"
    assert normalize_date(None) == ""
    assert normalize_date("2026-09") == "20260901"
    assert normalize_date("2026-09", end=True) == "202609"


def test_frames_to_points():
    # 2026-09-11 00:00 UTC ≈ 1789056000000 ms
    df = pd.DataFrame({
        "time": [1789056000000],
        "open": [47.0], "high": [47.8], "low": [46.9], "close": [47.51],
        "volume": [100000.0], "amount": [4700000.0],
    })
    points = frames_to_points(df, "stock_close:601088")
    assert len(points) == 1
    p = points[0]
    assert p.value == 47.51
    assert p.period_date == "2026-09-11"
    assert p.extra["adjust"] == "qfq"
    assert p.source_name == "迅投QMT"
    assert p.source_type == DataSourceType.API
    assert p.fetch_method == FetchMethod.API_CALL
    assert p.confidence == 0.9


def test_frames_to_points_empty():
    assert frames_to_points(None, "x") == []
    assert frames_to_points(pd.DataFrame(), "x") == []


def test_supports():
    assert XtQuantConnector.supports("stock_close:600000")
    assert not XtQuantConnector.supports("CPI")
    assert not XtQuantConnector.supports("PE(TTM):600000")


class _FakeXtData:
    """首次本地为空，隔离补下载（子进程）之后才有数据。

    注意：`download_history_data` **不允许**在这里被调用 ——
    补下载必须走 `qmt_guard.download_history_isolated`（子进程隔离），
    否则 xtquant 原生崩溃会带走整个服务进程（实测事故，见 src/core/qmt_guard.py）。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.in_process_downloads: list[str] = []
        self.enable_hello = True

    def get_market_data_ex(self, fields, codes, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return {codes[0]: pd.DataFrame()}
        ts = 1789056000000  # 2026-09-11 UTC，东八区当日不跨天
        return {codes[0]: pd.DataFrame({
            "time": [ts], "open": [10.0], "high": [10.2], "low": [9.9],
            "close": [10.1], "volume": [1.0], "amount": [10.0]})}

    def download_history_data(self, code, period, start_time="", end_time="",
                              incrementally=None):
        self.in_process_downloads.append(code)


class _IsolatedDownloadRecorder:
    """替代 qmt_guard.download_history_isolated 的桩，记录调用并返回预设结果。"""

    def __init__(self, ok: bool = True, detail: str = "") -> None:
        self.ok = ok
        self.detail = detail
        self.calls: list[tuple[str, str, str, str]] = []

    def __call__(self, qmt_code, period, *, start_time="", end_time="", timeout=0.0):
        self.calls.append((qmt_code, period, start_time, end_time))
        return self.ok, self.detail


async def test_load_triggers_single_download_then_succeed(monkeypatch):
    from src.core import qmt_guard

    recorder = _IsolatedDownloadRecorder()
    monkeypatch.setattr(qmt_guard, "download_history_isolated", recorder)
    connector = XtQuantConnector()
    fake = _FakeXtData()
    connector._xtdata = fake  # 绕过真实xtquant导入
    points = await connector.fetch("stock_close:600000", "2026-08-01", "2026-09-30")
    assert [call[0] for call in recorder.calls] == ["600000.SH"]
    assert recorder.calls[0][1] == "1d"
    assert fake.in_process_downloads == [], "补下载必须走子进程，不能在服务进程里跑"
    assert len(points) == 1
    assert points[0].value == 10.1


class _AlwaysEmpty(_FakeXtData):
    def get_market_data_ex(self, fields, codes, **kwargs):
        return {codes[0]: pd.DataFrame()}


async def test_load_empty_after_download_raises(monkeypatch):
    from src.core import qmt_guard

    monkeypatch.setattr(
        qmt_guard, "download_history_isolated", _IsolatedDownloadRecorder())
    connector = XtQuantConnector()
    connector._xtdata = _AlwaysEmpty()
    with pytest.raises(DataFetchError):
        await connector.fetch("stock_close:600000")


async def test_load_reports_isolated_download_failure(monkeypatch):
    """隔离下载失败（例如子进程原生崩溃）→ 报 DataFetchError 让路由回退别的数据源。"""
    from src.core import qmt_guard

    monkeypatch.setattr(
        qmt_guard, "download_history_isolated",
        _IsolatedDownloadRecorder(ok=False, detail="补下载子进程异常退出(code=-1)"))
    connector = XtQuantConnector()
    fake = _FakeXtData()
    connector._xtdata = fake
    with pytest.raises(DataFetchError, match="补下载失败"):
        await connector.fetch("stock_close:600000", "2026-08-01", "2026-09-30")
    assert fake.in_process_downloads == []
