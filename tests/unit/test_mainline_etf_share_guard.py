"""ETF 份额启动自检（`src/mainline/etf_share_guard.py`）单元测试。

## 为什么这组测试值得写细

它守的是一个**每晚都会重演**的时序坑：份额次日 8:30 才发布，而日线当晚就有，
于是"份额发布前跑一次同步"必然给最新交易日留下一批 `shares = NULL` 的行；
面板以 `MAX(trade_date)` 为锚点只读最后一行，那几只 ETF 的份额、1/5/10/20 日
变化率、对应指数分位就**一起变空**（2026-09-22 实测：9 只监控里 7 只中招）。

所以这里钉的不是"有没有补上"，而是四条边界 —— 任何一条写错都会让它变成
"每天白打一轮接口"或"真缺口不补"：

1. **最新已收盘日的份额本来就没发布** → 必须判 fresh（不联网）。
   把这种情况当缺口，服务每晚 17:50 之后每次启动都会白打接口。
2. **基准日（前一个交易日）份额成片为 NULL** → 必须补，且**只 UPDATE 不插行**。
   插一行"行情有、份额无"的记录正是当初出事的机制。
3. **场外基金（`158008.OF`）长期留 NULL 不算缺口** → 判据贴着观测清单走。
4. **任何异常都只降级**：自检挂在启动路径上，它失败绝不能变成服务起不来。

时间一律**冻结**（`_freeze_now`）而不是 `datetime.now()`：`recent_closed_days`
要看"今天"和"15:00 前不算已收盘"，不冻结的话这组测试会在真实日期流逝后
自己变红 —— 那种红是最难查的一类。
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from src.mainline import etf_share_guard as guard
from src.mainline.config import load_config
from src.mainline.datastore import MainlineDataStore

#: 观测清单里的一只 ETF（判据按它走）与一只**场外基金**（份额长期不发布）
ETF = "510300.SH"
OF = "158008.OF"
#: 冻结的"现在"：2026-09-18（周五）15:00 之后 → 已收盘交易日到 20260918 为止
NOW = (2026, 9, 18, 16, 0)
#: 日历里登记的最后四个交易日（`recent_closed_days` 的 `days[0]/days[1]` 从它来）
DAYS = ("20260915", "20260916", "20260917", "20260918")
#: 更早的交易日：份额早就发布完了，用来把"正常状态"的缺口比例压到阈值之下。
#: 没有它，`ml_etf` 里就只有"缺份额的那几行"，缺口比例恒为 100% ——
#: 那会把"正常"与"真缺口"抹平成同一种输入，测试也就测不出区分能力了。
HISTORY = ("20260901", "20260902", "20260903", "20260904", "20260907",
           "20260908", "20260909", "20260910", "20260911", "20260914")


# ==================================================================
# 夹具：假 Tushare + 冻结时钟的本地库
# ==================================================================


class _Frame:
    """最小 DataFrame 替身：`sync_etf_shares` 只用 `.to_dict("records")`。"""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def to_dict(self, orient: str = "records") -> list[dict]:  # noqa: ARG002
        return list(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


class _FakeClient:
    """假 Tushare 客户端：只实现 `fund_share` 的两种入参形状。

    `raises_after`：前 N 次调用正常、之后抛错 —— 用来测"补份额时接口挂了"
    这条降级路径（只让全市场那次成功，是按代码逐只重试那一支挂掉）。
    """

    def __init__(self, *, market: dict[str, float] | None = None,
                 by_code: dict[str, float] | None = None,
                 raises: bool = False, raises_after: int = 0) -> None:
        self.market = dict(market or {})
        self.by_code = dict(by_code or {})
        self.raises = raises
        self.raises_after = raises_after
        self.calls: list[dict] = []

    def call(self, name: str, **kwargs):
        self.calls.append({"name": name, **kwargs})
        if self.raises or (self.raises_after
                           and len(self.calls) > self.raises_after):
            raise RuntimeError("tushare 不可用")
        if name != "fund_share":
            return _Frame([])
        if kwargs.get("ts_code"):
            code = str(kwargs["ts_code"])
            value = self.by_code.get(code)
            rows = ([{"ts_code": code, "trade_date": kwargs.get("end_date"),
                      "fd_share": value}] if value is not None else [])
            return _Frame(rows)
        day = str(kwargs.get("trade_date") or "")
        return _Frame([{"ts_code": code, "trade_date": day, "fd_share": value,
                        "fund_type": "OF" if code.endswith(".OF") else "ETF",
                        "market": "SH"}
                       for code, value in self.market.items()])


class _FakeRemote:
    def __init__(self, client: _FakeClient | None) -> None:
        self.tushare = None if client is None else _Tushare(client)


class _Tushare:
    def __init__(self, client: _FakeClient) -> None:
        self.client = client


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> _dt.datetime:
    """冻结 ``etf_share_guard`` 里的 ``datetime.now()``（见模块 docstring）。"""

    class _Frozen(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return cls(*NOW)

    fixed = _Frozen(*NOW)
    monkeypatch.setattr(guard, "datetime", _Frozen)
    return fixed


@pytest.fixture(autouse=True)
def _watchlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """判据只看我们造的代码，不读真实 `configs/etf_flow.yaml` 里的 9 只。"""
    monkeypatch.setattr(guard, "monitored_codes", lambda: [ETF])


def _store(tmp_path: Path, client: _FakeClient | None) -> MainlineDataStore:
    config = load_config(force=True)
    config.data.cache_path = str(tmp_path / "cache.db")
    made = MainlineDataStore(config=config,
                             remote=_FakeRemote(client))  # type: ignore[arg-type]
    made.ensure_schema()
    made._write("INSERT INTO ml_calendar(trade_date, is_open) VALUES(?,1)",
                [(day,) for day in (*HISTORY, *DAYS)])
    made._calendar = []
    return made


def _seed_etf(store: MainlineDataStore, day: str, *,
              codes: tuple[str, ...] = (ETF, OF),
              shares: float | None = None) -> None:
    """写一行 ETF 日线（`shares=None` = 份额还没发布）。同键重复写会覆盖。"""
    store._write(
        "INSERT INTO ml_etf(code, trade_date, name, close, pct_chg, volume,"
        " amount, shares) VALUES(?,?,?,?,?,?,?,?)"
        " ON CONFLICT(code, trade_date) DO UPDATE SET shares=excluded.shares",
        [(code, day, code, 1.0, 0.0, 1.0, 1.0, shares) for code in codes])


def _seed_history(store: MainlineDataStore, *, last_shares: float | None) -> None:
    """历史交易日份额齐全，最新日（20260918）按 `last_shares` 定。

    每天 ETF 与场外基金两行都要写：真实库里每天本来就是几百行混在一起，
    只写"想造缺口的那一行"会让 `MAX(trade_date)` 与缺口比例都不再真实。
    """
    for day in (*HISTORY, *DAYS[:3]):
        _seed_etf(store, day, shares=100.0)
    _seed_etf(store, DAYS[3], shares=last_shares)


# ==================================================================
# 一、正常情况下不该联网
# ==================================================================


def test_latest_day_without_shares_is_reported_unpublished(
        tmp_path: Path, now: _dt.datetime) -> None:
    """最新已收盘日份额还没发布 → `unpublished`，**只打一次**接口就收工。

    份额次日 8:30 才发布，所以"最新那天没有份额"是**正常状态**。判据不去猜
    它、也不反复等：实测一次 `fund_share` 拿回 0 行就如实记 `unpublished`。
    代价是一次调用（幂等 UPDATE，有份额就会补上），换来的是永远不误报、
    也永远不漏补。
    """
    client = _FakeClient(market={ETF: 999.0})
    store = _store(tmp_path, client)
    _seed_history(store, last_shares=None)

    report = guard.ensure_etf_shares(store)

    assert report.state == "unpublished"
    assert report.trade_date == DAYS[3]
    assert len(client.calls) == 1, "拿回 0 行就收工，绝不逐只重试"
    assert client.calls[0]["trade_date"] == DAYS[3]


def test_complete_latest_day_is_fresh(tmp_path: Path, now: _dt.datetime) -> None:
    """份额齐全 → fresh，**一次接口都不打**（这是绝大多数启动的情形）。"""
    client = _FakeClient(market={ETF: 999.0})
    store = _store(tmp_path, client)
    _seed_history(store, last_shares=130.0)

    report = guard.ensure_etf_shares(store)

    assert report.state == "fresh"
    assert report.watched_missing == 0
    assert client.calls == []


# ==================================================================
# 二、真缺口要补，且补的方式不能造脏数据
# ==================================================================


def test_offshore_fund_nulls_are_not_a_gap(
        tmp_path: Path, now: _dt.datetime) -> None:
    """接口只回了场外基金的份额 → 绝不按代码逐只重试。

    这是"接口这一轮根本没给 ETF 发布份额"的现场，实测能缺 1373 只。要是把这批
    代码逐只再问一遍，光启动自检就要打上千次请求，而结果一定是 0 行。
    判据用接口自己回的 `fund_type`：非 ETF 的跳过，一次都不重试。
    """
    # 只回 OF 一只：ETF 这一轮完全没份额 → 缺口比例 100%，必然触发补拉
    client = _FakeClient(market={OF: 456.0})
    store = _store(tmp_path, client)
    _seed_etf(store, DAYS[3], codes=(ETF, OF), shares=None)

    report = guard.ensure_etf_shares(store)

    assert report.state == "unpublished"
    # 最多两次：一次全市场快照 + 一次针对"接口明确标为 ETF 却仍缺"的那只。
    # 场外基金（138 只量级）绝不能被逐只重试 —— 那正是这里要守住的东西。
    assert len(client.calls) <= 2, client.calls
    assert client.calls[0].get("trade_date") == DAYS[3], "先走全市场口径"
    assert all(call.get("ts_code") != OF for call in client.calls), \
        "场外基金不该被逐只重试"
    assert report.watched_missing == 1, "监控的那只确实还缺着，如实记录"


def test_stale_reference_day_is_backfilled(
        tmp_path: Path, now: _dt.datetime) -> None:
    """本地最新日（20260918）缺份额，而**前一个交易日**也缺 → 两个都补上。

    20260918 缺是正常的（份额没发布），20260917 缺就是真缺口了 ——
    面板的 1/5/10/20 日变化率全靠它做基准。判据只看最新一天的行，
    所以补拉范围要盖住整段（`sync_etf_shares` 只管那一天，`_resync` 管更早的）。
    """
    client = _FakeClient(market={ETF: 123.0, OF: 456.0})
    store = _store(tmp_path, client)
    _seed_history(store, last_shares=None)
    _seed_etf(store, DAYS[2], shares=None, codes=(ETF, OF))   # 基准日也缺
    before = store._read("SELECT COUNT(*) AS n FROM ml_etf")[0]["n"]

    report = guard.ensure_etf_shares(store)

    assert report.state in ("filled", "unpublished")
    assert report.action.startswith(("补份额", "本地最新"))
    rows = {str(r["code"]): r["shares"] for r in store._read(
        "SELECT code, shares FROM ml_etf WHERE trade_date = ?", (DAYS[3],))}
    assert rows == {ETF: 123.0, OF: 456.0}
    after = store._read("SELECT COUNT(*) AS n FROM ml_etf")[0]["n"]
    assert after == before, "补份额绝不能插入新行（那正是当初出事的机制）"


def test_backfill_never_overwrites_existing_shares(
        tmp_path: Path, now: _dt.datetime) -> None:
    """已经有值的那一行必须原样不动 —— 这是"幂等且不可破坏"的全部含义。

    假接口在这几天一律回 999.0；真实库里若把已有份额覆盖掉，
    "份额变化率"就会凭空多出一根假柱，且没有任何报错。
    """
    client = _FakeClient(market={ETF: 999.0})
    store = _store(tmp_path, client)
    _seed_history(store, last_shares=None)
    _seed_etf(store, DAYS[2], codes=(OF,), shares=None)   # 把比例推过阈值

    guard.ensure_etf_shares(store)

    kept = [r["shares"] for r in store._read(
        "SELECT shares FROM ml_etf WHERE code=? AND trade_date IN (?,?,?)"
        " ORDER BY trade_date", (ETF, DAYS[0], DAYS[1], DAYS[2]))]
    assert kept == [100.0, 100.0, 100.0], "已有份额被覆盖了"
    assert client.calls, "缺口过阈值后才该开火"


# ==================================================================
# 三、降级路径：任何异常都不能拦住启动
# ==================================================================


def test_offline_without_tushare_is_reported_not_raised(
        tmp_path: Path, now: _dt.datetime) -> None:
    """没有 Tushare 源 → offline，不抛异常、不把作业打成 failed。"""
    store = _store(tmp_path, None)
    _seed_history(store, last_shares=None)

    report = guard.ensure_etf_shares(store)

    assert report.state == "offline"
    assert "Tushare" in (report.action + " ".join(report.notes))


def test_api_failure_degrades_to_offline(
        tmp_path: Path, now: _dt.datetime) -> None:
    """补份额时接口抛错 → offline（降级），绝不冒泡成异常。

    "数据源不可用"与"份额还没发布"必须分开报：前者要人去查，后者是正常时序。
    混成一种，运维会为一个正常的时序每天收到一条看起来像故障的记录。
    """
    store = _store(tmp_path, _FakeClient(raises=True))
    _seed_etf(store, DAYS[3], codes=(ETF, OF), shares=None)

    report = guard.ensure_etf_shares(store)

    assert report.state == "offline"


def test_missing_calendar_is_not_treated_as_gap(
        tmp_path: Path, now: _dt.datetime) -> None:
    """没有日历就不做新鲜度比对 —— 空串比任何日期都小，拿它比会把整库判成缺口。"""
    client = _FakeClient(market={ETF: 1.0})
    store = _store(tmp_path, client)
    _seed_history(store, last_shares=130.0)      # 数据其实是齐的
    # ⚠️ 必须带参数逐行删：`_write` 走的是 `executemany`，**参数为空的行集合
    # 会让整条语句一次都不执行**（Python 语义，不是本项目的行为），
    # 于是"清空日历"这一步会静默失效，测试变成什么都没验证。
    store._write("DELETE FROM ml_calendar WHERE trade_date = ?",
                 [(day,) for day in (*HISTORY, *DAYS)])
    store._calendar = []

    report = guard.ensure_etf_shares(store)

    assert report.state == "no_calendar"
    assert client.calls == []


def test_empty_table_reports_no_data(tmp_path: Path, now: _dt.datetime) -> None:
    report = guard.ensure_etf_shares(_store(tmp_path, _FakeClient()))
    assert report.state == "no_data"
    assert report.trade_date == ""


def test_none_store_is_skipped(now: _dt.datetime) -> None:
    assert guard.ensure_etf_shares(None).state == "skipped"


def test_report_serialises_for_logs(tmp_path: Path, now: _dt.datetime) -> None:
    """`to_dict()` 必须可 JSON 化 —— 它要进作业说明与接口。"""
    import json

    store = _store(tmp_path, _FakeClient(market={ETF: 123.0}))
    _seed_etf(store, DAYS[2], shares=None, codes=(ETF, OF))
    _seed_etf(store, DAYS[3], shares=None, codes=(ETF, OF))
    for day in (*HISTORY, *DAYS[:2]):
        _seed_etf(store, day, shares=100.0)          # 历史齐全 → 比例低于阈值

    payload = json.loads(json.dumps(guard.ensure_etf_shares(store).to_dict()))

    assert payload["state"] in ("filled", "unpublished")
    assert payload["trade_date"] == DAYS[3]
    assert isinstance(payload["changes"], dict)


# ==================================================================
# 四、口径函数本身
# ==================================================================


def test_recent_closed_days_skips_today_before_close(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """15:00 之前不把"今天"算作已收盘日（日线都还没落定）。"""

    class _BeforeClose(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return cls(2026, 9, 18, 10, 0)

    monkeypatch.setattr(guard, "datetime", _BeforeClose)
    store = _store(tmp_path, _FakeClient())

    assert guard.recent_closed_days(store)[0] == "20260917"
