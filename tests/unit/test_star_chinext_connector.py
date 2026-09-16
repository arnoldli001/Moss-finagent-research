"""科创板/创业板连接器测试：腾讯GBK解析/东财fallback/AKShare备源/截面统计，不联网。"""

from __future__ import annotations

import pandas as pd
import pytest

from src.core.exceptions import DataFetchError
from src.infrastructure.connectors.star_chinext_connector import (
    StarChinextConnector,
    _summarize_rows,
)


def _tencent_line(code: str, name: str, amount_wan: str) -> str:
    """构造腾讯行情报文（split("~")后索引37=成交额万元）。"""
    parts = ["0"] * 40
    parts[0] = "1"
    parts[1] = name
    parts[37] = amount_wan
    return f'v_{code}="' + "~".join(parts) + '";'


class _FakeResponse:
    def __init__(self, *, content: bytes | None = None, payload: dict | None = None):
        self._content = content or b""
        self._payload = payload

    @property
    def content(self) -> bytes:
        return self._content

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """按URL子串返回预置响应的假httpx.AsyncClient；fail=True时全部断连。"""

    routes: dict = {}
    fail: bool = False

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def aclose(self) -> None:
        return None

    async def get(self, url: str):
        if _FakeAsyncClient.fail:
            raise ConnectionError("server disconnected")
        for key, resp in _FakeAsyncClient.routes.items():
            if key in url:
                return resp
        raise ConnectionError(f"未预置的URL: {url}")


@pytest.fixture
def patch_httpx(monkeypatch):
    def _patch(routes: dict, *, fail: bool = False):
        _FakeAsyncClient.routes = routes
        _FakeAsyncClient.fail = fail
        monkeypatch.setattr(
            "src.infrastructure.connectors.star_chinext_connector.httpx.AsyncClient",
            _FakeAsyncClient,
        )
    return _patch


# ---------- supports路由 ----------

def test_supports_indicator_routing():
    c = StarChinextConnector()
    assert c.supports("mkt:cybkcb:turnover:all")
    assert c.supports("mkt:cybkcb:turnover:cyb")
    assert c.supports("mkt:cybkcb:turnover:kcb_all")
    assert c.supports("mkt:cybkcb:turnover_hist")
    assert c.supports("mkt:cybkcb:val:cyb_pe")
    assert c.supports("mkt:cybkcb:val:all")
    assert c.supports("mkt:cybkcb:spot_summary")
    assert not c.supports("mkt:turnover:cyb")  # 大盘流动性连接器的指标
    assert not c.supports("mkt:cybkcb:val:cyb_pb")  # 板块PB无免费源，诚实降级
    assert not c.supports("mkt:cybkcb:unknown")
    assert not c.supports("mkt:margin_balance")


# ---------- 实时成交额：腾讯主源 ----------

@pytest.mark.asyncio
async def test_turnover_realtime_from_tencent(patch_httpx):
    # 创业板1800亿=1.8e7万、科创50 900亿、科创综指1500亿
    lines = "".join([
        _tencent_line("sz399006", "创业板指", "18000000"),
        _tencent_line("sh000688", "科创50", "9000000"),
        _tencent_line("sh000680", "科创综指", "15000000"),
    ])
    patch_httpx({"qt.gtimg.cn": _FakeResponse(content=lines.encode("gbk"))})

    c = StarChinextConnector()
    pts = await c.fetch("mkt:cybkcb:turnover:all")
    assert len(pts) == 3
    by_ind = {p.indicator: p for p in pts}
    assert by_ind["mkt:cybkcb:turnover:cyb"].value == 1800.0
    assert by_ind["mkt:cybkcb:turnover:kcb"].value == 900.0
    assert by_ind["mkt:cybkcb:turnover:kcb_all"].value == 1500.0
    assert all(p.extra["source"] == "腾讯财经" for p in pts)
    assert by_ind["mkt:cybkcb:turnover:cyb"].unit == "亿元"


@pytest.mark.asyncio
async def test_turnover_realtime_fallback_to_eastmoney(patch_httpx):
    # 腾讯断连（无腾讯URL路由）→ 东财push2逐secid返回f48（元）
    routes = {}
    for secid, amount in (("0.399006", 1.8e10), ("1.000688", 9e9),
                          ("1.000680", 1.5e10)):
        routes[f"secid={secid}"] = _FakeResponse(
            payload={"data": {"f48": amount}})
    patch_httpx(routes, fail=False)
    c = StarChinextConnector()
    pts = await c.fetch("mkt:cybkcb:turnover:all")
    assert len(pts) == 3
    by_ind = {p.indicator: p for p in pts}
    assert by_ind["mkt:cybkcb:turnover:cyb"].value == 180.0
    assert all(p.extra["source"] == "东方财富push2" for p in pts)


@pytest.mark.asyncio
async def test_turnover_realtime_double_failure(patch_httpx):
    patch_httpx({}, fail=True)
    c = StarChinextConnector()
    with pytest.raises(DataFetchError):
        await c.fetch("mkt:cybkcb:turnover:cyb")


# ---------- 历史成交额：东财日K主源 → AKShare备源 ----------

def _em_kline_payload(dates: list[str], amounts: list[float]) -> _FakeResponse:
    klines = [
        f"{d},100.0,101.0,102.0,99.0,99.5,{a}"
        for d, a in zip(dates, amounts, strict=True)
    ]
    return _FakeResponse(payload={"data": {"klines": klines}})


@pytest.mark.asyncio
async def test_turnover_hist_from_eastmoney(patch_httpx):
    dates = [f"2026-09-{d:02d}" for d in range(1, 11)]
    routes = {
        "secid=0.399006": _em_kline_payload(dates, [1.8e10] * 10),
        "secid=1.000688": _em_kline_payload(dates, [9e9] * 10),
        "secid=1.000680": _em_kline_payload(dates, [1.5e10] * 10),
    }
    patch_httpx(routes)
    c = StarChinextConnector()
    pts = await c.fetch("mkt:cybkcb:turnover_hist")
    assert len(pts) == 10
    first = pts[0]
    assert first.extra["cyb"] == 180.0
    assert first.extra["kcb"] == 90.0
    assert first.extra["kcb_all"] == 150.0
    # 合计=创业板全板+科创板全板口径（科创50成分不混入合计）
    assert first.value == round(180.0 + 150.0, 2)
    assert first.extra["total_caliber"] == "创业板全板+科创板全板"
    assert first.extra["source"] == "东方财富push2his"


@pytest.mark.asyncio
async def test_turnover_hist_fallback_to_akshare(patch_httpx, monkeypatch):
    patch_httpx({}, fail=True)  # 东财断连
    dates = pd.date_range("2026-08-01", periods=10).strftime("%Y-%m-%d")

    def _fake_hist(symbol, period="daily", start_date=None, end_date=None):
        amount = {"399006": 1.8e10, "000688": 9e9, "000680": 1.5e10}[symbol]
        return pd.DataFrame({
            "日期": dates, "成交额": [amount] * 10,
        })

    monkeypatch.setattr("akshare.index_zh_a_hist", _fake_hist)
    c = StarChinextConnector()
    pts = await c.fetch("mkt:cybkcb:turnover_hist")
    assert len(pts) == 10
    assert pts[0].extra["source"] == "AKShare index_zh_a_hist"
    assert pts[0].extra["cyb"] == 180.0
    assert pts[0].value == round(180.0 + 150.0, 2)  # cyb+kcb_all合计口径


@pytest.mark.asyncio
async def test_turnover_hist_double_failure(patch_httpx, monkeypatch):
    patch_httpx({}, fail=True)

    def _fail_hist(*args, **kwargs):
        raise ConnectionError("akshare down")

    monkeypatch.setattr("akshare.index_zh_a_hist", _fail_hist)
    c = StarChinextConnector()
    with pytest.raises(DataFetchError):
        await c.fetch("mkt:cybkcb:turnover_hist")


# ---------- 板块PE估值：乐咕主源 → 东财板块/中证官网兜底 ----------

def _legu_df(pe_col: str, pe_series: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2020-01-01", periods=len(pe_series)).strftime("%Y-%m-%d")
    return pd.DataFrame({"日期": dates, pe_col: pe_series})


@pytest.mark.asyncio
async def test_valuation_legu_fuzzy_column(monkeypatch):
    # 创业板列为"平均市盈率"（乐咕真实schema），模糊匹配
    pe_series = [float(i) for i in range(1, 101)]  # 1..100
    monkeypatch.setattr(
        "akshare.stock_market_pe_lg",
        lambda symbol: _legu_df("平均市盈率", pe_series),
    )
    c = StarChinextConnector()
    pts = await c.fetch("mkt:cybkcb:val:cyb_pe")
    assert len(pts) == 1
    p = pts[0]
    assert p.value == 100.0
    assert p.extra["pct_all"] == 100.0  # 历史最大值 → 全历史分位100%
    assert p.extra["pe_col"] == "平均市盈率"
    assert p.extra["history_size"] == 100
    assert p.unit == "倍"


@pytest.mark.asyncio
async def test_valuation_all_two_boards(monkeypatch):
    schemas = {"创业板": _legu_df("平均市盈率", [40.0] * 300),
               "科创板": _legu_df("市盈率", [120.0] * 1700)}
    monkeypatch.setattr(
        "akshare.stock_market_pe_lg", lambda symbol: schemas[symbol])
    c = StarChinextConnector()
    pts = await c.fetch("mkt:cybkcb:val:all")
    assert {p.indicator for p in pts} == {
        "mkt:cybkcb:val:cyb_pe", "mkt:cybkcb:val:kcb_pe"}
    by_ind = {p.indicator: p for p in pts}
    assert by_ind["mkt:cybkcb:val:kcb_pe"].value == 120.0


@pytest.mark.asyncio
async def test_valuation_cyb_fallback_to_em_board(patch_httpx, monkeypatch):
    def _fail_lg(*args, **kwargs):
        raise ConnectionError("legulegu blocked")

    monkeypatch.setattr("akshare.stock_market_pe_lg", _fail_lg)
    routes = {"secid=90.BK0475": _FakeResponse(
        payload={"data": {"f9": 45.6, "f58": "创业板"}})}
    patch_httpx(routes)
    c = StarChinextConnector()
    pts = await c.fetch("mkt:cybkcb:val:cyb_pe")
    assert len(pts) == 1
    assert pts[0].value == 45.6
    assert pts[0].extra["source"] == "东方财富板块"
    assert pts[0].confidence < 0.9  # 备源置信度降级


@pytest.mark.asyncio
async def test_valuation_kcb_fallback_to_csindex(monkeypatch):
    def _fail_lg(*args, **kwargs):
        raise ConnectionError("legu down")

    monkeypatch.setattr("akshare.stock_market_pe_lg", _fail_lg)

    class _FakeProxy:
        async def fetch(self, indicator, *a, **k):
            from src.core.schemas import DataPoint, FetchMethod
            assert indicator == "idx_val:pe_ttm:科创50"
            return [DataPoint(
                indicator=indicator, value=98.5, unit="倍",
                period_date="2026-09-12", source_name="中证",
                source_url="https://www.csindex.com.cn",
                fetch_method=FetchMethod.API_CALL,
            )]

    import src.infrastructure.connectors.index_valuation_connector as ivm
    monkeypatch.setattr(ivm, "IndexValuationConnector", _FakeProxy)
    c = StarChinextConnector()
    pts = await c.fetch("mkt:cybkcb:val:kcb_pe")
    assert pts[0].value == 98.5
    assert pts[0].extra["source"] == "中证指数官网"
    assert "代理" in pts[0].extra["note"]  # 口径差异诚实披露


@pytest.mark.asyncio
async def test_valuation_double_failure(patch_httpx, monkeypatch):
    def _fail_lg(*args, **kwargs):
        raise ConnectionError("legu down")

    monkeypatch.setattr("akshare.stock_market_pe_lg", _fail_lg)
    patch_httpx({}, fail=True)  # 东财板块/中证官网都断连
    c = StarChinextConnector()
    with pytest.raises(DataFetchError):
        await c.fetch("mkt:cybkcb:val:cyb_pe")


# ---------- 个股截面：东财clist主源 → AKShare备源 → 新浪第三源 ----------


class _PagedClistClient:
    """按pn=路由的假httpx客户端；page_map缺页时抛ConnectionError。"""

    page_map: dict[int, dict] = {}
    urls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def aclose(self) -> None:
        return None

    async def get(self, url: str):
        import re as _re
        _PagedClistClient.urls.append(url)
        m = _re.search(r"pn=(\d+)", url)
        page = int(m.group(1)) if m else 1
        payload = _PagedClistClient.page_map.get(page)
        if payload is None:
            raise ConnectionError("server disconnected")
        return _FakeResponse(payload={"data": payload})


@pytest.fixture
def patch_paged_httpx(monkeypatch):
    def _patch(page_map: dict[int, dict]):
        _PagedClistClient.page_map = page_map
        _PagedClistClient.urls = []
        monkeypatch.setattr(
            "src.infrastructure.connectors.star_chinext_connector.httpx.AsyncClient",
            _PagedClistClient,
        )
    return _patch


@pytest.mark.asyncio
async def test_spot_summary_from_clist(patch_httpx):
    rows = [
        {"f12": "300001", "f14": "甲公司", "f3": 5.0, "f6": 2e9, "f8": 8.0},
        {"f12": "300002", "f14": "乙公司", "f3": -2.0, "f6": 1e9, "f8": 3.0},
        {"f12": "688001", "f14": "丙公司", "f3": 1.0, "f6": 5e9, "f8": 12.0},
        {"f12": "688002", "f14": "丁公司", "f3": 0.0, "f6": 2e8, "f8": 1.0},
    ]
    patch_httpx({"clist/get": _FakeResponse(
        payload={"data": {"total": 4, "diff": rows}})})
    c = StarChinextConnector()
    pts = await c.fetch("mkt:cybkcb:spot_summary")
    assert len(pts) == 1
    extra = pts[0].extra
    assert extra["stock_count"] == 4
    assert extra["up_count"] == 2 and extra["down_count"] == 1
    assert extra["flat_count"] == 1
    assert extra["up_ratio"] == 50.0
    assert extra["cyb_turnover_yi"] == 30.0  # (2e9+1e9)/1e8
    assert extra["kcb_turnover_yi"] == 52.0  # (5e9+2e8)/1e8
    assert extra["top5_gainers"][0]["code"] == "300001"
    assert extra["source"] == "东方财富clist"


@pytest.mark.asyncio
async def test_spot_summary_clist_uses_fltt2(patch_paged_httpx):
    """clist必须带fltt=2：否则涨跌幅按×100整数返回导致口径错位。"""
    patch_paged_httpx({1: {"total": 1, "diff": [
        {"f12": "300001", "f14": "甲", "f3": 5.0, "f6": 1e9, "f8": 2.0}]}})
    c = StarChinextConnector()
    await c.fetch("mkt:cybkcb:spot_summary")
    assert _PagedClistClient.urls
    assert all("fltt=2" in u for u in _PagedClistClient.urls)


@pytest.mark.asyncio
async def test_spot_summary_partial_page_accepted_near_total(patch_paged_httpx):
    """尾页断连但已拉取≥80%total：接受部分数据（东财口径）。"""
    patch_paged_httpx({
        1: {"total": 110, "diff": [
            {"f12": f"300{i:03d}", "f14": f"创{i}", "f3": 1.0,
             "f6": 1e8, "f8": 2.0} for i in range(100)]},
        # 第2页缺页 → 抛ConnectionError → 100 >= 0.8*110 接受部分
    })
    c = StarChinextConnector()
    pts = await c.fetch("mkt:cybkcb:spot_summary")
    extra = pts[0].extra
    assert extra["stock_count"] == 100
    assert extra["source"] == "东方财富clist"


@pytest.mark.asyncio
async def test_spot_summary_partial_below_80pct_falls_to_sina(
    patch_paged_httpx, monkeypatch,
):
    """首页后断连且100/1000<80%接受线：拒绝部分数据，整链降级到新浪。"""
    patch_paged_httpx({
        1: {"total": 1000, "diff": [
            {"f12": f"300{i:03d}", "f14": f"创{i}", "f3": 1.0,
             "f6": 1e8, "f8": 2.0} for i in range(100)]},
    })

    def _fail_akshare():
        raise ConnectionError("akshare(东财上游)同样被阻断")

    monkeypatch.setattr(StarChinextConnector, "_spot_akshare", _fail_akshare)

    async def _fake_sina(self):
        return {"stock_count": 2024, "up_ratio": 51.5, "up_count": 1042}

    monkeypatch.setattr(StarChinextConnector, "_spot_sina", _fake_sina)
    c = StarChinextConnector()
    pts = await c.fetch("mkt:cybkcb:spot_summary")
    assert pts[0].extra["source"] == "新浪行情列表"
    assert pts[0].value == 51.5
    assert pts[0].extra["stock_count"] == 2024


@pytest.mark.asyncio
async def test_spot_summary_fallback_to_akshare(patch_httpx, monkeypatch):
    patch_httpx({}, fail=True)

    def _fake_cy():
        return pd.DataFrame({
            "代码": ["300001"], "名称": ["甲"], "涨跌幅": [3.0],
            "成交额": [2e9], "换手率": [5.0],
        })

    def _fake_kcb():
        return pd.DataFrame({
            "代码": ["688001"], "名称": ["乙"], "涨跌幅": [-1.0],
            "成交额": [1e9], "换手率": [2.0],
        })

    monkeypatch.setattr("akshare.stock_cy_a_spot_em", _fake_cy)
    monkeypatch.setattr("akshare.stock_zh_kcb_spot", _fake_kcb)
    c = StarChinextConnector()
    pts = await c.fetch("mkt:cybkcb:spot_summary")
    extra = pts[0].extra
    assert extra["stock_count"] == 2
    assert extra["source"].startswith("AKShare")


class _SinaClient:
    """按node/page路由的假httpx客户端（新浪列表接口）；非新浪URL一律断连。"""

    pages: dict[tuple[str, int], list] = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def aclose(self) -> None:
        return None

    async def get(self, url: str):
        import re as _re
        if "Market_Center.getHQNodeData" not in url:
            raise ConnectionError("server disconnected")  # 模拟东财被阻断
        node = "cyb" if "node=cyb" in url else "kcb"
        m = _re.search(r"page=(\d+)", url)
        batch = _SinaClient.pages.get((node, int(m.group(1)) if m else 1))
        if batch is None:
            raise ConnectionError("server disconnected")
        return _FakeResponse(payload=batch)


@pytest.mark.asyncio
async def test_spot_summary_full_chain_to_sina(monkeypatch):
    """东财+AKShare（同上游）均被阻断 → 新浪第三源兜底，真值字段聚合。"""
    _SinaClient.pages = {
        ("cyb", 1): [
            {"code": "300001", "name": "甲", "changepercent": 20.0,
             "amount": "2e9", "turnoverratio": "8.0"},  # 新浪数值可能为字符串
            {"code": "300002", "name": "乙", "changepercent": -1.5,
             "amount": 1e9, "turnoverratio": 3.0},
        ],
        ("cyb", 2): [],  # 空页终止分页
        ("kcb", 1): [
            {"code": "688001", "name": "丙", "changepercent": 2.0,
             "amount": 5e9, "turnoverratio": 12.0},
        ],
        ("kcb", 2): [],
    }
    monkeypatch.setattr(
        "src.infrastructure.connectors.star_chinext_connector.httpx.AsyncClient",
        _SinaClient,
    )

    def _fail_akshare():
        raise ConnectionError("akshare(东财上游)同样被阻断")

    monkeypatch.setattr(StarChinextConnector, "_spot_akshare", _fail_akshare)
    c = StarChinextConnector()
    pts = await c.fetch("mkt:cybkcb:spot_summary")
    extra = pts[0].extra
    assert extra["source"] == "新浪行情列表"
    assert extra["stock_count"] == 3
    assert extra["up_count"] == 2 and extra["down_count"] == 1
    assert extra["cyb_turnover_yi"] == 30.0  # (2e9+1e9)/1e8，amount字符串可解析
    assert extra["kcb_turnover_yi"] == 50.0
    assert extra["top5_gainers"][0]["chg_pct"] == 20.0  # 真值口径，非×100


@pytest.mark.asyncio
async def test_spot_summary_paginates_full_market(monkeypatch):
    """clist单页上限100：分页循环拉全量（2页共105只）。"""
    import src.infrastructure.connectors.star_chinext_connector as scm

    pages = {
        1: {"total": 105, "diff": [
            {"f12": f"300{i:03d}", "f14": f"创{i}", "f3": 1.0,
             "f6": 1e8, "f8": 2.0} for i in range(100)]},
        2: {"total": 105, "diff": [
            {"f12": f"688{i:03d}", "f14": f"科{i}", "f3": -1.0,
             "f6": 2e8, "f8": 3.0} for i in range(5)]},
    }

    class _PagedClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def aclose(self) -> None:
            return None

        async def get(self, url: str):
            import re as _re
            m = _re.search(r"pn=(\d+)", url)
            page = int(m.group(1)) if m else 1
            return _FakeResponse(payload={"data": pages[page]})

    monkeypatch.setattr(
        "src.infrastructure.connectors.star_chinext_connector.httpx.AsyncClient",
        _PagedClient,
    )
    c = scm.StarChinextConnector()
    pts = await c.fetch("mkt:cybkcb:spot_summary")
    extra = pts[0].extra
    assert extra["stock_count"] == 105
    assert extra["cyb_turnover_yi"] == 100.0
    assert extra["kcb_turnover_yi"] == 10.0


# ---------- 截面统计纯函数 ----------

def test_summarize_rows_edge_cases():
    assert _summarize_rows([])["stock_count"] == 0
    rows = [{"f12": "301999", "f14": "次新", "f3": 20.0, "f6": 0.0, "f8": 0.0}]
    s = _summarize_rows(rows)
    assert s["cyb_turnover_yi"] == 0.0
    assert s["up_ratio"] == 100.0
