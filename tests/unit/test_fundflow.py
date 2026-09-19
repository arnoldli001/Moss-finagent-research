"""资金流监控单元测试（离线：注入假 provider，不碰网络/仓库）。

要钉住的是四件会真正影响用户判断的事：

  1. **排序口径**：个股榜必须按「净额均值 / 流通市值」而不是绝对额
     —— 否则榜单永远是大市值股票的天下，用户要的"大资金动向"就看不见了；
  2. **只算触及过的数据**：缺流通市值的票不能静默混进比值榜，要单列并说明；
  3. **选择持久化**：加入/移除幂等；默认热门**只播种一次**，用户删掉的不会被塞回来；
  4. **失败即缺口**：取不到就空着并写清楚原因，绝不用旧值顶替。
"""

from __future__ import annotations

import os

import pytest

from src.fundflow.models import FlowBoard, FlowEntity, FlowPoint
from src.fundflow.service import FundFlowService, session_state
from src.infrastructure.repositories.fund_flow_sqlite_repo import (
    FlowWatchEntry,
    FlowWatchRepository,
)


class FakeProvider:
    """假取数器：所有数据由测试注入，绝不发网络请求。

    接口与真实 `FundFlowProvider` 对齐（板块截面走 Tushare 东财口径，
    历史优先 ts_code 查询，东财/本地聚合是备用）。
    """

    def __init__(self, *, snapshot=None, histories=None, frame=None, caps=None,
                 stock_series=None, tushare_histories=None, limit_up=None,
                 quotes=None):
        self._snapshot = snapshot or {}
        self._histories = histories or {}
        self._tushare_histories = tushare_histories or {}
        self._frame = frame
        self._caps = caps
        self._stock_series = stock_series or {}
        # 昨日涨停（仓库自算）与盘中快照：默认空 —— 让用例显式说明它测的是哪条分支
        self._limit_up = limit_up or {}
        self._quotes = quotes or {}
        self.sector_history_calls: list[str] = []

    def limit_up_codes_from_warehouse(self, **_kwargs):
        return dict(self._limit_up)

    async def fetch_tencent_snapshot(  # pragma: no cover - 由 service 内部 monkeypatch
        self, codes
    ):
        return {code: self._quotes[code] for code in codes if code in self._quotes}

    async def sector_snapshot(self):
        return self._snapshot

    async def sector_history_tushare(self, name, code="", *, window_days=10,
                                     force=False):
        return self._tushare_histories.get(name)

    async def sector_history(self, name, *, window_days=10, force=False):
        self.sector_history_calls.append(name)
        return self._histories.get(name) or FlowEntity(
            kind="sector", code=name, name=name, gap="测试：无该板块历史")

    async def sector_history_local(self, name, *, window_days=10, max_members=60):
        return None

    async def sector_history_many(self, names, *, window_days=10):
        out = {}
        for name in names:
            out[name] = await self.sector_history(name, window_days=window_days)
        return out

    async def stock_frame(self, *, window_days=10):
        return self._frame, self._caps

    async def stock_series(self, codes, *, window_days=10):
        if not codes:
            return dict(self._stock_series)
        return {code: self._stock_series[code] for code in codes
                if code in self._stock_series}

    async def stock_realtime(self, codes):
        return {}


def _snapshot_item(name: str, net: float, **kwargs) -> dict:
    """一条板块截面（Tushare moneyflow_ind_dc 口径，net 单位元）。"""
    return {"code": kwargs.pop("code", f"BK{abs(hash(name)) % 9999:04d}.DC"),
            "trade_date": kwargs.pop("trade_date", "20260916"),
            "net": net, "net_rate": kwargs.pop("net_rate", 1.0),
            "buy_elg": None, "buy_lg": None,
            "pct_change": kwargs.pop("pct_change", 0.5),
            "rank": kwargs.pop("rank", None), "content_type": "行业"}


@pytest.fixture
def repo(tmp_dir: str) -> FlowWatchRepository:
    return FlowWatchRepository(path=os.path.join(tmp_dir, "fundflow.db"))


def _entity(name: str, nets: list[float], **kwargs) -> FlowEntity:
    entity = FlowEntity(kind=kwargs.pop("kind", "sector"), code=name, name=name,
                        available=True,
                        data_source=kwargs.pop("data_source", "东财板块资金流历史"),
                        **kwargs)
    entity.series = [FlowPoint(date=f"2026-09-{index + 1:02d}", net=value)
                     for index, value in enumerate(nets)]
    nets_only = [point.net for point in entity.series if point.net is not None]
    entity.net_avg = sum(nets_only) / len(nets_only)
    entity.latest_net = nets_only[-1]
    entity.latest_date = entity.series[-1].date
    return entity


# ==================== 时段口径 ====================

def test_session_state_covers_all_phases() -> None:
    from datetime import datetime

    cases = {
        "2026-09-17 08:30": ("pre_open", "盘前"),
        "2026-09-17 09:20": ("call_auction", "集合竞价"),
        "2026-09-17 10:00": ("trading", "交易中"),
        "2026-09-17 12:00": ("lunch_break", "午间休市"),
        "2026-09-17 14:30": ("trading", "交易中"),
        "2026-09-17 16:00": ("closed", "已收盘"),
        "2026-09-19 10:00": ("closed", "周末休市"),
    }
    for text, expected in cases.items():
        assert session_state(datetime.strptime(text, "%Y-%m-%d %H:%M")) == expected


# ==================== 排序口径 ====================

@pytest.mark.asyncio
async def test_stock_rank_uses_net_to_market_cap_ratio(repo, monkeypatch) -> None:
    """大市值小幅净流入 vs 小市值大幅净流入：后者必须排前面（这才是"大资金动向"）。"""
    import pandas as pd

    # `_realtime_snapshot` 内部 `from src.fundflow.provider import fetch_tencent_snapshot`
    # → 必须 monkeypatch **provider 模块里的名字**（打 service 没用），
    # 否则会真的去打腾讯（实测会拿到真实行情，断言随行情漂移）。
    from src.fundflow import provider as provider_module

    async def fake_quote(codes):        # noqa: ANN001
        return {"300001": {"change_pct": 3.5, "circ_mv": 2e9,
                           "source": "腾讯行情(qt.gtimg.cn)"}}

    async def fake_pool(trade_date=""):  # noqa: ANN001
        return {"600000": {"theme": "银行", "streak": 2, "pool_date": "20260917"}}

    monkeypatch.setattr(provider_module, "fetch_tencent_snapshot", fake_quote)
    monkeypatch.setattr(provider_module, "fetch_limit_up_pool", fake_pool)

    frame = pd.DataFrame({
        "code": ["600000", "600000", "300001", "300001"],
        "date": ["20260916", "20260917", "20260916", "20260917"],
        "net": [1e8, 1e8, 2e7, 2e7],
        "buy_lg": [0, 0, 0, 0], "sell_lg": [0, 0, 0, 0],
        "buy_elg": [0, 0, 0, 0], "sell_elg": [0, 0, 0, 0],
    })
    caps = pd.DataFrame({"code": ["600000", "300001"],
                         "circ_mv": [1e11, 2e9]})   # 1000亿 vs 20亿
    provider = FakeProvider(
        frame=frame, caps=caps,
        # 昨日涨停给一只（600000，市值 1000 亿 ≥30 亿门槛）：它会排在最前
        # —— 这正是"昨日涨停股优先"的新口径，排序断言里要体现。
        limit_up={"600000": "20260916"},
        stock_series={
            "600000": {"points": [{"date": "20260916", "net": 1e8},
                                  {"date": "20260917", "net": 1e8}],
                       "circ_mv": 1e11},
            "300001": {"points": [{"date": "20260916", "net": 2e7},
                                  {"date": "20260917", "net": 2e7}],
                       "circ_mv": 2e9},
        })
    service = FundFlowService(provider=provider, repo=repo)
    rank, series, gaps = await service._rank_stocks(  # noqa: SLF001
        window_days=10, top=20, extra_codes=[])
    by_code = {item.code: item for item in rank}
    # 「昨日涨停」优先：600000 排第一，即使它的"净额/市值"比值更低
    assert [item.code for item in rank] == ["600000", "300001"]
    assert by_code["600000"].rank_group == "昨日涨停"
    assert by_code["300001"].rank_group == "净流入前10"
    assert by_code["300001"].change_pct == pytest.approx(3.5)
    assert by_code["300001"].change_source.startswith("腾讯")
    assert by_code["300001"].limitup_reason == ""      # 非涨停股没有原因
    assert rank[0].net_to_mv == pytest.approx(1e8 / 1e11)
    assert rank[1].net_to_mv == pytest.approx(2e7 / 2e9)
    assert not gaps
    assert set(series) == {"600000", "300001"}


@pytest.mark.asyncio
async def test_stock_rank_reports_missing_market_cap(repo) -> None:
    """缺流通市值的票不能静默混进比值榜：要单列并写清楚。"""
    import pandas as pd

    frame = pd.DataFrame({
        "code": ["600000", "999999"], "date": ["20260917", "20260917"],
        "net": [1e8, 5e7],
        "buy_lg": [0, 0], "sell_lg": [0, 0], "buy_elg": [0, 0], "sell_elg": [0, 0],
    })
    caps = pd.DataFrame({"code": ["600000"], "circ_mv": [1e11]})
    provider = FakeProvider(
        frame=frame, caps=caps,
        limit_up={"600000": "20260916"},     # 有涨停数据 → 不报缺口
        stock_series={
            "600000": {"points": [{"date": "20260917", "net": 1e8}], "circ_mv": 1e11},
            "999999": {"points": [{"date": "20260917", "net": 5e7}], "circ_mv": None},
        })
    service = FundFlowService(provider=provider, repo=repo)
    rank, _series, gaps = await service._rank_stocks(  # noqa: SLF001
        window_days=10, top=20, extra_codes=[])
    codes = [item.code for item in rank]
    assert codes[0] == "600000"          # 昨日涨停 + 有市值（比值口径）
    assert "999999" in codes             # 没市值的仍然出现，但排在后面
    assert rank[-1].net_to_mv is None
    assert not gaps                      # 涨停与市值都取到了，不该报缺口


@pytest.mark.asyncio
async def test_stock_rank_without_caps_degrades_and_says_so(repo) -> None:
    import pandas as pd

    frame = pd.DataFrame({
        "code": ["600000"], "date": ["20260917"], "net": [1e8],
        "buy_lg": [0], "sell_lg": [0], "buy_elg": [0], "sell_elg": [0],
    })
    provider = FakeProvider(
        frame=frame, caps=pd.DataFrame(),
        stock_series={"600000": {"points": [{"date": "20260917", "net": 1e8}],
                                 "circ_mv": None}})
    service = FundFlowService(provider=provider, repo=repo)
    rank, _series, gaps = await service._rank_stocks(  # noqa: SLF001
        window_days=10, top=20, extra_codes=[])
    assert [item.code for item in rank] == ["600000"]
    assert any("流通市值" in gap for gap in gaps)


@pytest.mark.asyncio
async def test_stock_rank_without_warehouse_reports_command(repo) -> None:
    import pandas as pd

    provider = FakeProvider(frame=pd.DataFrame(), caps=pd.DataFrame())
    service = FundFlowService(provider=provider, repo=repo)
    rank, series, gaps = await service._rank_stocks(  # noqa: SLF001
        window_days=10, top=20, extra_codes=[])
    assert rank == [] and series == {}
    assert gaps and "quant_warehouse.py" in gaps[0], "缺口必须给出可执行的补数命令"


@pytest.mark.asyncio
async def test_extra_watch_codes_always_in_rank(repo) -> None:
    """用户手动加的票必须在榜单里（即使没进前 N）。"""
    import pandas as pd

    frame = pd.DataFrame({
        "code": [f"60000{index}" for index in range(1, 6)],
        "date": ["20260917"] * 5,
        "net": [5e7, 4e7, 3e7, 2e7, 1e7],
        "buy_lg": [0] * 5, "sell_lg": [0] * 5, "buy_elg": [0] * 5, "sell_elg": [0] * 5,
    })
    caps = pd.DataFrame({"code": [f"60000{index}" for index in range(1, 6)],
                         "circ_mv": [1e9 * index for index in range(1, 6)]})
    series = {f"60000{index}": {
        "points": [{"date": "20260917", "net": 5e7 / index}],
        "circ_mv": 1e9 * index} for index in range(1, 6)}
    provider = FakeProvider(frame=frame, caps=caps, stock_series=series)
    service = FundFlowService(provider=provider, repo=repo)
    rank, _series, _gaps = await service._rank_stocks(  # noqa: SLF001
        window_days=10, top=2, extra_codes=["600005"])
    codes = [item.code for item in rank]
    assert "600005" in codes, "用户手动加入的票不能因为不在前 N 就消失"


# ==================== 板块榜 ====================

def test_sector_rank_splits_influx_and_outflow(repo) -> None:
    histories = {
        "净流入板块": _entity("净流入板块", [1e9, 2e9]),
        "净流出板块": _entity("净流出板块", [-3e9, -1e9]),
        "零轴板块": _entity("零轴板块", [0.0, 0.0]),
    }
    snapshot = {"净流入板块": _snapshot_item("净流入板块", 5e8, net_rate=1.2,
                                          pct_change=1.2, rank=3)}
    service = FundFlowService(provider=FakeProvider(), repo=repo)
    ranked = service._rank_sectors(histories, snapshot, 2)  # noqa: SLF001
    by_name = {item.name: item for item in ranked}
    assert ranked[0].name == "净流入板块", "净流入段必须排在最前"
    assert ranked[0].today_net == pytest.approx(5e8)
    assert ranked[0].change_pct == pytest.approx(1.2)
    assert any("当日净占比" in note for note in ranked[0].notes)
    # 净流入段按"越大越靠前"，净流出段按"越负越靠前"（零轴 =0 排在负值之后）
    assert by_name["净流出板块"].net_avg is not None
    assert by_name["净流出板块"].net_avg < 0
    assert [item.name for item in ranked] == ["净流入板块", "净流出板块", "零轴板块"]


# ==================== 选择持久化 ====================

@pytest.mark.asyncio
async def test_watch_add_remove_is_idempotent(repo) -> None:
    items = await repo.add(FlowWatchEntry(kind="sector", code="CPO概念", name="CPO概念"))
    assert [item.code for item in items] == ["CPO概念"]
    again = await repo.add(FlowWatchEntry(kind="sector", code="CPO概念", name="CPO概念"))
    assert len(again) == 1, "重复加入不能变两条"
    removed, rest = await repo.remove("sector", "CPO概念")
    assert removed is True and rest == []
    removed_again, _ = await repo.remove("sector", "CPO概念")
    assert removed_again is False, "移除必须幂等"


@pytest.mark.asyncio
async def test_defaults_seeded_once_and_not_resurrected(repo) -> None:
    """默认热门只播种一次；用户删掉之后不会再被自动塞回来。"""
    provider = FakeProvider()
    service = FundFlowService(provider=provider, repo=repo)
    names = [f"板块{index}" for index in range(25)]
    await service.ensure_defaults(names=names, top=20)
    seeded = await repo.list("sector")
    assert len(seeded) == 20
    assert all(item.source == "default" for item in seeded)

    # 用户删掉第一个，再触发一次 ensure_defaults：不该把它塞回来
    victim = seeded[0].code
    await repo.remove("sector", victim)
    service._seeded = False        # 模拟"进程重启后再次进入页面"  # noqa: SLF001
    await service.ensure_defaults(names=names, top=20)
    remaining = [item.code for item in await repo.list("sector")]
    assert victim not in remaining
    assert len(remaining) == 19


def test_defaults_absolute_order_is_stream_flow_first(repo) -> None:
    """播种顺序按当日净额**绝对值**：净流出最多的板块同样是"热门"。"""
    realtime = {
        "流入王": _snapshot_item("流入王", 3e9),
        "流出王": _snapshot_item("流出王", -5e9),
        "小角色": _snapshot_item("小角色", 1e8),
    }
    ranked = [name for name, _ in sorted(
        ((name, data) for name, data in realtime.items()
         if data.get("net") is not None),
        key=lambda item: -abs(float(item[1]["net"])))]
    assert ranked == ["流出王", "流入王", "小角色"]


# ==================== 快照组装 ====================

@pytest.mark.asyncio
async def test_snapshot_reports_gap_when_realtime_missing(repo) -> None:
    histories = {"A板块": _entity("A板块", [1e9])}
    service = FundFlowService(
        provider=FakeProvider(snapshot={}, histories=histories), repo=repo)
    board = await service.snapshot(force=True)
    assert isinstance(board, FlowBoard)
    assert any("板块资金流截面不可用" in gap for gap in board.gaps)
    assert board.refresh_hint


@pytest.mark.asyncio
async def test_snapshot_seeds_defaults_and_builds_series(repo) -> None:
    snapshot = {"A板块": _snapshot_item("A板块", 3e8),
                "B板块": _snapshot_item("B板块", -2e8)}
    tushare = {"A板块": _entity("A板块", [1e9, 2e9]),
               "B板块": _entity("B板块", [-1e9, -2e9])}
    service = FundFlowService(
        provider=FakeProvider(snapshot=snapshot, tushare_histories=tushare),
        repo=repo)
    board = await service.snapshot(force=True)
    assert len(board.sectors) == 2
    assert {item.name for item in board.sectors} == {"A板块", "B板块"}
    assert board.source_notes, "必须说明数据来自哪里、多快更新"
    assert board.trade_date, "交易日要能回填给前端"


@pytest.mark.asyncio
async def test_sector_history_falls_back_when_tushare_missing(repo) -> None:
    """Tushare 历史取不到时退回东财口径，并在 notes 里写明降级原因。"""
    snapshot = {"A板块": _snapshot_item("A板块", 3e8)}
    service = FundFlowService(
        provider=FakeProvider(snapshot=snapshot,
                              histories={"A板块": _entity("A板块", [1e9])}),
        repo=repo)
    entity = await service._one_sector_history(  # noqa: SLF001
        "A板块", realtime=snapshot, window_days=10)
    assert entity.available
    assert entity.data_source, "降级后必须仍标注数据来源（前端要靠它说明口径）"
    assert service.provider.sector_history_calls == ["A板块"], "应走到东财备用口径"


@pytest.mark.asyncio
async def test_snapshot_uses_cache_unless_forced(repo) -> None:
    provider = FakeProvider(snapshot={"A板块": _snapshot_item("A板块", 1e8)},
                            tushare_histories={"A板块": _entity("A板块", [1e9])})
    service = FundFlowService(provider=provider, repo=repo)
    first = await service.snapshot()
    second = await service.snapshot()
    assert first is second, "60 秒内应命中缓存（盘中每 15 秒取一次不能都重算）"
    third = await service.snapshot(force=True)
    assert third is not first, "force=True 必须穿透缓存"


@pytest.mark.asyncio
async def test_snapshot_without_repo_still_serves_rank(repo) -> None:
    """仓储不可用（未装配）时：**不能因为没选过就整页空白** ——
    榜单仍要按当日截面给出（用户至少能看"钱在往哪去"），只是走势为空。"""
    provider = FakeProvider(snapshot={"A板块": _snapshot_item("A板块", 1e8)},
                            tushare_histories={"A板块": _entity("A板块", [1e9])})
    service = FundFlowService(provider=provider, repo=None)
    board = await service.snapshot(force=True)
    assert board.sectors == [], "没有选择列表 → 不画走势"
    assert [item.name for item in board.sector_rank] == ["A板块"]
    assert board.sector_rank[0].today_net == pytest.approx(1e8)
    assert board.sector_rank[0].gap, "没历史序列的要如实标注缺口"
