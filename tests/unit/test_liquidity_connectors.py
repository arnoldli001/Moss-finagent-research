"""估值分位/两融/北向/FedWatch连接器测试：假akshare与假cme_fedwatch，不联网。"""

from __future__ import annotations

import math
import sys
import types

import pandas as pd
import pytest

from src.infrastructure.connectors.fedwatch_connector import FedWatchConnector
from src.infrastructure.connectors.index_valuation_connector import (
    IndexValuationConnector,
    percentile_rank,
)
from src.infrastructure.connectors.margin_trading_connector import (
    MarginTradingConnector,
)
from src.infrastructure.connectors.northbound_flow_connector import (
    NorthboundFlowConnector,
)

# ---------------- 指数估值分位 ----------------

class _FakeAkValuation:
    # 乐咕symbol_map不支持的指数（真实akshare会抛KeyError(symbol)）
    unsupported = {"科创50", "创业板指", "上证综指", "深证成指"}

    def __init__(self, *, fail_names: set[str] | None = None,
                 attr_blocked: set[str] | None = None):
        self.fail_names = fail_names or set()
        self.attr_blocked = attr_blocked or set()

    def stock_index_pe_lg(self, symbol):
        if symbol in self.unsupported:
            raise KeyError(symbol)
        if symbol in self.attr_blocked:
            raise AttributeError("'NoneType' object has no attribute 'attrs'")
        if symbol in self.fail_names:
            raise ConnectionError("legu blocked")
        # 100期PE：最新值12.0处于历史中位附近
        return pd.DataFrame({
            "日期": [f"2026-0{(i // 30) + 6}-{i % 28 + 1:02d}" for i in range(100)],
            "滚动市盈率": [10.0 + i * 0.05 for i in range(100)],
        })

    def stock_index_pb_lg(self, symbol):
        if symbol in self.fail_names:
            raise ConnectionError("legu blocked")
        return pd.DataFrame({
            "日期": [f"2026-0{(i // 30) + 6}-{i % 28 + 1:02d}" for i in range(100)],
            "市净率": [1.0 + i * 0.005 for i in range(100)],
        })


def test_percentile_rank_known_series():
    assert percentile_rank([1.0, 2.0, 3.0, 4.0], 2.0) == 50.0
    assert percentile_rank([1.0, 2.0, 3.0], 3.0) == 100.0
    assert percentile_rank([], 1.0) is None


@pytest.mark.asyncio
async def test_index_valuation_pe_with_percentiles(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", _FakeAkValuation())
    pts = await IndexValuationConnector().fetch("idx_val:pe_ttm:沪深300")
    assert len(pts) == 1
    p = pts[0]
    assert p.unit == "倍"
    assert p.value == pytest.approx(14.95, abs=0.01)  # 10+99*0.05
    assert p.extra["index_name"] == "沪深300"
    # 单指标请求只拉PE（快照才并发拉PB）
    assert p.extra["pb"] is None
    assert set(p.extra) >= {"pe_pct_1y", "pe_pct_3y", "pe_pct_5y", "pe_pct_all"}
    assert p.extra["pe_pct_all"] == 100.0  # 单调递增序列最新值最大


@pytest.mark.asyncio
async def test_index_valuation_snapshot_fallback_and_skip(monkeypatch):
    # 测试不等待真实网络退避/错峰
    monkeypatch.setattr(
        "src.infrastructure.connectors.index_valuation_connector._RETRY_SLEEP_SEC", 0)
    monkeypatch.setattr(
        "src.infrastructure.connectors.index_valuation_connector._SNAPSHOT_STAGGER_SEC", 0)

    # 中证官网兜底（桩，禁止测试联网）：返回(日期, PE滚动, PE静态)
    def fake_csindex(name):
        return ("2026-09-12", 9.9, 9.8)

    monkeypatch.setattr(IndexValuationConnector, "_csindex_pe",
                        staticmethod(fake_csindex))
    # 沪深300乐咕被反爬（中证系列5指数可兜底）；创业板50为国证指数仅乐咕源
    # 中证500触发乐咕挑战页（AttributeError），应不重试立即兜底
    fake = _FakeAkValuation(fail_names={"沪深300"},
                            attr_blocked={"中证500"})
    monkeypatch.setitem(sys.modules, "akshare", fake)
    conn = IndexValuationConnector()
    pts = await conn.fetch("idx_val:snapshot:all")
    assert len(pts) == 6  # 乐咕失败也有中证现值兜底，6/6不缺
    hs = next(p for p in pts if p.extra["index_name"] == "沪深300")
    assert hs.value == 9.9
    assert hs.extra.get("percentiles_unavailable") is True
    zz500 = next(p for p in pts if p.extra["index_name"] == "中证500")
    assert zz500.value == 9.9
    assert zz500.extra.get("percentiles_unavailable") is True
    # 科创50：乐咕不支持（KeyError立即抛出，无重试）→ 同样走兜底
    kc = next(p for p in pts if p.extra["index_name"] == "科创50")
    assert kc.value == 9.9
    assert kc.extra.get("percentiles_unavailable") is True
    # 乐咕正常的指数（含创业板50）带5年分位
    legu_pts = [p for p in pts if not p.extra.get("percentiles_unavailable")]
    assert len(legu_pts) == 3
    assert all(p.extra.get("pe_pct_5y") is not None for p in legu_pts)

    # 中证兜底也失败 → 沪深300/科创50跳过（不阻断其他指数）
    def boom(name):
        raise ConnectionError("csindex down")

    monkeypatch.setattr(IndexValuationConnector, "_csindex_pe",
                        staticmethod(boom))
    pts2 = await conn.fetch("idx_val:snapshot:all")
    got2 = {p.extra["index_name"] for p in pts2}
    assert not ({"沪深300", "中证500", "科创50"} & got2)
    assert len(pts2) == 3


def test_valuation_supports():
    assert IndexValuationConnector.supports("idx_val:pb:创业板指")
    assert IndexValuationConnector.supports("idx_val:snapshot:all")
    assert not IndexValuationConnector.supports("mkt:turnover:total")


# ---------------- 两融余额 ----------------

class _FakeAkMargin:
    def __init__(self, *, szse_fail: bool = False):
        self.szse_calls: list[str] = []
        self.szse_fail = szse_fail

    def stock_margin_sse(self, start_date, end_date):
        # 单位元；两行，降序由连接器负责
        return pd.DataFrame({
            "信用交易日期": ["20260911", "20260910"],
            "融资余额": [1.8e12, 1.81e12],
            "融资买入额": [5.0e10, 5.2e10],
            "融券余量": [1e8, 1e8],
            "融券余量金额": [1.2e10, 1.2e10],
            "融券卖出量": [1e6, 1e6],
            "融资融券余额": [1.9e12, 1.91e12],  # 19000亿 / 19100亿
        })

    def stock_margin_szse(self, date):
        self.szse_calls.append(date)
        if self.szse_fail:
            raise ValueError("empty response")
        return pd.DataFrame({
            "融资买入额": [737.18],
            "融资余额": [12632.47],
            "融券卖出量": [0.3],
            "融券余量": [11.94],
            "融券余额": [105.58],
            "融资融券余额": [12738.05],  # 亿元
        })


@pytest.mark.asyncio
async def test_margin_balance_sh_plus_sz(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", _FakeAkMargin())
    pts = await MarginTradingConnector().fetch("mkt:margin_balance")
    p = pts[0]
    assert p.period_date == "2026-09-11"
    assert p.value == pytest.approx(19000.0 + 12738.05, abs=0.1)
    assert p.extra["sh_yi"] == 19000.0
    assert p.extra["sz_yi"] == 12738.05
    assert p.extra["coverage"] == "沪深两市"


@pytest.mark.asyncio
async def test_margin_balance_degrades_to_sh_only(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "akshare", _FakeAkMargin(szse_fail=True))
    pts = await MarginTradingConnector().fetch("mkt:margin_balance")
    assert pts[0].value == 19000.0
    assert pts[0].extra["coverage"] == "仅沪市（深市暂缺）"
    assert pts[0].confidence == 0.7


@pytest.mark.asyncio
async def test_margin_hist_series(monkeypatch):
    fake = _FakeAkMargin()
    monkeypatch.setitem(sys.modules, "akshare", fake)
    pts = await MarginTradingConnector().fetch("mkt:margin_balance:hist")
    assert len(pts) == 2
    assert pts[0].period_date == "2026-09-11"  # 最新在前
    assert fake.szse_calls  # 每日都尝试了深市


# ---------------- 北向资金 ----------------

class _FakeAkNorth:
    def stock_hsgt_hist_em(self, symbol):
        return pd.DataFrame({
            "日期": ["2024-08-15", "2024-08-16", "2026-09-10", "2026-09-11"],
            "当日成交净买额": [30.1, 42.3, math.nan, math.nan],
            "买入成交额": [500.0, 520.0, math.nan, math.nan],
            "卖出成交额": [469.9, 477.7, math.nan, math.nan],
        })


class _FakeAkNorthNormal:
    def stock_hsgt_hist_em(self, symbol):
        return pd.DataFrame({
            "日期": ["2026-09-10", "2026-09-11"],
            "当日成交净买额": [-10.0, 25.5],
            "买入成交额": [500.0, 530.0],
            "卖出成交额": [510.0, 504.5],
        })


@pytest.mark.asyncio
async def test_north_flow_halted_uses_last_valid(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", _FakeAkNorth())
    pts = await NorthboundFlowConnector().fetch("mkt:north_flow")
    p = pts[0]
    assert p.value == 42.3
    assert p.period_date == "2024-08-16"
    assert p.extra["status"] == "disclosure_halted"
    assert p.extra["halted_since"] == "2026-09-10"
    assert p.confidence == 0.5


@pytest.mark.asyncio
async def test_north_flow_hist_excludes_nan(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", _FakeAkNorth())
    pts = await NorthboundFlowConnector().fetch("mkt:north_flow:hist")
    assert len(pts) == 2
    assert pts[-1].period_date == "2024-08-16"
    assert pts[0].extra["status"] == "disclosure_halted"


@pytest.mark.asyncio
async def test_north_flow_normal_status(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", _FakeAkNorthNormal())
    pts = await NorthboundFlowConnector().fetch("mkt:north_flow")
    assert pts[0].value == 25.5
    assert pts[0].extra["status"] == "normal"
    assert pts[0].confidence == 0.85


# ---------------- CME FedWatch ----------------

def _fake_fed_module(prob_data: dict | None = None, raise_exc: Exception | None = None):
    def get_probabilities(meeting):
        if raise_exc:
            raise raise_exc
        return prob_data

    mod = types.SimpleNamespace(get_probabilities=get_probabilities)
    return mod


@pytest.fixture
def _fed_probe_stubbed(monkeypatch):
    """钉死 TCP 预检 + 清失败冷却，让 FedWatch 用例真正离线。

    connector 在调 `cme_fedwatch` 之前会**真探** `www.cmegroup.com:443`
    （2026-09-26 新增的快失败预检）。该主机可达性随网络环境漂移
    （实测同一天时通时断），不放桩的话"离线解析"用例就成了网络敏感用例；
    另外失败冷却是**进程级单例**，本文件或前序用例记下的失败会把
    fetch 直接短路成空列表 —— 前后都要清。
    """
    from src.infrastructure.connectors import net_probe
    from src.infrastructure.connectors.source_cooldown import get_cooldown

    async def _reachable(host, port, timeout=net_probe.DEFAULT_PROBE_TIMEOUT):
        return True

    monkeypatch.setattr(net_probe, "probe_tcp", _reachable)
    get_cooldown().clear()
    yield
    get_cooldown().clear()


@pytest.mark.asyncio
async def test_fedwatch_parses_probabilities(_fed_probe_stubbed, monkeypatch):
    data = {
        "effr": 3.64, "current_target": "3.50%-3.75%",
        "meetings": [{
            "date": "2026-10-28", "contract": "ZQX6",
            "probabilities": {
                "3.25%-3.50%": 30.0, "3.50%-3.75%": 60.0, "3.75%-4.00%": 10.0,
            },
        }],
    }
    monkeypatch.setitem(
        sys.modules, "cme_fedwatch", _fake_fed_module(data))
    pts = await FedWatchConnector().fetch("fed:rate_prob:next")
    assert len(pts) == 3
    top = pts[0]
    assert top.value == 60.0
    assert top.period_date == "2026-10-28"
    assert top.extra["cut_prob"] == 30.0
    assert top.extra["hold_prob"] == 60.0
    assert top.extra["hike_prob"] == 10.0
    assert top.extra["dominant_range"] == "3.50%-3.75%"
    assert top.extra["rate_range"] == "3.50%-3.75%"


@pytest.mark.asyncio
async def test_fedwatch_unreachable_returns_empty(_fed_probe_stubbed, monkeypatch):
    mod = _fake_fed_module(raise_exc=TimeoutError("connect timeout"))
    monkeypatch.setitem(sys.modules, "cme_fedwatch", mod)
    pts = await FedWatchConnector().fetch("fed:rate_prob:next")
    assert pts == []


def test_fedwatch_supports():
    assert FedWatchConnector.supports("fed:rate_prob:next")
    assert FedWatchConnector.supports("fed:rate_prob:2026-10-28")
    assert not FedWatchConnector.supports("us_fed_rate")
