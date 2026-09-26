"""Tushare 数据源与分区存储单测（离线：token 解析、限流、错误分类、规范化、增量）。

不发任何网络请求；`TushareClient` 的 SDK 调用被替换为假对象。
"""
from __future__ import annotations

import asyncio
import os
import time

import pandas as pd
import pytest

from src.quant import tushare_source as ts_src
from src.quant.dataset_store import DatasetStore, quarter_periods, trading_days
from src.quant.tushare_source import (
    TushareClient,
    TushareConfigError,
    TushareError,
    TusharePermissionError,
    _is_permission_error,
    _is_rate_limit_error,
    normalize,
    resolve_token,
    to_code,
)

# ==================== token 解析三级兜底 ====================


def test_token_from_env(monkeypatch) -> None:
    monkeypatch.setenv("TUSHARE_TOKEN", "env-token-123")
    assert resolve_token() == "env-token-123"


def test_token_env_is_case_insensitive_name(monkeypatch) -> None:
    """用户写的是小写 tushare_token（实测场景）。"""
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.setenv("tushare_token", "lower-token-456")
    assert resolve_token() == "lower-token-456"


def test_token_falls_back_to_dotenv(monkeypatch, tmp_dir) -> None:
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("tushare_token", raising=False)
    monkeypatch.delenv("TS_TOKEN", raising=False)
    monkeypatch.setattr(ts_src, "_from_windows_registry", lambda: "")
    add = os.path.join(tmp_dir, ".env")
    with open(add, "w", encoding="utf-8") as handle:
        handle.write("# 注释\nTUSHARE_TOKEN='dotenv-token-789'\n")
    monkeypatch.chdir(tmp_dir)
    assert resolve_token() == "dotenv-token-789"


def test_token_falls_back_to_windows_registry(monkeypatch) -> None:
    """环境变量没继承到时（DSH 早于设置启动）必须能从注册表兜住。"""
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("tushare_token", raising=False)
    monkeypatch.delenv("TS_TOKEN", raising=False)
    monkeypatch.setattr(ts_src, "_from_dotenv", lambda root=None: "")
    monkeypatch.setattr(ts_src, "_from_windows_registry", lambda: "registry-token")
    assert resolve_token() == "registry-token"


def test_explicit_token_wins(monkeypatch) -> None:
    monkeypatch.setenv("TUSHARE_TOKEN", "env-token")
    assert resolve_token("explicit") == "explicit"


def test_token_missing_raises_actionable_error(monkeypatch) -> None:
    for key in ("TUSHARE_TOKEN", "tushare_token", "TS_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(ts_src, "_from_dotenv", lambda root=None: "")
    monkeypatch.setattr(ts_src, "_from_windows_registry", lambda: "")
    with pytest.raises(TushareConfigError, match="未找到 Tushare token"):
        resolve_token()


def test_token_hint_never_leaks_full_token() -> None:
    hint = ts_src.token_hint("1bc8abcdef0123456789abcdef0123456789abcdef0123456789abcd556e")
    assert "1bc8" in hint and "556e" in hint
    assert "abcdef0123456789" not in hint
    assert ts_src.token_hint("") == "（空）"


# ==================== 错误分类 ====================


@pytest.mark.parametrize("message", [
    "抱歉，您没有访问该接口的权限",
    "积分不足，请提升积分",
    "Sorry, you need more points",
])
def test_permission_error_detection(message) -> None:
    assert _is_permission_error(message) is True


@pytest.mark.parametrize("message", [
    "抱歉，您每分钟最多访问该接口500次",
    "访问过快，请稍后再试",
    "rate limit exceeded",
])
def test_rate_limit_error_detection(message) -> None:
    assert _is_rate_limit_error(message) is True


def test_normal_error_is_not_misclassified() -> None:
    assert _is_permission_error("网络超时") is False
    assert _is_rate_limit_error("网络超时") is False


# ==================== 客户端行为 ====================


class _FakePro:
    """假 SDK：按脚本返回/抛异常。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, api: str):
        def caller(**params):
            self.calls.append((api, params))
            item = self.script.pop(0) if self.script else pd.DataFrame()
            if isinstance(item, Exception):
                raise item
            return item
        return caller


def _client_with(script: list, **kwargs) -> TushareClient:
    client = TushareClient("fake-token", **kwargs)
    client._pro = _FakePro(script)          # noqa: SLF001 测试注入
    return client


def test_client_returns_dataframe_and_casts_none() -> None:
    client = _client_with([pd.DataFrame({"a": [1]}), None])
    assert len(client.call("daily", trade_date="20260914")) == 1
    assert client.call("moneyflow", trade_date="20260914").empty
    assert client.stats.calls == 2


def test_client_raises_permission_error_immediately() -> None:
    """权限不足不能重试浪费时间，必须立刻暴露给用户。"""
    client = _client_with([Exception("抱歉，您没有访问该接口的权限")] * 3, retries=3)
    with pytest.raises(TusharePermissionError, match="权限/积分不足"):
        client.call("fina_indicator_vip", period="20260630")
    assert client.stats.calls == 1, "权限错误不应重试"


def test_client_retries_on_rate_limit_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    client = _client_with(
        [Exception("抱歉，您每分钟最多访问该接口500次"), pd.DataFrame({"a": [1]})],
        retries=3, retry_wait=0.0)
    frame = client.call("daily_basic", trade_date="20260914")
    assert len(frame) == 1
    assert client.stats.retries == 1


def test_client_retries_generic_error_then_raises(monkeypatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    client = _client_with([Exception("网络抖动")] * 3, retries=3, retry_wait=0.0)
    with pytest.raises(TushareError, match="调用失败"):
        client.call("daily", trade_date="20260914")


def test_client_async_entry() -> None:
    client = _client_with([pd.DataFrame({"a": [1, 2]})])
    frame = asyncio.run(client.acall("daily", trade_date="20260914"))
    assert len(frame) == 2


def test_rate_limiter_blocks_when_window_full() -> None:
    limiter = ts_src._RateLimiter(max_calls=3, window_seconds=0.2)  # noqa: SLF001
    assert limiter.acquire() == 0.0
    assert limiter.acquire() == 0.0
    assert limiter.acquire() == 0.0
    wait = limiter.acquire()
    assert wait > 0, "窗口满时必须给出等待时间"


def test_rate_limiter_is_thread_safe() -> None:
    import threading

    limiter = ts_src._RateLimiter(max_calls=100, window_seconds=60.0)  # noqa: SLF001
    results: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(20):
            value = limiter.acquire()
            with lock:
                results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 160
    assert sum(1 for value in results if value == 0.0) == 100, \
        "并发下窗口计数不能失效（只应放行 max_calls 个）"


def test_probe_reports_permission_failures(monkeypatch) -> None:
    client = TushareClient("fake-token")
    called: list[str] = []

    def fake_call(api: str, **params):
        called.append(api)
        if api == "fina_indicator_vip":
            raise TusharePermissionError("积分不足")
        return pd.DataFrame({"x": [1, 2]})

    monkeypatch.setattr(client, "call", fake_call)
    report = client.probe()
    assert report["daily"]["ok"] is True
    assert report["fina_indicator_vip"]["ok"] is False
    assert report["fina_indicator_vip"]["kind"] == "permission"
    assert len(called) == len(report)


# ==================== 单位与列名规范化 ====================


def test_normalize_converts_units() -> None:
    """万元→元、千元→元、万股→股：搞错就是 10000 倍级错误。"""
    frame = pd.DataFrame({
        "ts_code": ["600519.SH"], "trade_date": ["20260914"],
        "total_mv": [1.5e5],          # 万元 → 15 亿元
        "circ_mv": [1.2e5],
        "total_share": [1.25e4],      # 万股 → 1.25 亿股
        "turnover_rate": [0.85],
        "pe_ttm": [22.5],
    })
    out = normalize(frame, ts_src.DAILY_BASIC_MAP)
    assert out["total_mv"].iloc[0] == pytest.approx(1.5e9)
    assert out["circ_mv"].iloc[0] == pytest.approx(1.2e9)
    assert out["total_share"].iloc[0] == pytest.approx(1.25e8)
    assert out["turnover_rate"].iloc[0] == pytest.approx(0.85)
    assert "ts_code" in out.columns


def test_normalize_daily_amount_thousand_yuan() -> None:
    frame = pd.DataFrame({"ts_code": ["600519.SH"], "trade_date": ["20260914"],
                          "close": [1278.5], "amount": [1.75e6],  # 千元
                          "vol": [13762.0]})
    out = normalize(frame, ts_src.DAILY_MAP)
    assert out["amount"].iloc[0] == pytest.approx(1.75e9)
    assert out["volume_lot"].iloc[0] == pytest.approx(13762.0)


def test_normalize_empty_frame_keeps_columns() -> None:
    out = normalize(pd.DataFrame(), ts_src.DAILY_MAP)
    assert out.empty
    assert "amount" in out.columns


def test_normalize_handles_missing_columns() -> None:
    out = normalize(pd.DataFrame({"ts_code": ["600519.SH"]}), ts_src.DAILY_BASIC_MAP)
    assert out["ts_code"].iloc[0] == "600519.SH"
    assert "pe_ttm" not in out.columns


def test_to_code_strips_exchange() -> None:
    codes = to_code(pd.Series(["600519.SH", "000001.SZ", "300750.SZ"]))
    assert codes.tolist() == ["600519", "000001", "300750"]


# ==================== 分区存储与增量 ====================


def test_store_write_read_roundtrip(tmp_dir) -> None:
    store = DatasetStore("daily_basic", root=tmp_dir)
    frame = pd.DataFrame({"code": ["600519"], "pe_ttm": [22.5]})
    store.write("20260914", frame)
    assert store.has("20260914")
    assert store.keys() == ["20260914"]
    loaded = store.read("20260914")
    assert loaded["pe_ttm"].iloc[0] == pytest.approx(22.5)


def test_store_load_adds_partition_key(tmp_dir) -> None:
    store = DatasetStore("daily", root=tmp_dir)
    store.write("20260914", pd.DataFrame({"code": ["600519"], "close": [1.0]}))
    store.write("20260915", pd.DataFrame({"code": ["600519"], "close": [2.0]}))
    long_frame = store.load(key_column="trade_date")
    assert len(long_frame) == 2
    assert sorted(long_frame["trade_date"]) == ["20260914", "20260915"]


def test_store_isolates_universe(tmp_dir) -> None:
    """股票池必须隔离：a_share 与 all 不能互相命中缓存。"""
    a_share = DatasetStore("daily", root=tmp_dir, universe="a_share")
    everything = DatasetStore("daily", root=tmp_dir, universe="all")
    a_share.write("20260914", pd.DataFrame({"code": ["600519"]}))
    assert a_share.has("20260914") is True
    assert everything.has("20260914") is False
    assert everything.keys() == []


def test_store_rejects_bad_dataset_name(tmp_dir) -> None:
    with pytest.raises(ValueError, match="非法数据集名"):
        DatasetStore("../evil", root=tmp_dir)


def test_store_sync_is_incremental(tmp_dir) -> None:
    store = DatasetStore("daily", root=tmp_dir)
    calls: list[str] = []

    async def fetch(key: str) -> pd.DataFrame:
        calls.append(key)
        return pd.DataFrame({"code": ["600519"], "close": [1.0]})

    first = asyncio.run(store.sync(["20260914", "20260915"], fetch=fetch))
    assert first.fetched == ["20260914", "20260915"]
    second = asyncio.run(store.sync(["20260914", "20260915"], fetch=fetch))
    assert second.skipped == ["20260914", "20260915"]
    assert len(calls) == 2, "第二次同步不应再取数"


def test_store_sync_records_empty_and_exceptions(tmp_dir) -> None:
    store = DatasetStore("daily", root=tmp_dir)

    async def fetch(key: str) -> pd.DataFrame:
        if key == "20260914":
            return pd.DataFrame()
        raise RuntimeError("接口挂了")

    result = asyncio.run(store.sync(["20260914", "20260915"], fetch=fetch))
    assert result.fetched == []
    assert "空表" in result.failed["20260914"]
    assert "接口挂了" in result.failed["20260915"]
    assert store.keys() == [], "失败的分区不能留下空文件"


def test_store_coverage_summary(tmp_dir) -> None:
    store = DatasetStore("daily", root=tmp_dir)
    store.write("20260914", pd.DataFrame({"code": ["600519"]}))
    store.write("20260915", pd.DataFrame({"code": ["600519"]}))
    coverage = store.coverage()
    assert coverage["partitions"] == 2
    assert coverage["rows"] == 2
    assert coverage["first"] == "20260914" and coverage["last"] == "20260915"


def test_quarter_periods_skips_future() -> None:
    periods = quarter_periods(2026, 2026, as_of="20260915")
    assert periods == ["20260331", "20260630"]


def test_trading_days_falls_back_to_weekdays() -> None:
    days = trading_days(start="20260911", end="20260915")
    assert days == ["20260911", "20260914", "20260915"]   # 跳过周末


# ============== 文本列不能被数字强转吃掉 ==============
# 事故记录（2026-09-25）：`normalize` 原先只放行 4 个日期/代码列，其余
# 一律 `pd.to_numeric(errors="coerce")`。后果是 `namechange.name` 全变 NaN，
# 表现为"历史上曾被 ST 的票数 = 0" —— 一个**不报错**的错误答案；
# 同批被清空的还有 `suspend_d.suspend_type`（S=停牌/R=复牌）与 `suspend_timing`。


def test_normalize_keeps_registered_text_columns() -> None:
    frame = pd.DataFrame({
        "ts_code": ["000001.SZ", "000002.SZ"],
        "name": ["ST一号", "平安银行"],
        "start_date": ["20100101", "19910403"],
        "end_date": ["20101231", None],
        "ann_date": ["20091231", None],
        "change_reason": ["其他", "其他"],
    })
    out = normalize(frame, ts_src.NAME_CHANGE_MAP)
    assert out["name"].tolist() == ["ST一号", "平安银行"]
    assert out["change_reason"].tolist() == ["其他", "其他"]
    # 日期也要保持字符串，不能被转成 20100101.0 这种浮点
    assert out["start_date"].tolist() == ["20100101", "19910403"]
    assert out["end_date"].iloc[0] == "20101231"


def test_normalize_keeps_suspend_type() -> None:
    """停复牌类型：丢了它就分不清"停牌"与"复牌"（后者被当成停牌会少交易一天）。"""
    frame = pd.DataFrame({
        "ts_code": ["000001.SZ", "000002.SZ"],
        "trade_date": ["20260924", "20260924"],
        "suspend_type": ["S", "R"],
        "suspend_timing": ["09:30-10:00", None],
    })
    out = normalize(frame, {"ts_code": "ts_code", "trade_date": "trade_date",
                            "suspend_type": "suspend_type",
                            "suspend_timing": "suspend_timing"})
    assert out["suspend_type"].tolist() == ["S", "R"]
    assert out["suspend_timing"].iloc[0] == "09:30-10:00"


def test_normalize_warns_when_a_text_column_gets_wiped(caplog) -> None:
    """没登记的文本列仍会被强转 —— 但必须**留下一条 warning**。

    这条守的是"以后加了新数据集、忘了登记文本字段"的场景：
    静默清空是这次事故的本质，所以至少要让它在日志里响一声。
    """
    frame = pd.DataFrame({"ts_code": ["000001.SZ"],
                          "sell_reason": ["大股东减持"]})
    with caplog.at_level("WARNING"):
        out = normalize(frame, {"ts_code": "ts_code",
                                "sell_reason": "sell_reason"})
    assert out["sell_reason"].isna().all()          # 现状：仍会被吃掉
    assert "sell_reason" in caplog.text             # 但不再是静默的
    assert "_TEXT_COLUMNS" in caplog.text
