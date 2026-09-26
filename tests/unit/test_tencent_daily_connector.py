"""腾讯日K连接器测试（离线：行解析纯函数 + 假 httpx client）。

要钉住的是**口径与分页语义**，不是"能不能连上"：
  1. 行序是「开、**收**、高、低」—— 读反会让 K 线形态整体错位；
  2. 成交量单位=手、成交额不返回（置 None，不编造）；
  3. 复权标签跟随**实际命中的节点**：`qfqday`→qfq，`day`→none，
     指数（实测只有 `day` 节点）恒为 none —— 绝不把不复权标成前复权；
  4. 个股/ETF/指数的 sh/sz 归属与 `xtquant_connector.quote_qmt_code` 一致；
  5. 长区间靠 end 锚点回退翻页，翻不满页数**响亮报错**而不是返回截断数据；
  6. **当天形成中bar只能靠"不带区间"的请求拿到**（实测 2026-09-23）：
     要最新的请求（end 缺省或不早于今天）第 1 页必须发完全开放形式，
     历史区间（end 在过去）绝不能发 —— 否则回测会拿到未收盘的 bar。
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

from src.core.exceptions import DataFetchError
from src.infrastructure.connectors.tencent_daily_connector import (
    TencentDailyConnector,
    rows_to_points,
    to_tencent_market_symbol,
    to_tencent_symbol,
)

# 腾讯行序：日期, 开, 收, 高, 低, 成交量(手)
_ROWS = [
    ["2026-09-15", "873.38", "864.01", "883.73", "858.00", "179554.41"],
    ["2026-09-16", "869.02", "907.80", "918.38", "866.00", "240218.46"],
]


@pytest.mark.parametrize(
    ("code", "expected"),
    [("300308", "sz300308"), ("002463", "sz002463"), ("600036", "sh600036"),
     ("688981", "sh688981"), ("000001", "sz000001")],
)
def test_symbol_mapping(code: str, expected: str) -> None:
    assert to_tencent_symbol(code) == expected


@pytest.mark.parametrize("bad", ["30030", "abcdef", "", "830799", "920001",
                                 "588170", "510300", "159915"])
def test_symbol_rejects_bj_and_etf(bad: str) -> None:
    """北交所与 ETF 在**个股路径**上仍必须明确拒绝（ETF 走 etf_close: 前缀）。"""
    with pytest.raises(DataFetchError):
        to_tencent_symbol(bad)


@pytest.mark.parametrize(
    ("indicator", "expected"),
    [
        # 个股
        ("stock_close:600036", "sh600036"),
        ("stock_close:300308", "sz300308"),
        # 指数：000xxx/880xxx 沪，399xxx 深（与 quote_qmt_code / index_symbol 一致）
        ("index_close:000300", "sh000300"),
        ("index_close:000001", "sh000001"),
        ("index_close:880001", "sh880001"),
        ("index_close:399006", "sz399006"),
        # ETF：51/56/58 沪，15/16 深（588170 是沪市科创ETF，判成深市会取数全空）
        ("etf_close:510300", "sh510300"),
        ("etf_close:588170", "sh588170"),
        ("etf_close:560050", "sh560050"),
        ("etf_close:159915", "sz159915"),
        ("etf_close:161725", "sz161725"),
    ],
)
def test_market_symbol_mapping(indicator: str, expected: str) -> None:
    assert to_tencent_market_symbol(indicator) == expected


@pytest.mark.parametrize(
    "bad",
    ["index_close:399001.SZ", "index_close:600036", "index_close:00030",
     "etf_close:51030", "etf_close:600036", "etf_close:500001",
     "etf_close:920001", "mkt:turnover", "stock_close:830799"],
)
def test_market_symbol_rejects_bad(bad: str) -> None:
    """代码段与指标类别不符时必须拒绝 —— 拼个错符号只会换回空结果。"""
    with pytest.raises(DataFetchError):
        to_tencent_market_symbol(bad)


def test_rows_mapped_with_correct_column_order() -> None:
    """腾讯行是「开、**收**、高、低」—— 把 high/low 读反会让K线形态整体错位。"""
    points = rows_to_points(_ROWS, "stock_close:300308", symbol="sz300308")
    assert [p.period_date for p in points] == ["2026-09-15", "2026-09-16"]
    latest = points[-1]
    assert latest.value == pytest.approx(907.80)          # close
    assert latest.extra["open"] == pytest.approx(869.02)
    assert latest.extra["high"] == pytest.approx(918.38)  # 不是 866.00
    assert latest.extra["low"] == pytest.approx(866.00)   # 不是 918.38
    assert latest.extra["volume"] == pytest.approx(240218.46)  # 手
    assert latest.extra["amount"] is None, "腾讯日K不返回成交额，不能编造"
    assert latest.extra["adjust"] == "qfq"


def test_rows_adjust_label_is_taken_from_actual_node() -> None:
    """口径标签由调用方按实际节点传入：只有 `day` 节点时标 none，不冒充前复权。"""
    points = rows_to_points(_ROWS, "etf_close:510300", symbol="sh510300",
                            adjust="none")
    assert points[-1].extra["adjust"] == "none"


def test_index_rows_volume_magnitude_guard() -> None:
    """指数行只做**量级兜底**：超过阈值的量 ÷1000，正常量原样。

    实测 2026-09-22（000300.SH 同日）：真值 177,863,876 手
    （Tushare index_daily / baostock 股÷100 / 新浪股÷100 三源一致），
    但腾讯同一分钟内两次请求分别给出 1.7786e11 与 1.7786e14 —— 单位不自洽，
    因此不能写死系数，只能按量级兜底（详见 rows_to_points docstring）。
    """
    normal = [["2026-09-22", "4500", "4507", "4523", "4480", "177863876"]]
    points = rows_to_points(normal, "index_close:000300", symbol="sh000300",
                            adjust="none")
    assert points[0].extra["volume"] == pytest.approx(177863876.0)  # 原样（已在真值量级）
    assert points[0].extra["volume_raw_unit"] == "手"

    inflated = [["2026-09-22", "4500", "4507", "4523", "4480", "177863876000"]]
    points = rows_to_points(inflated, "index_close:000300", symbol="sh000300",
                            adjust="none")
    assert points[0].extra["volume"] == pytest.approx(177863876.0)  # 1.7786e11 ÷1000
    assert "1000" in points[0].extra["volume_raw_unit"]

    # 个股行不受影响：本来就是手，量再大也不动
    stock = [["2026-09-22", "40", "41", "41", "40", "999999999999"]]
    points = rows_to_points(stock, "stock_close:600036", symbol="sh600036")
    assert points[0].extra["volume"] == pytest.approx(999999999999.0)
    assert points[0].extra["volume_raw_unit"] == "手"


def test_rows_filtered_by_range_and_bad_rows_skipped() -> None:
    rows = [*_ROWS, ["bad-date", "1", "1", "1", "1", "1"], ["2026-09-14"]]
    points = rows_to_points(rows, "stock_close:300308", symbol="sz300308",
                            start_date="2026-09-16", end_date="2026-09-17")
    assert [p.period_date for p in points] == ["2026-09-16"]


def test_supports_stock_index_and_etf_close() -> None:
    """三类日频行情都支持；非行情指标仍必须拒绝。"""
    assert TencentDailyConnector.supports("stock_close:300308")
    assert TencentDailyConnector.supports("index_close:000001")
    assert TencentDailyConnector.supports("etf_close:588170")
    assert not TencentDailyConnector.supports("CPI")
    assert not TencentDailyConnector.supports("mkt:turnover")


def test_adjust_flag_is_none_for_index_and_follows_node_otherwise() -> None:
    """指数恒 none（实测指数只有 `day` 节点）；个股/ETF 按实际节点。"""
    assert TencentDailyConnector._adjust_for(  # noqa: SLF001
        "index_close:000300", "day") == "none"
    assert TencentDailyConnector._adjust_for(  # noqa: SLF001
        "stock_close:600036", "qfqday") == "qfq"
    assert TencentDailyConnector._adjust_for(  # noqa: SLF001
        "etf_close:510300", "day") == "none"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload=None, *, boom: bool = False,
                 pages: dict | None = None) -> None:
        self.payload = payload
        self.boom = boom
        self.pages = pages or {}
        self.urls: list[str] = []
        self.closed = False

    def _payload_for(self, url: str):
        if self.pages:
            # 按 URL 里的 end 锚点选页：
            #   带区间 `...,day,1990-01-01,<锚点>,640,qfq` → 锚点字段
            #   不带区间 `...,day,,,640,qfq`           → 空串（第 1 页的"要最新"形式）
            anchor = url.split(",")[3]
            return self.pages.get(anchor, {"data": {}})
        return self.payload

    async def get(self, url: str):
        self.urls.append(url)
        if self.boom:
            raise httpx.ConnectError("模拟：连接被重置")
        return _FakeResponse(self._payload_for(url))

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_fetch_parses_qfqday_node() -> None:
    client = _FakeClient({"data": {"sz300308": {"qfqday": _ROWS}}})
    connector = TencentDailyConnector(client=client)
    points = await connector.fetch("stock_close:300308")
    assert [p.period_date for p in points] == ["2026-09-15", "2026-09-16"]
    assert points[-1].source_name == "腾讯财经日K"
    assert "qfq" in client.urls[0], "必须显式请求前复权"


@pytest.mark.asyncio
async def test_fetch_falls_back_to_day_node() -> None:
    client = _FakeClient({"data": {"sz300308": {"day": _ROWS}}})
    points = await TencentDailyConnector(client=client).fetch("stock_close:300308")
    assert len(points) == 2
    # 只拿到 `day`（不复权）节点 → 必须如实标 none
    assert points[-1].extra["adjust"] == "none"


@pytest.mark.asyncio
async def test_fetch_index_uses_day_node_and_labels_none() -> None:
    """指数实测只有 `day` 节点（不除权）→ adjust 必须是 none。"""
    client = _FakeClient({"data": {"sh000300": {"day": _ROWS}}})
    points = await TencentDailyConnector(client=client).fetch("index_close:000300")
    assert points[-1].extra["adjust"] == "none"
    assert "sh000300" in client.urls[0]


@pytest.mark.asyncio
async def test_fetch_etf_uses_qfq_and_labels_qfq() -> None:
    client = _FakeClient({"data": {"sh588170": {"qfqday": _ROWS}}})
    points = await TencentDailyConnector(client=client).fetch("etf_close:588170")
    assert points[-1].extra["adjust"] == "qfq"
    assert "sh588170" in client.urls[0]


@pytest.mark.asyncio
async def test_fetch_pages_backwards_until_start_date() -> None:
    """长区间：按 end 锚点回退翻页，直到覆盖请求起点（一次请求上限约 641 根）。"""
    page1 = {"data": {"sh600036": {"qfqday": [
        ["2024-01-31", "30", "31", "32", "29", "1000"],
        ["2024-02-01", "31", "32", "33", "30", "1000"]]}}}
    page2 = {"data": {"sh600036": {"qfqday": [
        ["2023-12-01", "28", "29", "30", "27", "1000"],
        ["2024-01-30", "29", "30", "31", "28", "1000"]]}}}
    # 第 1 页可能是不带区间的形式（end 不早于今天时，见 test_forming_bar_*），
    # 用一个"任意 end 都命中"的 client：假 client 的 payload 对所有锚点生效
    client = _FakeClient(pages={"2026-09-30": page1, "2024-01-30": page2, "": page1})
    points = await TencentDailyConnector(client=client).fetch(
        "stock_close:600036", "2023-12-01", "2026-09-30")
    assert [p.period_date for p in points] == [
        "2023-12-01", "2024-01-30", "2024-01-31", "2024-02-01"]
    assert len(client.urls) == 2, "第二页必须把锚点回退到本页最早日期的前一天"
    assert ",2024-01-30," in client.urls[1]


@pytest.mark.asyncio
async def test_forming_bar_requested_uses_open_range_first_page() -> None:
    """盘中要最新（end = 今天）：第 1 页必须不带区间 —— 只有它给当天形成中bar。

    ⚠️ fixture 日期必须**相对今天**推导，不能写死。

    这条断言的是"`end=今天` 时当天那根形成中 bar 要被保留"，
    期望值锚在**运行那一刻**。原先 fixture 写死 `2026-09-22/23/21`，
    于是它只在写入当天通过、之后自己变红 —— 报错
    （`assert '2026-09-23' == '2026-09-24'`）看不出与日期有关，
    很容易被当成"别人改坏了"长期挂着。
    """
    today = date.today()
    d0 = today.isoformat()                                  # 当天：形成中 bar
    d1 = (today - timedelta(days=1)).isoformat()             # 昨天
    d2 = (today - timedelta(days=2)).isoformat()             # 前天：翻页锚点
    page1 = {"data": {"sz300308": {"qfqday": [
        [d1, "968", "927.72", "974.99", "921", "234785"],
        [d0, "936.01", "922.30", "939.00", "920.88", "47797"]]}}}
    page2 = {"data": {"sz300308": {"qfqday": [
        [d2, "939.5", "941", "958.86", "931.66", "209622"]]}}}
    client = _FakeClient(pages={"": page1, d2: page2})
    points = await TencentDailyConnector(client=client).fetch(
        "stock_close:300308", d2, d0)

    assert "day,,," in client.urls[0], (
        "要最新的请求第 1 页必须发不带区间的形式，否则拿不到当天形成中bar")
    assert "1990-01-01" not in client.urls[0]
    assert points[-1].period_date == d0, "当天这根形成中bar必须被保留"
    assert points[-1].extra["volume"] == 47797.0
    # 第 2 页回到锚点形式，翻页语义不变
    assert f",{d2}," in client.urls[1]


@pytest.mark.asyncio
async def test_historical_range_keeps_anchored_first_page() -> None:
    """历史区间（end 在过去）：第 1 页**不能**跳到今天 —— 回测不许吃未收盘的 bar。"""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    earlier = (date.today() - timedelta(days=400)).isoformat()
    payload = {"data": {"sh600036": {"qfqday": [
        [earlier, "30", "31", "32", "29", "1000"],
        [yesterday, "31", "32", "33", "30", "1000"]]}}}
    client = _FakeClient(payload)
    points = await TencentDailyConnector(client=client).fetch(
        "stock_close:600036", earlier, yesterday)

    assert "1990-01-01" in client.urls[0], "历史区间仍走带区间的锚点形式"
    assert f",{yesterday},640,qfq" in client.urls[0]
    assert points[-1].period_date == yesterday


@pytest.mark.asyncio
async def test_single_shot_fetch_without_dates_uses_open_range() -> None:
    """单次取数（无区间）= 「给我最新的 N 根」：必须走不带区间的形式。"""
    client = _FakeClient({"data": {"sz300308": {"qfqday": _ROWS}}})
    await TencentDailyConnector(client=client).fetch("stock_close:300308")
    assert "day,,," in client.urls[0]
    assert "1990-01-01" not in client.urls[0]


def test_wants_forming_bar_judgement() -> None:
    """要不要当天形成中bar：end 缺省或不早于今天 = 要；end 在过去 = 不要。"""
    connector = TencentDailyConnector
    today = date.today()
    assert connector._wants_forming_bar(None) is True  # noqa: SLF001
    assert connector._wants_forming_bar(today.isoformat()) is True  # noqa: SLF001
    assert connector._wants_forming_bar(  # noqa: SLF001
        (today + timedelta(days=1)).isoformat()) is True
    assert connector._wants_forming_bar(  # noqa: SLF001
        (today - timedelta(days=1)).isoformat()) is False


@pytest.mark.asyncio
async def test_fetch_raises_when_pages_cannot_reach_start_date() -> None:
    """翻满页数仍到不了请求起点 → 抛错，**不能返回截断的数据**。"""
    payload = {"data": {"sh600036": {"qfqday": [
        ["2024-01-31", "30", "31", "32", "29", "1000"]]}}}
    client = _FakeClient(payload)
    connector = TencentDailyConnector(client=client, max_pages=3)
    with pytest.raises(DataFetchError, match="仍未覆盖请求起点"):
        await connector.fetch("stock_close:600036", "2015-01-01", "2026-09-30")
    assert len(client.urls) == 3


@pytest.mark.asyncio
async def test_fetch_history_forces_paging_without_dates() -> None:
    payload = {"data": {"sh600036": {"qfqday": [["2024-01-31", "30", "31", "32", "29", "1000"]]}}}
    client = _FakeClient(payload)
    connector = TencentDailyConnector(client=client, max_pages=1)
    with pytest.raises(DataFetchError, match="仍未覆盖请求起点"):
        await connector.fetch_history("stock_close:600036")
    assert "1990-01-01" in client.urls[0], "fetch_history 必须从最早起点翻起"


@pytest.mark.asyncio
async def test_fetch_raises_on_network_error() -> None:
    client = _FakeClient(boom=True)
    with pytest.raises(DataFetchError, match="请求失败"):
        await TencentDailyConnector(client=client).fetch("stock_close:300308")


@pytest.mark.asyncio
async def test_fetch_raises_on_empty_payload() -> None:
    client = _FakeClient({"data": {"sz300308": {}}})
    with pytest.raises(DataFetchError, match="无"):
        await TencentDailyConnector(client=client).fetch("stock_close:300308")


@pytest.mark.asyncio
async def test_fetch_rejects_unsupported_indicator() -> None:
    with pytest.raises(DataFetchError, match="不支持"):
        await TencentDailyConnector(client=_FakeClient({})).fetch("CPI")
