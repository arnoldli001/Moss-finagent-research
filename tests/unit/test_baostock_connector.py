"""baostock 连接器测试（**全离线**：注入假 baostock 模块，绝不打网络）。

要钉住的是**单位与口径**，不是"能不能连上"：
  1. 符号映射：个股/ETF/指数三套代码段，sh/sz 归属复用 core.symbols；
  2. adjustflag 按前缀选：个股/ETF=前复权(2)、指数=不复权(3)；
  3. **单位换算**：baostock `volume` 是股 → ÷100 折手
     （实测对照：sh.510300 2026-09-16 volume=968759702 ↔ Tushare vol=9687597.02 手），
     `amount` 是元 → 原样；这是 100 倍级静默误差的护栏；
  4. 空结果 → DataFetchError（不能用空列表冒充成功）；
  5. 登录失败（error_code != '0'）→ DataFetchError，且带原始 message。
"""

from __future__ import annotations

import pytest

from src.core.exceptions import DataFetchError
from src.infrastructure.connectors import baostock_connector as module
from src.infrastructure.connectors.baostock_connector import (
    BaostockConnector,
    adjust_label_for,
    adjustflag_for,
    rows_to_points,
    to_baostock_symbol,
)

# 实测 sh.510300 2026-09-16 行（baostock 数值都是字符串）
_FIELDS = ["date", "open", "high", "low", "close", "preclose", "volume",
           "amount", "adjustflag", "turn", "pctChg"]
_ROWS_ETF = [
    ["2026-09-16", "4.5220", "4.5550", "4.4850", "4.5500", "4.5230",
     "968759702", "4384779786.0000", "2", "4.114907", "0.596900"],
    ["2026-09-17", "4.5390", "4.5680", "4.5240", "4.5320", "4.5500",
     "507546102", "2304178588.0000", "2", "2.133023", "-0.395600"],
]
# 实测 sh.000300 2026-09-18 行（指数字段同为字符串）
_ROWS_INDEX = [
    ["2026-09-18", "4492.3233", "4523.0915", "4480.5856", "4507.3926",
     "4460.1557", "18992416600", "537699359226.9000", "3", "0.568974",
     "1.059086"],
]


@pytest.fixture(autouse=True)
def _reset_login() -> None:
    """每个用例前重置进程内登录标志（模块级全局状态，避免用例间串味）。"""
    module.reset_login_state()


class _FakeResult:
    """假 ResultData：实现 error_code/error_msg/fields/next/get_row_data。"""

    def __init__(self, rows: list[list[str]], *,
                 error_code: str = "0", error_msg: str = "success",
                 fields: list[str] | None = None) -> None:
        self.error_code = error_code
        self.error_msg = error_msg
        self.fields = fields if fields is not None else list(_FIELDS)
        self._rows = rows
        self._cursor = -1

    def next(self) -> bool:
        self._cursor += 1
        return self._cursor < len(self._rows)

    def get_row_data(self) -> list[str]:
        return self._rows[self._cursor]


class _FakeLoginResult:
    def __init__(self, error_code: str = "0",
                 error_msg: str = "success") -> None:
        self.error_code = error_code
        self.error_msg = error_msg


class _FakeBaostock:
    """假 baostock 模块：记录调用参数，返回预置结果。"""

    def __init__(self, rows: list[list[str]] | None = None, *,
                 login_code: str = "0", login_msg: str = "success",
                 query_code: str = "0", query_msg: str = "success",
                 fields: list[str] | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.login_code = login_code
        self.login_msg = login_msg
        self.query_code = query_code
        self.query_msg = query_msg
        self.fields = fields
        self.login_calls = 0
        self.query_calls: list[dict] = []

    def login(self) -> _FakeLoginResult:
        self.login_calls += 1
        return _FakeLoginResult(self.login_code, self.login_msg)

    def logout(self) -> _FakeLoginResult:
        return _FakeLoginResult()

    def query_history_k_data_plus(self, code: str, fields: str, **kwargs):
        self.query_calls.append({"code": code, "fields": fields, **kwargs})
        return _FakeResult(self.rows, error_code=self.query_code,
                           error_msg=self.query_msg, fields=self.fields)


# ==================== 符号映射 ====================

@pytest.mark.parametrize(
    ("indicator", "expected"),
    [
        ("stock_close:600036", "sh.600036"),
        ("stock_close:300308", "sz.300308"),
        ("stock_close:002463", "sz.002463"),
        ("index_close:000300", "sh.000300"),
        ("index_close:000001", "sh.000001"),
        ("index_close:880001", "sh.880001"),
        ("index_close:399006", "sz.399006"),
        ("etf_close:510300", "sh.510300"),
        ("etf_close:588170", "sh.588170"),
        ("etf_close:560050", "sh.560050"),
        ("etf_close:159915", "sz.159915"),
        ("etf_close:161725", "sz.161725"),
    ],
)
def test_symbol_mapping(indicator: str, expected: str) -> None:
    assert to_baostock_symbol(indicator) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "stock_close:830799",      # 北交所
        "stock_close:920001",      # 北交所新号段
        "stock_close:510300",      # ETF 走个股路径
        "index_close:600036",      # 代码段不是指数
        "index_close:300308",
        "etf_close:600036",
        "etf_close:500001",
        "etf_close:920001",
        "etf_close:51030",         # 非 6 位
        "CPI",
        "stock_close:",
    ],
)
def test_symbol_rejects_bad(bad: str) -> None:
    with pytest.raises(DataFetchError):
        to_baostock_symbol(bad)


# ==================== adjustflag 选择 ====================

@pytest.mark.parametrize(
    ("indicator", "flag"),
    [
        ("stock_close:600036", "2"),   # 前复权
        ("etf_close:510300", "2"),     # 前复权
        ("index_close:000300", "3"),   # 指数不除权
        ("index_close:399006", "3"),
    ],
)
def test_adjustflag_per_prefix(indicator: str, flag: str) -> None:
    assert adjustflag_for(indicator) == flag


@pytest.mark.parametrize(
    ("indicator", "label"),
    [("stock_close:600036", "qfq"), ("etf_close:510300", "qfq"),
     ("index_close:000300", "none"), ("index_close:399006", "none")],
)
def test_adjust_label_matches_flag(indicator: str, label: str) -> None:
    assert adjust_label_for(indicator) == label


def test_adjustflag_rejects_unsupported() -> None:
    with pytest.raises(DataFetchError):
        adjustflag_for("CPI")


# ==================== 行 → DataPoint（单位） ====================

def test_rows_to_points_converts_shares_to_lots() -> None:
    """volume 原始单位=股 → ÷100 折手；amount 原始单位=元 → 原样。"""
    points = rows_to_points(_ROWS_ETF, _FIELDS, "etf_close:510300",
                            adjust="qfq")
    assert [p.period_date for p in points] == ["2026-09-16", "2026-09-17"]
    latest = points[-1]
    assert latest.value == pytest.approx(4.5320)
    assert latest.extra["open"] == pytest.approx(4.5390)
    assert latest.extra["high"] == pytest.approx(4.5680)
    assert latest.extra["low"] == pytest.approx(4.5240)
    # 507546102 股 → 5075461.02 手（Tushare 同日 fund_daily 的 vol 正是这个量级）
    assert latest.extra["volume"] == pytest.approx(5075461.02)
    assert latest.extra["volume_unit"] == "手"
    assert latest.extra["volume_raw_unit"] == "股"
    assert latest.extra["amount"] == pytest.approx(2304178588.0)  # 元，不换算
    assert latest.extra["adjust"] == "qfq"
    assert latest.extra["pre_close"] == pytest.approx(4.5500)
    assert latest.source_name == "baostock"


def test_rows_to_points_index_divides_by_hundred() -> None:
    """指数 volume 原始是股 → ÷100 折手（**与个股同一系数**），并标 none。

    实测（2026-09-22，000300.SH 同日四源对照）：
      baostock volume=17,786,387,600 股 ÷100 = 177,863,876
      Tushare index_daily vol=177,863,876                ← 逐位相同
      AkShare 新浪指数 volume=17,786,387,600 股 ÷100      ← 同样一致
      腾讯 fqkline 指数成交量=177,863,876,000             ← 1000 倍，由腾讯连接器自己换算
    """
    points = rows_to_points(_ROWS_INDEX, _FIELDS, "index_close:000300",
                            adjust="none")
    assert points[0].extra["adjust"] == "none"
    assert points[0].value == pytest.approx(4507.3926)
    assert points[0].extra["volume"] == pytest.approx(189924166.0)  # 18,992,416,600 ÷100
    assert points[0].extra["volume_unit"] == "手"
    assert points[0].extra["volume_raw_unit"] == "股"
    assert points[0].extra["amount"] == pytest.approx(537699359226.9)  # 元，原样


def test_rows_to_points_filters_range_and_skips_suspended_rows() -> None:
    rows = [
        *_ROWS_ETF,
        ["bad-date", "1", "1", "1", "1", "1", "1", "1", "2", "0", "0"],
        # 停牌行：close 为空 → 跳过，不编造
        ["2026-09-18", "4.5", "4.5", "4.5", "", "4.532", "", "",
         "2", "0", "0"],
    ]
    points = rows_to_points(rows, _FIELDS, "etf_close:510300", adjust="qfq",
                            start_date="2026-09-17", end_date="2026-09-18")
    assert [p.period_date for p in points] == ["2026-09-17"]


# ==================== 连接器行为 ====================

def test_supports_stock_index_and_etf_close() -> None:
    assert BaostockConnector.supports("stock_close:600036")
    assert BaostockConnector.supports("index_close:000300")
    assert BaostockConnector.supports("etf_close:510300")
    assert not BaostockConnector.supports("CPI")


@pytest.mark.asyncio
async def test_fetch_passes_adjustflag_and_converts_units() -> None:
    fake = _FakeBaostock(_ROWS_ETF)
    points = await BaostockConnector(bs=fake).fetch(
        "etf_close:510300", "2026-09-01", "2026-09-30")
    assert fake.login_calls == 1
    call = fake.query_calls[0]
    assert call["code"] == "sh.510300"
    assert call["adjustflag"] == "2"
    assert call["frequency"] == "d"
    assert call["start_date"] == "2026-09-01"
    assert call["end_date"] == "2026-09-30"
    assert points[-1].extra["volume"] == pytest.approx(5075461.02)


@pytest.mark.asyncio
async def test_fetch_index_uses_unadjusted_flag() -> None:
    fake = _FakeBaostock(_ROWS_INDEX)
    points = await BaostockConnector(bs=fake).fetch("index_close:000300")
    assert fake.query_calls[0]["adjustflag"] == "3"
    assert fake.query_calls[0]["code"] == "sh.000300"
    assert points[0].extra["adjust"] == "none"


@pytest.mark.asyncio
async def test_fetch_empty_result_raises() -> None:
    """区间外/已退市 → 空表必须抛 DataFetchError，不能返回空列表冒充成功。"""
    fake = _FakeBaostock([])
    with pytest.raises(DataFetchError, match="无"):
        await BaostockConnector(bs=fake).fetch("stock_close:600036")


@pytest.mark.asyncio
async def test_fetch_login_failure_raises_with_message() -> None:
    """登录失败（error_code != '0'）→ DataFetchError 且带原始 message。"""
    fake = _FakeBaostock(_ROWS_ETF, login_code="10001001",
                         login_msg="网络接收错误。")
    with pytest.raises(DataFetchError, match="baostock 登录失败"):
        await BaostockConnector(bs=fake).fetch("stock_close:600036")
    assert fake.query_calls == [], "登录失败时不应再发查询"


@pytest.mark.asyncio
async def test_fetch_query_failure_raises() -> None:
    fake = _FakeBaostock([], query_code="10001", query_msg="无此代码")
    with pytest.raises(DataFetchError, match="baostock 查询失败"):
        await BaostockConnector(bs=fake).fetch("stock_close:600036")


@pytest.mark.asyncio
async def test_fetch_rejects_unsupported_indicator() -> None:
    with pytest.raises(DataFetchError, match="不支持"):
        await BaostockConnector(bs=_FakeBaostock()).fetch("CPI")


@pytest.mark.asyncio
async def test_login_is_idempotent_across_calls() -> None:
    """登录是进程级的：同一连接器连续取数只 login 一次（模块锁 + 幂等标志）。"""
    fake = _FakeBaostock(_ROWS_ETF)
    connector = BaostockConnector(bs=fake)
    await connector.fetch("etf_close:510300")
    await connector.fetch("etf_close:510300")
    assert fake.login_calls == 1
    assert len(fake.query_calls) == 2


def test_import_baostock_raises_data_fetch_error_when_missing(monkeypatch) -> None:
    """未安装 baostock 时给 DataFetchError（不是 ImportError），保证路由能正常回退。"""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "baostock":
            raise ImportError("No module named 'baostock'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    with pytest.raises(DataFetchError, match="baostock 未安装"):
        module._import_baostock()  # noqa: SLF001


def test_capabilities_declare_three_indicators() -> None:
    caps = BaostockConnector().get_capabilities()
    assert caps["indicators"] == [
        "stock_close:{code}", "index_close:{code}", "etf_close:{code}"]
    assert caps["source_type"] == "api"
