"""A股流动性连接器测试：腾讯GBK行情/东财fallback/clist聚合/日K解析，不联网。"""

from __future__ import annotations

import pytest

from src.infrastructure.connectors.a_share_liquidity_connector import (
    AShareLiquidityConnector,
)


def _tencent_line(code: str, name: str, amount_wan: str) -> str:
    """构造腾讯行情报文（真实格式 v_code="1~名称~...~成交额万元..."，共88段）。"""
    parts = ["0"] * 40
    parts[0] = "1"
    parts[1] = name
    parts[37] = amount_wan  # split("~")后索引37=成交额（万元）
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
            "src.infrastructure.connectors.a_share_liquidity_connector.httpx.AsyncClient",
            _FakeAsyncClient,
        )
    return _patch


@pytest.mark.asyncio
async def test_supports_indicator_routing():
    assert AShareLiquidityConnector.supports("mkt:turnover:total")
    assert AShareLiquidityConnector.supports("mkt:turnover:hist")
    assert AShareLiquidityConnector.supports("mkt:turnover_rate:all_a")
    assert not AShareLiquidityConnector.supports("mkt:margin_balance")
    assert not AShareLiquidityConnector.supports("ind:动力煤价格(元/吨)")


@pytest.mark.asyncio
async def test_turnover_total_from_tencent(patch_httpx):
    # 上证10000亿（1e8万元）、深成6000亿、创业板1800亿、科创50 900亿
    lines = "".join([
        _tencent_line("sh000001", "上证指数", "100000000"),
        _tencent_line("sz399001", "深证成指", "60000000"),
        _tencent_line("sz399006", "创业板指", "18000000"),
        _tencent_line("sh000688", "科创50", "9000000"),
    ])
    patch_httpx({"qt.gtimg.cn": _FakeResponse(content=lines.encode("gbk"))})

    c = AShareLiquidityConnector()
    pts = await c.fetch("mkt:turnover:total")
    assert len(pts) == 1
    p = pts[0]
    assert p.unit == "亿元"
    assert p.value == 16000.0  # 10000+6000
    assert p.extra["cyb"] == 1800.0
    assert p.extra["kcb"] == 900.0
    assert p.extra["source"] == "腾讯财经"


@pytest.mark.asyncio
async def test_turnover_total_fallback_to_eastmoney(patch_httpx):
    # 腾讯无预置→断连；东财push2返回f48（元），沪深各8000亿
    patch_httpx({"push2.eastmoney.com": _FakeResponse(
        payload={"data": {"f48": 8.0e11}})})
    c = AShareLiquidityConnector()
    pts = await c.fetch("mkt:turnover:total")
    assert pts[0].value == 16000.0
    assert pts[0].extra["source"] == "东方财富push2"


@pytest.mark.asyncio
async def test_all_a_rate_weighted_and_concentration(patch_httpx):
    diff = [
        {"f6": 1.0e10, "f21": 4.0e11},  # 2.5%换手
        {"f6": 2.0e10, "f21": 8.0e11},  # 2.5%
        {"f6": 3.0e10, "f21": 1.2e12},  # 2.5%
    ]
    patch_httpx({"clist/get": _FakeResponse(
        payload={"data": {"diff": diff}})})
    c = AShareLiquidityConnector()
    pts = await c.fetch("mkt:turnover_rate:all_a")
    assert pts[0].value == pytest.approx(2.5, abs=0.01)
    # 前5%取max(1,int(3*0.05))=1只：最大3e10/总6e10=50%
    assert pts[0].extra["top5pct_concentration_pct"] == 50.0


@pytest.mark.asyncio
async def test_clist_failure_falls_back_to_constant(patch_httpx):
    """所有 HTTP 都挂了（clist + 腾讯都断）时，终极降级返回常数代理。"""
    patch_httpx({}, fail=True)
    c = AShareLiquidityConnector()
    # 现在不再 raise，而是返回 confidence=0.5 的常数代理
    pts = await c.fetch("mkt:turnover_rate:all_a")
    assert len(pts) == 1
    assert pts[0].confidence <= 0.5
    assert pts[0].fetch_method.value == "fallback"
    assert pts[0].extra.get("source") == "fallback_constant"


def _kline_resp(rows: list[str]) -> _FakeResponse:
    return _FakeResponse(payload={"data": {"klines": rows}})


@pytest.mark.asyncio
async def test_turnover_hist_sums_sh_sz_kline(patch_httpx):
    sh = _kline_resp([
        "2026-09-11,3910,3888,3912,3852,579123145,958186336970.1,1.5,-1.18,-46,1.19",
        "2026-09-14,3867,3885,3895,3867,458888916,779281246497.2,0.73,-0.07,-2.78,0.95",
    ])
    sz = _kline_resp([
        "2026-09-11,1,2,3,4,5,800000000000,0,0,0,1.5",
        "2026-09-14,1,2,3,4,5,850000000000,0,0,0,1.6",
    ])
    patch_httpx({"secid=1.000001": sh, "secid=0.399001": sz})
    c = AShareLiquidityConnector()
    pts = await c.fetch("mkt:turnover:hist")
    assert [p.period_date for p in pts] == ["2026-09-11", "2026-09-14"]
    assert pts[0].value == pytest.approx(17581.86, abs=0.1)  # 9581.86+8000
    assert pts[0].extra["sh"] == pytest.approx(9581.86, abs=0.1)
    assert pts[1].extra["sz"] == pytest.approx(8500.0, abs=0.1)


@pytest.mark.asyncio
async def test_turnover_rate_hist_proxy(patch_httpx):
    row = "2026-09-14,3867,3885,3895,3867,458888916,779000000000,0.73,-0.07,-2.78,0.95"
    patch_httpx({"kline/get": _kline_resp([row])})
    c = AShareLiquidityConnector()
    pts = await c.fetch("mkt:turnover_rate:hist")
    assert len(pts) == 1
    assert pts[0].value == 0.95
    assert "代理" in pts[0].extra["proxy"]
