"""主线挖掘：数据仓（`datastore`）的单元测试。

用**假仓库**（注入 `remote`）而不是真的连 15 GiB 的 `warehouse.db`：
单测里绝不能依赖本机数据，否则 CI 上必然红。

重点钉住五件事：

1. **单位口径**：本地仓的金额字段**已经是「元」**，读的时候一个乘数都不能加。
   多乘一次会让金额放大 1000~10000 倍 —— 而打分用的是横截面分位，
   整体放大后**排序不变**，界面上看不出任何异常（这一点吃过一次亏）。
2. **防前视**：财务必须按 `ann_date <= trade_date` 截断。年报的 `end_date`
   是 12-31，但 `ann_date` 可能到次年 3 月；只看 `end_date` 就是用了未来信息。
3. **同步台账**：失败与"真的没数据"要能区分开。
4. **增量判断以交易日历为准**，不是自然日（否则每个周末都会发一次注定失败的请求）。
5. **北向只认 A 股**：`hk_hold` 在 2024-08 之后的非季末日会返回**港股**，
   不过滤就会把港股写进 A 股北向表（静默污染）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.mainline.config import load_config
from src.mainline.datastore import MainlineDataStore
from src.mainline.models import SeatRow


class _FakeWarehouse:
    """假行情仓：只回答调用方问的那几条 SQL，并记录收到的参数。"""

    def __init__(self, *, amount_yuan: float = 1.0e8,
                 circ_mv_yuan: float = 5.0e9,
                 net_yuan: float = 2.0e7) -> None:
        self.amount_yuan = amount_yuan
        self.circ_mv_yuan = circ_mv_yuan
        self.net_yuan = net_yuan
        self.calls: list[tuple[str, tuple]] = []

    def available(self) -> bool:
        return True

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        self.calls.append((" ".join(sql.split()), tuple(params)))
        if "quant_moneyflow" in sql and "GROUP BY m.trade_date" in sql:
            return [{"trade_date": "20260917", "net": self.net_yuan,
                     "amount": self.amount_yuan, "circ": self.circ_mv_yuan}]
        if "quant_fina_indicator" in sql:
            return [{"code": "600519", "roe_yoy": 12.0, "netprofit_yoy": 8.0,
                     "or_yoy": 6.0, "end_date": "20260630",
                     "ann_date": "20260815"}]
        if "quant_daily_basic" in sql:
            return [{"code": "600519", "circ_mv": self.circ_mv_yuan}]
        if "quant_daily" in sql and "quant_moneyflow" in sql:
            return [{"code": "600519", "trade_date": "20260917", "close": 10.0,
                     "amount": self.amount_yuan, "net": self.net_yuan}]
        return []

    def stock_names(self, codes) -> dict[str, str]:
        return {str(code): f"股票{code}" for code in codes}


class _FakeTushare:
    """假 Tushare 源：日历固定，其余按需返回。"""

    def __init__(self) -> None:
        self.holdings_calls: list[str] = []
        self.margin_calls: list[str] = []

    def trade_dates(self, start: str, end: str) -> list[str]:
        return [day for day in ("20260915", "20260916", "20260917")
                if start <= day <= end]

    def margin_daily(self, trade_date: str):
        self.margin_calls.append(trade_date)
        if trade_date != "20260917":
            return []
        from src.mainline.models import MarginRow
        return [MarginRow(code="600519", date=trade_date, rzye=1.0e9,
                          rqye=0.0, net_buy=1.0e7)]

    def northbound_holdings(self, trade_date: str):
        self.holdings_calls.append(trade_date)
        return []

    def holder_number(self, codes):
        return []

    def seat_rows(self, trade_date: str):
        return []


class _FakeCatalog:
    def all_boards(self, **kwargs):
        from src.mainline.models import BoardInfo, BoardKind
        return [BoardInfo(code="801780.SI", name="银行", kind=BoardKind.SW_L1)]

    def members_of(self, board):
        return ["600519", "601398"]


class _FakeRemote:
    def __init__(self) -> None:
        self.warehouse = _FakeWarehouse()
        self.tushare = _FakeTushare()
        self.catalog = _FakeCatalog()


@pytest.fixture
def store(tmp_path: Path) -> MainlineDataStore:
    config = load_config(force=True)
    config.data.cache_path = str(tmp_path / "cache.db")
    config.universe.include_concept = False
    # ⚠️ 显式走"自建名录"路径：默认的 `source="crowding"` 会把 `sync_boards()`
    # 委托给 `import_crowding_pool()`，那要读**真实的拥挤度库**
    # （`data/moss_finagent.db`）—— 单测不该依赖本机数据，CI 上必然红。
    # 本组测试关心的是假目录 + 假仓库下的**单位换算**，与板块从哪来无关。
    config.universe.source = "catalog"
    made = MainlineDataStore(config=config, remote=_FakeRemote())
    made.ensure_schema()
    return made


# ==================================================================
# 一、建表与台账
# ==================================================================


def test_schema_creates_all_tables(store: MainlineDataStore) -> None:
    stats = store.stats()
    assert set(stats) >= {"ml_board", "ml_board_bar", "ml_board_flow",
                          "ml_margin", "ml_northbound", "ml_seat", "ml_macro",
                          "ml_future", "ml_calendar"}
    assert all(count == 0 for count in stats.values())


def test_sync_calendar_is_idempotent(store: MainlineDataStore) -> None:
    first = store.sync_calendar("20260901", "20260930")
    second = store.sync_calendar("20260901", "20260930")
    assert first.rows == second.rows == 3
    assert store.calendar("20260901", "20260930") == [
        "20260915", "20260916", "20260917"]


def test_sync_status_records_last_run(store: MainlineDataStore) -> None:
    store.sync_calendar("20260901", "20260930")
    status = store.sync_status()
    assert any(row["dataset"] == "calendar" for row in status)


# ==================================================================
# 一之二、ETF 份额补拉（`sync_etf_shares` / `etf_share_coverage`）
# ==================================================================
#
# 背景（2026-09-22 实测事故）：份额次日 8:30 才发布，而日线当晚就有。于是
# "份额发布前跑一次全市场同步"会给最新交易日写下一批 `shares = NULL` 的行，
# 而面板以 `MAX(trade_date)` 为锚点只读最后一行 —— 那几只 ETF 的份额、
# 1/5/10/20 日变化率、对应指数分位**一起变空**。
#
# 这组测试钉的是修补方式本身，而不是"补上了没有"：
# 只 UPDATE 已有的 NULL 行、**绝不插入新行**、已有份额绝不被覆盖。


class _Frame:
    """最小 DataFrame 替身：被测代码只用 `.to_dict("records")`。"""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def to_dict(self, orient: str = "records") -> list[dict]:  # noqa: ARG002
        return list(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


class _ShareClient:
    """假 Tushare 客户端：按 `trade_date` 或 `ts_code` 两种入参返回份额。

    `market` 里的值可以是 `None` —— 表示"接口这一轮提到了这只代码，但没给份额"
    （被行数上限截掉、或当天未发布）。这正是需要逐只重试的那一类。
    """

    def __init__(self, *, market: dict[str, float | None],
                 by_code: dict[str, float] | None = None,
                 with_type: bool = True) -> None:
        self.market = dict(market)
        self.by_code = dict(by_code or {})
        self.with_type = with_type
        self.calls: list[dict] = []

    def call(self, name: str, **kwargs):
        self.calls.append({"name": name, **kwargs})
        if name != "fund_share":
            return _Frame([])
        if kwargs.get("ts_code"):
            code = str(kwargs["ts_code"])
            value = self.by_code.get(code)
            return _Frame([{"ts_code": code, "trade_date": kwargs.get("end_date"),
                            "fd_share": value}] if value is not None else [])
        day = str(kwargs.get("trade_date") or "")
        rows = []
        for code, value in self.market.items():
            row = {"ts_code": code, "trade_date": day, "fd_share": value}
            if self.with_type:
                row["fund_type"] = "OF" if code.endswith(".OF") else "ETF"
            rows.append(row)
        return _Frame(rows)


class _ShareTushare:
    def __init__(self, client: _ShareClient) -> None:
        self.client = client


class _ShareRemote:
    def __init__(self, client: _ShareClient | None) -> None:
        self.tushare = None if client is None else _ShareTushare(client)


def _share_store(tmp_path: Path, client: _ShareClient | None
                 ) -> MainlineDataStore:
    config = load_config(force=True)
    config.data.cache_path = str(tmp_path / "shares.db")
    made = MainlineDataStore(config=config,
                             remote=_ShareRemote(client))  # type: ignore[arg-type]
    made.ensure_schema()
    return made


def _etf_row(code: str, day: str, shares: float | None) -> tuple:
    return (code, day, code, 1.0, 0.0, 1.0, 1.0, shares)


def test_sync_etf_shares_only_updates_null_rows(tmp_path: Path) -> None:
    """只补 `shares IS NULL` 的那一行；已有份额原样不动；不新增任何行。"""
    client = _ShareClient(market={"510300.SH": 111.0, "510310.SH": 222.0})
    store = _share_store(tmp_path, client)
    store._write(  # noqa: SLF001
        "INSERT INTO ml_etf(code, trade_date, name, close, pct_chg, volume,"
        " amount, shares) VALUES(?,?,?,?,?,?,?,?)",
        [_etf_row("510300.SH", "20260921", None),      # 该补
         _etf_row("510310.SH", "20260921", 999.0)])    # 已有值，不许动
    before = store._read("SELECT COUNT(*) AS n FROM ml_etf")[0]["n"]  # noqa: SLF001

    result = store.sync_etf_shares(trade_date="20260921")

    assert result.status == "ok"
    # 注意：`rows` 是 `executemany` 的**送执行行数**（接口回了 2 只），
    # 不等于"真正改了 2 行" —— 真正改了几行只有库里的值能证明，
    # 所以下面直接核对值，而不是拿这个计数当断言。
    assert result.rows == 2
    got = {str(row["code"]): row["shares"] for row in store._read(  # noqa: SLF001
        "SELECT code, shares FROM ml_etf ORDER BY code")}
    assert got == {"510300.SH": 111.0, "510310.SH": 999.0}, \
        "已有份额被覆盖，或该补的那一行没补上"
    after = store._read("SELECT COUNT(*) AS n FROM ml_etf")[0]["n"]  # noqa: SLF001
    assert after == before, "补份额绝不能插入新行（那正是当初出事的机制）"


def test_sync_etf_shares_does_not_retry_offshore_funds(
        tmp_path: Path) -> None:
    """接口明确标为 `OF` 的代码不逐只重试 —— 它们的份额本来就还没发布。

    实测未发布那一轮能缺 1373 只：逐只重问在 5000 积分档的频次限制下足以把
    启动自检拖垮，而结果一定是 0 行。
    """
    client = _ShareClient(market={"158008.OF": 5.0}, with_type=True)
    store = _share_store(tmp_path, client)
    store._write(  # noqa: SLF001
        "INSERT INTO ml_etf(code, trade_date, name, close, pct_chg, volume,"
        " amount, shares) VALUES(?,?,?,?,?,?,?,?)",
        [_etf_row("158008.OF", "20260921", None),
         _etf_row("510300.SH", "20260921", None)])   # 接口这一轮没提它

    store.sync_etf_shares(trade_date="20260921")

    per_code = [call for call in client.calls if call.get("ts_code")]
    assert per_code == [], "场外基金 / 接口没提过的代码都不该被逐只重试"


def test_sync_etf_shares_retries_etf_seen_but_without_value(
        tmp_path: Path) -> None:
    """接口标为 ETF、这一轮却没给份额 → 必须逐只重试。

    这正是"被 `fund_share` 行数上限截掉"或"当天只发布了一部分"的形状：
    没有这次重试，那些份额就永远补不上，而它们在接口里其实是查得到的。
    """
    client = _ShareClient(market={"510300.SH": 111.0, "510310.SH": None},
                          by_code={"510310.SH": 222.0}, with_type=True)
    store = _share_store(tmp_path, client)
    store._write(  # noqa: SLF001
        "INSERT INTO ml_etf(code, trade_date, name, close, pct_chg, volume,"
        " amount, shares) VALUES(?,?,?,?,?,?,?,?)",
        [_etf_row("510300.SH", "20260921", None),
         _etf_row("510310.SH", "20260921", None)])

    store.sync_etf_shares(trade_date="20260921")

    got = {str(row["code"]): row["shares"] for row in store._read(  # noqa: SLF001
        "SELECT code, shares FROM ml_etf ORDER BY code")}
    assert got == {"510300.SH": 111.0, "510310.SH": 222.0}
    assert [call.get("ts_code") for call in client.calls
            if call.get("ts_code")] == ["510310.SH"], "只该重试缺的那一只"


def test_sync_etf_shares_without_tushare_is_skipped(tmp_path: Path) -> None:
    """无数据源 → `skipped`（不是 failed）：这是配置状态，不是故障。"""
    store = _share_store(tmp_path, None)
    result = store.sync_etf_shares(trade_date="20260921")
    assert result.status == "skipped"


def test_etf_share_coverage_reports_share_ratio_key(tmp_path: Path) -> None:
    """覆盖度的键必须叫 `share_ratio`（**已覆盖**比例）。

    早先叫 `ratio`，调用方按直觉当成"缺口比例"用 —— 分母正确、分子反了，
    判据恒为"没缺口"，自检永远不补数且不报任何错。键名歧义就是这起 bug 的根因。
    """
    store = _share_store(tmp_path, _ShareClient(market={}))
    store._write(  # noqa: SLF001
        "INSERT INTO ml_etf(code, trade_date, name, close, pct_chg, volume,"
        " amount, shares) VALUES(?,?,?,?,?,?,?,?)",
        [_etf_row("510300.SH", "20260921", 100.0),
         _etf_row("510310.SH", "20260921", None)])

    out = store.etf_share_coverage()

    assert out["trade_date"] == "20260921"
    assert out["rows"] == 2 and out["with_share"] == 1
    assert out["missing"] == 1
    assert out["share_ratio"] == pytest.approx(0.5)
    assert "ratio" not in out
    assert out["codes"] == ["510310.SH"]

    only = store.etf_share_coverage(codes=["510300.SH"])
    assert only["missing"] == 0 and only["share_ratio"] == pytest.approx(1.0)


def test_etf_share_coverage_empty_table_is_safe(tmp_path: Path) -> None:
    out = _share_store(tmp_path, _ShareClient(market={})).etf_share_coverage()
    assert out["trade_date"] == "" and out["rows"] == 0
    assert out["share_ratio"] == 0.0 and out["codes"] == []


def test_sync_catchup_reports_its_own_dataset(tmp_path: Path) -> None:
    """`sync_catchup` 要落在台账的 `etf_catchup` 上，别把 `etf` 那条记录盖掉。"""
    store = _share_store(tmp_path, _ShareClient(market={}))
    result = store.sync_catchup(days=5)
    assert result.dataset == "etf_catchup"


# ==================================================================
# 二、单位口径（**最关键**的一组测试）
# ==================================================================


def test_board_aggregation_does_not_rescale_warehouse_units(
        store: MainlineDataStore) -> None:
    """本地仓已是「元」，聚合结果必须原样透传 —— 不能再乘 1e3 / 1e4。"""
    store.sync_boards()
    store.sync_members()
    result = store.sync_board_flows(start="20260917", end="20260917")
    assert result.rows >= 1
    flows = store.board_flows(["801780.SI"], start="20260917", end="20260917")
    flow = flows["801780.SI"]
    assert flow.points == [("20260917", 2.0e7)]
    # 净占比 = 净流入 / 成交额，两者同口径（元 / 元）→ 0.2
    rows = store._read(  # noqa: SLF001 直接核对落库值
        "SELECT net_amount, amount, circ_mv FROM ml_board_flow"
        " WHERE board_code = '801780.SI'")
    assert rows[0]["net_amount"] == pytest.approx(2.0e7)
    assert rows[0]["amount"] == pytest.approx(1.0e8)
    assert rows[0]["circ_mv"] == pytest.approx(5.0e9)


def test_member_stats_keeps_circ_mv_in_yuan(store: MainlineDataStore) -> None:
    store._read("SELECT 1")  # 触发建表  # noqa: B018
    stats = store.member_stats(["600519"], trade_date="20260917")
    assert stats["600519"]["circ_mv"] == pytest.approx(5.0e9)


# ==================================================================
# 三、防前视
# ==================================================================


def test_member_stats_filters_by_announce_date(store: MainlineDataStore) -> None:
    """公告日 20260815 的报告，在 20260701 那天**必须不可见**。"""
    early = store.member_stats(["600519"], trade_date="20260701")
    # 假仓库的 SQL 会返回同一行，但函数内的二次过滤必须把它挡掉
    assert "600519" not in early or early["600519"].get("roe_yoy") is None
    later = store.member_stats(["600519"], trade_date="20260917")
    assert later["600519"]["roe_yoy"] == pytest.approx(12.0)


def test_member_stats_sql_uses_announce_date_clause(
        store: MainlineDataStore) -> None:
    """SQL 里必须带 `ann_date` 截断条件（否则前视偏差在前端毫无痕迹）。"""
    store.member_stats(["600519"], trade_date="20260917")
    warehouse = store.remote.warehouse
    sqls = [sql for sql, _ in warehouse.calls if "quant_fina_indicator" in sql]
    assert sqls, "必须查过财务表"
    assert any("ann_date" in sql for sql in sqls)


# ==================================================================
# 四、增量同步以交易日历为准
# ==================================================================


def test_missing_dates_uses_calendar_not_natural_days(
        store: MainlineDataStore) -> None:
    store.sync_calendar("20260901", "20260930")
    missing = store._missing_dates("ml_margin", "20260901", "20260930")  # noqa: SLF001
    assert missing == ["20260915", "20260916", "20260917"]
    assert "20260913" not in missing      # 周日不该出现在待同步列表里


def test_repeated_sync_skips_already_synced_days(
        store: MainlineDataStore) -> None:
    """已同步过的交易日不再重复请求；**拿不到数据的日期会被重试**（这是刻意的：
    "上游还没发布"与"这天没有两融数据"在接口层面无法区分，只能下次再试）。"""
    store.sync_calendar("20260901", "20260930")
    store.sync_margin(start="20260917", end="20260917")
    assert store.remote.tushare.margin_calls == ["20260917"]
    store.sync_margin(start="20260917", end="20260917")
    assert store.remote.tushare.margin_calls == ["20260917"]
    # 空数据的那两天会被再问一次（见 docstring）
    store.sync_margin(start="20260915", end="20260915")
    store.sync_margin(start="20260915", end="20260915")
    assert store.remote.tushare.margin_calls.count("20260915") == 2


# ==================================================================
# 五、日历自愈（上游 `trade_cal` 整段缺月时）
# ==================================================================


def _seed_witnesses(store: MainlineDataStore, table: str,
                    days: list[str]) -> None:
    """往交叉表里塞日期（只关心 `trade_date` 是否存在）。"""
    if table == "ml_board_bar":
        store._write(  # noqa: SLF001
            "INSERT INTO ml_board_bar(board_code, trade_date, close)"
            " VALUES('885001.TI', ?, 1.0)", [(day,) for day in days])
        return
    store._write(  # noqa: SLF001
        f"INSERT INTO {table}(code, trade_date, close) VALUES('000001', ?, 1.0)",
        [(day,) for day in days])


def _seed_calendar(store: MainlineDataStore, days: list[str]) -> None:
    """直接写日历。

    `_FakeTushare.trade_dates()` 只会返回 20260915~20260917 三天，
    所以这里不能走 `sync_calendar`，否则"日历里本来有什么"就不可控了。
    """
    store._write(  # noqa: SLF001
        "INSERT INTO ml_calendar(trade_date, is_open) VALUES(?, 1)",
        [(day,) for day in days])
    store._calendar = []  # noqa: SLF001  失效缓存


def test_repair_calendar_fills_month_missing_from_upstream(
        store: MainlineDataStore) -> None:
    """上游日历整段缺月 → 多表交叉确认的日期要补进来。

    这是本轮真实故障的回归测试：`ml_calendar` 整段缺 20251201~20251231，
    后果是**整个月不被打分**（`_trading_days` 只读日历），
    最新回测窗口凭空少 23 天，而全链路一声不响。
    """
    _seed_calendar(store, ["20251128", "20260105"])
    for table in ("ml_index", "ml_future", "ml_etf"):
        _seed_witnesses(store, table, ["20251201", "20251202"])
    # 只有 `ml_board_bar` 有数据（休市日脏数据）：它**不参与判定**，
    # 所以既不进日历，也不出现在 `missing` 里 —— 而这点很重要，
    # 把它当证人会在休市日上写出一整行 NaN，反而制造更毒的滚动窗口空洞。
    _seed_witnesses(store, "ml_board_bar", ["20260102"])
    result = store.repair_calendar()
    assert result.rows == 2
    # 洞在日历**自身跨度之内**（20251128 ~ 20260105）才会被补，
    # 这正是保守默认的意图：不去把区间外的历史全拉进来。
    assert store.calendar("20251101", "20260110") == [
        "20251128", "20251201", "20251202", "20260105"]
    assert "20260102" not in store.calendar("20260101", "20260103")
    assert result.missing == []


def test_repair_calendar_reports_single_witness_days(
        store: MainlineDataStore) -> None:
    """只有**一张**判定表有数据的日期列为可疑、但不补。"""
    _seed_calendar(store, ["20251128", "20260105"])
    _seed_witnesses(store, "ml_index", ["20251210"])
    result = store.repair_calendar()
    assert result.rows == 0
    assert result.missing == ["20251210"]
    assert store.calendar("20251201", "20251231") == []


def test_repair_calendar_is_idempotent_and_bounded(
        store: MainlineDataStore) -> None:
    """重复执行为空操作；默认区间是**日历自身跨度**，不把 2018 年拉进来。"""
    _seed_calendar(store, ["20251128", "20260105"])
    for table in ("ml_index", "ml_future", "ml_etf"):
        _seed_witnesses(store, table, ["20180102", "20251201"])
    first = store.repair_calendar()
    assert first.rows == 1                       # 只补区间内的 20251201
    assert store.calendar("20180101", "20180131") == []   # 2018 年没被拉进来
    assert store.repair_calendar().rows == 0


def test_repair_calendar_dry_run_writes_nothing(
        store: MainlineDataStore) -> None:
    _seed_calendar(store, ["20251128"])
    for table in ("ml_index", "ml_future"):
        _seed_witnesses(store, table, ["20251201"])
    result = store.repair_calendar(apply=False)
    assert result.rows == 0
    assert store.calendar() == ["20251128"]


def test_northbound_empty_days_are_not_failures(
        store: MainlineDataStore) -> None:
    """上游返回空（2024-08 后仅季末披露）记 partial 并说明，不记 failed。"""
    store.sync_calendar("20260901", "20260930")
    result = store.sync_northbound(start="20260901", end="20260930")
    assert result.status in ("ok", "partial")
    assert "季末" in result.message or result.rows > 0


# ==================================================================
# 五、读取接口
# ==================================================================


def test_board_bars_roundtrip_includes_pre_close(store: MainlineDataStore) -> None:
    """`pre_close` 必须能回读 —— 隔夜跳空维度完全依赖它。"""
    store._write(  # noqa: SLF001 直接造一行，避免依赖远端
        "INSERT INTO ml_board_bar(board_code, trade_date, open, high, low,"
        " close, pre_close, volume, amount, pct_change, source)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [("801780.SI", "20260917", 10.0, 11.0, 9.5, 10.5, 10.0, 1000.0,
          1.0e8, 5.0, "test")])
    series = store.board_bars(["801780.SI"], start="20260901", end="20260930")
    bar = series["801780.SI"].bars[0]
    assert bar.pre_close == pytest.approx(10.0)
    assert bar.date == "20260917"


def test_seats_reader_filters_by_date_range(store: MainlineDataStore) -> None:
    store._write(  # noqa: SLF001
        "INSERT INTO ml_seat(trade_date, code, exalter, side, buy, sell,"
        " net_buy, reason) VALUES(?,?,?,?,?,?,?,?)",
        [("20260917", "600519", "机构专用", "0", 1.0e7, 0.0, 1.0e7, "测试"),
         ("20260101", "600519", "某营业部", "0", 1.0e7, 0.0, 1.0e7, "测试")])
    rows = store.seats(start="20260901", end="20260930")
    assert len(rows) == 1
    assert isinstance(rows[0], SeatRow)
    assert rows[0].is_institution is True


def test_macro_reader_returns_sorted_series(store: MainlineDataStore) -> None:
    store._write(  # noqa: SLF001
        "INSERT INTO ml_macro(factor, period, value, updated_at)"
        " VALUES(?,?,?,?)",
        [("pmi", "202607", 49.2, ""), ("pmi", "202608", 49.8, "")])
    series = store.macro("pmi")
    assert series == [("202607", 49.2), ("202608", 49.8)]
    assert store.macro_latest()["pmi"] == ("202608", 49.8)


def test_read_failure_degrades_to_empty(tmp_path: Path) -> None:
    """库损坏时读接口返回空而不是抛错（面板不该 500）。"""
    config = load_config(force=True)
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a sqlite file at all")
    config.data.cache_path = str(broken)
    made = MainlineDataStore(config=config, remote=_FakeRemote())
    assert made.board_bars(["X"], start="20260101", end="20260102") == {}


def test_admission_separates_channel_from_score_source() -> None:
    """入池通道必须与「业务分来源」分开记 —— 这是用户 2026-09-23 报障的根因。

    `ml_member_pure` 里 `source='corr'` **可以带一个业务分**（判过但不及格），
    所以只看 `source` 会把「LLM 判过且只给 65 分」说成「LLM 未判过」，
    展示与数据相反。四种情形逐个钉住。
    """
    from src.mainline.datastore import _admission  # noqa: SLF001 纯函数

    # 相关性直通、LLM 从未判过该题材 → 中性（无证据，不罚）
    assert _admission("corr", None) == "corr"
    # ⚠️ 关键情形：判过、给了 65 分（< 70）、却仍按相关性入池
    assert _admission("corr", 65.0) == "corr_business_failed"
    # 判过且达标 → 正常业务入池
    assert _admission("llm", 85.0) == "business"
    # 边界：恰好 70 分算达标（与 BUSINESS_PASS 的 `>=` 口径一致）
    assert _admission("corr", 70.0) == "business"
    assert _admission("corr", 69.9) == "corr_business_failed"
    # 回查 ml_stock_theme 补出来的分同样按门槛判定
    assert _admission("llm_theme", 95.0) == "business"
    # 没有任何业务信息 → 空（展示字段缺失，前端显示"无业务判定"）
    assert _admission("", None) == ""


# ==================================================================
# 六、V1.0 结构升级（`storage` 里的评分/告警表，不是 `ml_*` 数据表）
# ==================================================================


def test_v1_score_table_is_rebuilt(tmp_path: Path) -> None:
    """旧版评分表（five_dim 列）必须被识别并重建，否则会一直撞 no such column。"""
    from src.mainline.storage import MainlineRepository

    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE mainline_score (trade_date TEXT, board_code TEXT,"
        " five_dim REAL, PRIMARY KEY(trade_date, board_code))")
    connection.commit()
    connection.close()
    repo = MainlineRepository(path=str(path))
    repo._ensure_schema_sync()  # noqa: SLF001 直接测建表内核
    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute(
        "PRAGMA table_info(mainline_score)")}
    connection.close()
    assert "accumulation" in columns
    assert "five_dim" not in columns
    assert "candidate" in columns and "selected" in columns


def test_v2_alerts_allow_duplicate_board_across_days(tmp_path: Path) -> None:
    """告警表主键是 `alert_id`（日期+板块+等级）——同板块跨日必须能各存一行。"""
    from src.mainline.models import AlertSignal, SignalLevel
    from src.mainline.storage import MainlineRepository

    repo = MainlineRepository(path=str(tmp_path / "alerts.db"))
    repo._ensure_schema_sync()  # noqa: SLF001

    async def _run() -> int:
        return await repo.save_alerts([
            AlertSignal(board_code="801780.SI", board_name="银行",
                        trade_date="20260620", level=SignalLevel.STRONG),
            AlertSignal(board_code="801780.SI", board_name="银行",
                        trade_date="20260628", level=SignalLevel.STRONG),
        ])

    import asyncio

    assert asyncio.run(_run()) == 2
    rows = asyncio.run(repo.load_alerts(level="strong", limit=10))
    assert len(rows) == 2
