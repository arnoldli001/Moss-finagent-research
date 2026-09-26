"""板块概念拥挤度：计算 / 存储 / 刷新 的单元测试。

重点钉住四个"看起来对、实际会错"的地方：

1. **水位分母必须是 expanding**：第 i 天只能看到第 i 天为止的最大值。
   若用全样本 `max()`，历史水位会被后来的高点压低 —— 回看曲线时
   "当时到底警没警"就失真了（而且这是典型的前视函数泄漏）。
2. **MA5 不足窗口的天数**：`min_periods=1` 让开头也有值，但水位另有
   `min_bars` 把关 —— 否则次新板块用 2 天数据算出水位 100% 直接误告警。
3. **分母为 0 / 缺失 → water_level = NULL**：把 NULL 当 0 会漏告警、
   当 100 会误告警，都是错的。
4. **增量刷新的水位线只在成功时推进**：中途失败还推进会让那段日期成为
   永久空洞（下次从错误日期开始，谁也补不回来）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.sector_crowding import db, refresh
from src.sector_crowding.config import (
    SectorCrowdingConfig,
    is_concept_board,
    load_config,
)
from src.sector_crowding.refresh import RefreshTask


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = db.get_db_connection(path=tmp_path / "crowding.db")
    db.init_tables(connection)
    yield connection
    connection.close()


@pytest.fixture
def config(tmp_path: Path) -> SectorCrowdingConfig:
    """指向临时库的配置（不碰真实库）。"""
    base = load_config()
    base.database.path = str(tmp_path / "crowding.db")
    return base


def _rows(count: int, *, share: float = 0.01, market: float = 1e12,
          ramp: bool = False) -> list[dict]:
    """造 `count` 天数据；`ramp=True` 时占比逐日上升。"""
    out = []
    for index in range(count):
        ratio = share * (1 + index / max(1, count)) if ramp else share
        out.append({
            "trade_date": f"2026{1 + index // 28:02d}{1 + index % 28:02d}",
            "sector_amount": market * ratio,
            "market_amount": market,
        })
    return out


# ======================================================================
# compute_series：口径正确性
# ======================================================================

def test_raw_crowding_is_ratio() -> None:
    rows = refresh.compute_series(_rows(3, share=0.02), min_bars=1)
    assert all(item["raw_crowding"] == pytest.approx(0.02) for item in rows)


def test_ma_is_rolling_mean_of_configured_window() -> None:
    """MA5 是**滚动**均值：前 4 天是不足窗口的均值（min_periods=1），第 5 天起才是 5 日均值。"""
    rows = refresh.compute_series(_rows(6, ramp=True), ma_window=5, min_bars=1)
    raws = [item["raw_crowding"] for item in rows]
    for index in range(len(raws)):
        window = raws[max(0, index - 4): index + 1]
        assert rows[index]["ma5_crowding"] == pytest.approx(
            sum(window) / len(window)), f"第 {index} 天"


def test_water_level_normalises_to_own_history_max() -> None:
    """水位 = 当前 MA / 自身历史 MA 最大值。占比恒定时 100% 封顶。"""
    rows = refresh.compute_series(_rows(30, share=0.01), min_bars=1)
    for item in rows:
        assert item["water_level"] == pytest.approx(1.0)


def test_water_level_denominator_is_expanding_not_global() -> None:
    """**关键**：分母只能看到"当天为止"的最大值（无未来函数）。

    占比从 1% 一路升到 2%：前 15 天的水位必须都 ≤ 1.0（因为当时的历史最高
    就是当时的自己），第 30 天才会接近 1.0。若误用全样本 max，
    前面那段会被后来的高点压到 ~0.5 —— 测试就是抓这个。
    """
    rows = refresh.compute_series(_rows(30, share=0.01, ramp=True), min_bars=1)
    first_half = [item["water_level"] for item in rows[:15]]
    assert all(value is not None and value <= 1.0 + 1e-9 for value in first_half)
    # 前半段接近 1.0（每天都创出新高 → 当天就是历史最高）
    assert min(value for value in first_half if value is not None) > 0.9
    # 后半段仍在 1.0 附近（单调上升序列）
    assert rows[-1]["water_level"] == pytest.approx(1.0, abs=1e-6)


def test_water_level_null_when_insufficient_bars() -> None:
    """样本不足 → NULL（前端显示"数据不足"），**不是** 0 或 100。"""
    rows = refresh.compute_series(_rows(10, share=0.01), min_bars=60)
    assert all(item["water_level"] is None for item in rows)
    rows = refresh.compute_series(_rows(70, share=0.01), min_bars=60)
    assert rows[58]["water_level"] is None       # 第 59 根：只有 59 个有效值
    assert rows[59]["water_level"] is not None   # 第 60 根起才够 min_bars


def test_water_level_none_not_nan() -> None:
    """**关键**：输出必须是 `None`（JSON null），不能是 `nan`。

    pandas 的 float 列会把 None 还原成 nan —— 接口返回 nan 时前端的
    "数据不足"判断（`value === null`）会失效，图上会画出一个 NaN 点。
    """
    rows = refresh.compute_series(_rows(20, share=0.01), min_bars=60)
    for item in rows:
        for key in ("raw_crowding", "ma5_crowding", "water_level"):
            value = item[key]
            assert not (isinstance(value, float) and value != value), \
                f"{key} 是 nan，应为 None"
    assert all(item["water_level"] is None for item in rows)


def test_water_level_null_when_market_amount_missing_or_zero() -> None:
    rows = [{"trade_date": "20260101", "sector_amount": 100.0, "market_amount": 0.0},
            {"trade_date": "20260102", "sector_amount": 100.0, "market_amount": None},
            {"trade_date": "20260103", "sector_amount": 100.0, "market_amount": 1e6}]
    out = refresh.compute_series(rows, min_bars=1)
    assert out[0]["water_level"] is None
    assert out[1]["water_level"] is None
    assert out[2]["water_level"] is not None


def test_compute_series_empty_and_single() -> None:
    assert refresh.compute_series([]) == []
    single = refresh.compute_series(_rows(1, share=0.02), min_bars=1)
    assert len(single) == 1
    assert single[0]["water_level"] == pytest.approx(1.0)


def test_compute_series_sorts_by_date() -> None:
    rows = list(reversed(_rows(5, share=0.01)))
    out = refresh.compute_series(rows, min_bars=1)
    assert [item["trade_date"] for item in out] == sorted(
        item["trade_date"] for item in rows)


# ======================================================================
# 概念判定
# ======================================================================

@pytest.mark.parametrize(("code", "name", "expected"), [
    ("885800.TI", "半导体", True),
    ("885801.TI", "光刻胶", True),
    ("700001.TI", "同花顺全A(加权)", False),
    ("700002.TI", "同花顺全A(除金融、石油石化)", False),
    ("882001.TI", "安徽", False),
    ("883300.TI", "沪深300样本股", False),
    ("883301.TI", "上证50样本股", False),
    ("700051R.TI", "同花顺金仓30全收益", False),
    ("864001.TI", "昨日涨幅超过10%", False),
    # 行业Ⅲ（dim_concept 独有、无 type）：靠 861 前缀 + 名称兜住
    ("861003.TI", "化学制品", False),
    ("861006.TI", "金属与采矿", False),
    ("700668.TI", "木材加工和木、竹、藤、棕、草制品业指数", False),
])
def test_is_concept_board(code, name, expected) -> None:
    assert is_concept_board(code, name) is expected


@pytest.mark.parametrize(("board_type", "name", "expected"), [
    # **同花顺 type 是权威分类**，优先于名称规则：
    # 实测 type='I' 的 777 个行业板块里，有 701 个靠名称规则会被误判成概念
    ("I", "半导体产品与设备Ⅲ", False),
    ("I", "化学制品", False),
    ("R", "临沂市指数", False),
    ("S", "昨日涨幅超过10%", False),
    ("BB", "同花顺全A(加权)", False),
    ("TH", "同花顺金仓30", False),
    ("ST", "同花顺小盘", False),
])
def test_is_concept_board_uses_authoritative_type(board_type, name, expected) -> None:
    """有 `board_type` 时按它判 —— 名称规则会漏掉「化学制品」这类不带"业指数"的行业。"""
    assert is_concept_board("861003.TI", name,
                            board_type=board_type) is expected


@pytest.mark.parametrize(("code", "name"), [
    # ⚠️ **N 不能一刀切成非概念**：实测「半导体」= 885800.TI 的 type 就是 N ——
    # 这个 type 是同花顺的"指数族"，里面既有沪深300样本股（883xxx，非概念）
    # 也有半导体/光刻胶（885xxx，是概念）。所以 N 交回名称/代码规则判。
    ("885800.TI", "半导体"),
    ("885801.TI", "光刻胶"),
    ("885802.TI", "低空经济"),
    ("886001.TI", "PCB概念"),
])
def test_type_n_is_not_blanket_excluded(code, name) -> None:
    assert is_concept_board(code, name, board_type="N") is True


def test_index_sample_boards_excluded_by_name_even_with_type_n() -> None:
    """type=N 里的"样本股"仍要被名称规则挡掉。"""
    assert is_concept_board("883300.TI", "沪深300样本股", board_type="N") is False
    assert is_concept_board("883301.TI", "上证50样本股", board_type="N") is False


@pytest.mark.parametrize(("code", "name"), [
    ("885800.TI", "光刻胶"),
    ("885801.TI", "低空经济"),
    ("885802.TI", "PCB概念"),
])
def test_concept_boards_without_type_are_kept(code, name) -> None:
    """`type` 为空（dim_concept 独有）且代码段/名称都不命中排除规则 → 仍算概念。"""
    assert is_concept_board(code, name, board_type="") is True


def test_is_concept_board_survives_bad_regex(monkeypatch) -> None:
    """配置里写了坏正则：忽略那一条，不能因此把所有板块判成非概念。"""
    config = load_config()
    original = list(config.data.non_concept_name_patterns)
    config.data.non_concept_name_patterns = ["([unclosed", *original]
    try:
        assert is_concept_board("885800.TI", "半导体", config) is True
    finally:
        config.data.non_concept_name_patterns = original


# ======================================================================
# 存储
# ======================================================================

def test_init_tables_idempotent(conn: sqlite3.Connection) -> None:
    db.init_tables(conn)
    db.init_tables(conn)
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {db.DAILY_TABLE, db.META_TABLE, db.MEMBER_TABLE, db.WATCH_TABLE} <= names


def test_upsert_is_idempotent_and_updates_values(conn: sqlite3.Connection) -> None:
    """同一 (trade_date, sector_code) 重复写不产生新行，数值被更新。"""
    base = {"trade_date": "20260915", "sector_code": "885800.TI",
            "sector_name": "半导体", "sector_amount": 100.0,
            "market_amount": 1000.0, "raw_crowding": 0.1,
            "ma5_crowding": 0.1, "water_level": 0.5}
    assert db.upsert_sector_crowding(conn, [base]) == 1
    assert db.count_rows(conn) == 1
    assert db.upsert_sector_crowding(conn, [{**base, "water_level": 0.9}]) == 1
    assert db.count_rows(conn) == 1
    stored = db.query_sector_crowding(conn, "885800.TI")
    assert stored[0]["water_level"] == pytest.approx(0.9)
    assert stored[0]["created_at"] == stored[0]["created_at"]   # 不置空


def test_get_last_update_date_roundtrip(conn: sqlite3.Connection) -> None:
    assert db.get_last_update_date(conn, "885800.TI") == ""
    db.update_sector_meta(conn, sector_code="885800.TI", sector_name="半导体",
                          last_update_date="20260915")
    assert db.get_last_update_date(conn, "885800.TI") == "20260915"


def test_update_sector_meta_can_refuse_to_advance_watermark(
        conn: sqlite3.Connection) -> None:
    """**关键**：`advance_watermark=False` 时水位线不动（失败不许推进）。"""
    db.update_sector_meta(conn, sector_code="A.TI", last_update_date="20260910")
    db.update_sector_meta(conn, sector_code="A.TI", last_update_date="20260915",
                          advance_watermark=False)
    assert db.get_last_update_date(conn, "A.TI") == "20260910"
    db.update_sector_meta(conn, sector_code="A.TI", last_update_date="20260915")
    assert db.get_last_update_date(conn, "A.TI") == "20260915"


def test_update_sector_meta_keeps_existing_name_when_blank(
        conn: sqlite3.Connection) -> None:
    db.update_sector_meta(conn, sector_code="A.TI", sector_name="半导体")
    db.update_sector_meta(conn, sector_code="A.TI", sector_name="")
    meta = db.query_sector_meta(conn)[0]
    assert meta["sector_name"] == "半导体"


def test_query_alerts_threshold_and_sorting(conn: sqlite3.Connection) -> None:
    day = "20260915"
    rows = [
        {"trade_date": day, "sector_code": "A.TI", "sector_name": "低",
         "water_level": 0.5, "ma5_crowding": 0.001},
        {"trade_date": day, "sector_code": "B.TI", "sector_name": "中",
         "water_level": 0.85, "ma5_crowding": 0.002},
        {"trade_date": day, "sector_code": "C.TI", "sector_name": "高",
         "water_level": 0.95, "ma5_crowding": 0.003},
        {"trade_date": day, "sector_code": "D.TI", "sector_name": "无数据",
         "water_level": None, "ma5_crowding": None},
    ]
    db.upsert_sector_crowding(conn, rows)
    alerts = db.query_alerts(conn, threshold=0.8)
    assert [item["sector_name"] for item in alerts] == ["高", "中"]   # 降序
    # NULL 水位不参与告警（"不知道"不等于"拥挤"）
    assert all(item["water_level"] is not None for item in alerts)
    assert len(db.query_alerts(conn, threshold=0.99)) == 0


def test_query_alerts_concepts_only_filter(conn: sqlite3.Connection) -> None:
    day = "20260915"
    db.upsert_sector_crowding(conn, [
        {"trade_date": day, "sector_code": "885800.TI", "sector_name": "半导体",
         "water_level": 0.9},
        {"trade_date": day, "sector_code": "882001.TI", "sector_name": "安徽",
         "water_level": 0.95},
    ])
    db.update_sector_meta(conn, sector_code="885800.TI", sector_name="半导体",
                          is_concept=True)
    db.update_sector_meta(conn, sector_code="882001.TI", sector_name="安徽",
                          is_concept=False)
    only_concepts = db.query_alerts(conn, threshold=0.8, concepts_only=True)
    assert [item["sector_name"] for item in only_concepts] == ["半导体"]
    everything = db.query_alerts(conn, threshold=0.8, concepts_only=False)
    assert [item["sector_name"] for item in everything] == ["安徽", "半导体"]


def test_query_all_latest_uses_single_trade_date(conn: sqlite3.Connection) -> None:
    """总览只取**全库最大交易日**：不能各板块取各自最新日（停牌板块会混进来）。"""
    db.upsert_sector_crowding(conn, [
        {"trade_date": "20260914", "sector_code": "A.TI", "water_level": 0.9},
        {"trade_date": "20260915", "sector_code": "B.TI", "water_level": 0.3},
    ])
    rows = db.query_all_latest_water_level(conn)
    assert [item["sector_code"] for item in rows] == ["B.TI"]


def test_watchlist_crud(conn: sqlite3.Connection) -> None:
    assert db.add_to_watchlist(conn, "885800.TI", sector_name="半导体") is True
    assert db.add_to_watchlist(conn, "885800.TI", sector_name="半导体") is False
    assert db.watchlist_codes(conn) == {"885800.TI"}
    assert db.remove_from_watchlist(conn, "885800.TI") is True
    assert db.remove_from_watchlist(conn, "885800.TI") is False
    assert db.watchlist_codes(conn) == set()


def test_watchlist_joins_latest_water_level(conn: sqlite3.Connection) -> None:
    db.upsert_sector_crowding(conn, [
        {"trade_date": "20260915", "sector_code": "A.TI", "sector_name": "半导体",
         "water_level": 0.87, "ma5_crowding": 0.002}])
    db.update_sector_meta(conn, sector_code="A.TI", sector_name="半导体")
    db.add_to_watchlist(conn, "A.TI", sector_name="半导体")
    items = db.list_watchlist(conn)
    assert len(items) == 1
    assert items[0]["water_level"] == pytest.approx(0.87)


def test_add_to_watchlist_rejects_blank(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        db.add_to_watchlist(conn, "   ")


def test_search_sectors(conn: sqlite3.Connection) -> None:
    db.update_sector_meta(conn, sector_code="885800.TI", sector_name="半导体")
    db.update_sector_meta(conn, sector_code="885801.TI", sector_name="半导体材料")
    db.update_sector_meta(conn, sector_code="885900.TI", sector_name="白酒")
    assert len(db.search_sectors(conn, "半导体")) == 2
    assert len(db.search_sectors(conn, "白酒")) == 1
    assert db.search_sectors(conn, "") == []


# ======================================================================
# 刷新任务（不碰网络）
# ======================================================================

def test_refresh_task_progress_and_message() -> None:
    task = RefreshTask(task_id="t1", status="running", total=10, processed=4)
    assert task.progress == pytest.approx(0.4)
    assert "已处理 4/10" in task.to_dict()["message"]

    task.status = "done"
    task.inserted = 123
    task.last_update_date = "20260915"
    message = task.to_dict()["message"]
    assert "新增 123 条" in message and "20260915" in message

    task.status = "failed"
    task.error = "boom"
    assert "boom" in task.to_dict()["message"]


def test_refresh_task_progress_never_exceeds_one() -> None:
    task = RefreshTask(task_id="t", total=3, processed=99)
    assert task.progress == 1.0
    assert RefreshTask(task_id="t", total=0).progress == 0.0


def test_get_refresh_progress_idle_and_unknown() -> None:
    from src.sector_crowding import refresh as refresh_module

    with refresh_module._TASKS_LOCK:            # noqa: SLF001 测试需要清空任务表
        refresh_module._TASKS.clear()
    assert refresh.get_refresh_progress()["status"] == "idle"
    assert refresh.get_refresh_progress("nope")["status"] == "unknown"


def test_start_refresh_all_reuses_running_task(monkeypatch) -> None:
    """已有任务在跑时**复用**它 —— 并发跑两轮会互相抢数据库锁。"""
    from src.sector_crowding import refresh as refresh_module

    with refresh_module._TASKS_LOCK:            # noqa: SLF001
        refresh_module._TASKS.clear()
        running = RefreshTask(task_id="running1", status="running", total=5)
        running.started_at = "2026-09-18T10:00:00+08:00"
        refresh_module._TASKS["running1"] = running

    started_threads: list[str] = []

    def fake_thread(**kwargs):
        started_threads.append(kwargs.get("name", ""))
        return None

    monkeypatch.setattr(refresh_module.threading, "Thread", fake_thread)
    outcome = refresh.start_refresh_all()
    assert outcome["task_id"] == "running1"
    assert started_threads == []                # 没有起新线程

    with refresh_module._TASKS_LOCK:            # noqa: SLF001
        refresh_module._TASKS.clear()


def test_task_registry_is_bounded() -> None:
    """任务表不能无限增长（长期运行会累积内存）。"""
    from src.sector_crowding import refresh as refresh_module

    with refresh_module._TASKS_LOCK:            # noqa: SLF001
        refresh_module._TASKS.clear()
    for index in range(refresh_module._TASKS_MAX + 6):
        task = RefreshTask(task_id=f"t{index:03d}")
        task.started_at = f"2026-09-18T10:{index:02d}:00+08:00"
        refresh_module._register(task)          # noqa: SLF001
    with refresh_module._TASKS_LOCK:            # noqa: SLF001
        size = len(refresh_module._TASKS)
        refresh_module._TASKS.clear()
    assert size <= refresh_module._TASKS_MAX


# ======================================================================
# 刷新水位线逻辑（用假数据源，不碰网络）
# ======================================================================

def test_refresh_single_sector_full_backfill_then_incremental(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """首次全量回填 → 第二次走增量 → 第三次幂等跳过。

    这是"一键刷新"最核心的行为，且必须验证 `last_update_date` 被正确推进。
    """
    config = load_config()
    config.database.path = str(tmp_path / "c.db")
    warehouse_path = tmp_path / "w.db"
    _make_warehouse(warehouse_path, days=["20260910", "20260911", "20260914"])
    config.database.warehouse_path = str(warehouse_path)

    calls: list[tuple[str, str]] = []

    def fake_fetch(_config, sector_code, *, start, end):
        calls.append((start, end))
        return [{"trade_date": day, "sector_amount": 1e10}
                for day in ("20260910", "20260911", "20260914")
                if start <= day <= end]

    monkeypatch.setattr(refresh.sources, "fetch_board_daily", fake_fetch)

    conn = db.get_db_connection(config)
    db.init_tables(conn)
    try:
        first = refresh.refresh_single_sector("885800.TI", conn=conn,
                                             sector_name="半导体", config=config)
        assert first["status"] == "ok"
        assert first["full_backfill"] is True
        assert db.get_last_update_date(conn, "885800.TI") == "20260914"
        assert first["inserted"] == 3

        second = refresh.refresh_single_sector("885800.TI", conn=conn,
                                              config=config)
        assert second["status"] == "skipped"      # 已是最新
        assert db.count_rows(conn) == 3           # 没有重复行
    finally:
        conn.close()


def test_refresh_does_not_advance_watermark_on_fetch_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**关键**：拉取失败时水位线不许推进，否则那段日期成为永久空洞。"""
    config = load_config()
    config.database.path = str(tmp_path / "c2.db")
    warehouse_path = tmp_path / "w2.db"
    _make_warehouse(warehouse_path, days=["20260914"])
    config.database.warehouse_path = str(warehouse_path)

    conn = db.get_db_connection(config)
    db.init_tables(conn)
    try:
        # 先成功写入一天并推进水位线
        db.update_sector_meta(conn, sector_code="A.TI", last_update_date="20260910")
        before = db.get_last_update_date(conn, "A.TI")

        def boom(*_args, **_kwargs):
            raise RuntimeError("Tushare 挂了")

        monkeypatch.setattr(refresh.sources, "fetch_board_daily", boom)
        with pytest.raises(RuntimeError):
            refresh.refresh_single_sector("A.TI", conn=conn, config=config)
        assert db.get_last_update_date(conn, "A.TI") == before
    finally:
        conn.close()


def test_refresh_skips_when_warehouse_missing(tmp_path: Path) -> None:
    config = load_config()
    config.database.path = str(tmp_path / "c3.db")
    config.database.warehouse_path = str(tmp_path / "missing.db")
    conn = db.get_db_connection(config)
    db.init_tables(conn)
    try:
        with pytest.raises(RuntimeError, match="仓库"):
            refresh.refresh_single_sector("A.TI", conn=conn, config=config)
    finally:
        conn.close()


def _make_warehouse(path: Path, *, days: list[str]) -> None:
    """造一个最小仓库：只要 quant_daily(trade_date, amount) 够用。"""
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE quant_daily (trade_date TEXT, code TEXT, amount REAL)")
        connection.executemany(
            "INSERT INTO quant_daily VALUES (?,?,?)",
            [(day, "600150", 1e9) for day in days])
        connection.commit()
    finally:
        connection.close()


def test_market_amount_excludes_zero_rows(tmp_path: Path) -> None:
    """`amount <= 0` 的行不参与求和：当 0 算会低估分母、把正常板块顶成告警。"""
    path = tmp_path / "m.db"
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE quant_daily (trade_date TEXT, code TEXT, amount REAL)")
        connection.executemany("INSERT INTO quant_daily VALUES (?,?,?)", [
            ("20260915", "600150", 100.0),
            ("20260915", "300308", 0.0),
            ("20260915", "000001", None),
        ])
        connection.commit()
    finally:
        connection.close()

    from src.sector_crowding import sources

    sources.clear_market_cache()
    with sqlite3.connect(str(path)) as connection:
        result = sources.market_amount_by_day(connection, start="20260901",
                                              end="20260930", use_cache=False)
    assert result["20260915"] == pytest.approx(100.0)


def test_lookback_start_uses_calendar_years() -> None:
    """近 6 年按**日历**推：按 250×6 交易日会少几周窗口。"""
    from src.sector_crowding import sources

    assert sources.lookback_start("20260915", 6) == "20200916"
    assert sources.lookback_start("20260915", 1) == "20250915"


def test_amount_from_row_prefers_amount_then_vol_times_price() -> None:
    from src.sector_crowding.sources import _amount_from_row

    assert _amount_from_row({"amount": 500.0, "vol": 10.0,
                             "avg_price": 20.0}) == pytest.approx(500.0)
    assert _amount_from_row({"vol": 10.0, "avg_price": 20.0}) == pytest.approx(200.0)
    # avg_price 缺失退到 close（口径略差但不静默丢数据）
    assert _amount_from_row({"vol": 10.0, "close": 20.0}) == pytest.approx(200.0)
    # 都没有 → None（不编造 0）
    assert _amount_from_row({"vol": 10.0}) is None
    assert _amount_from_row({"vol": 0.0, "avg_price": 20.0}) is None


# ======================================================================
# 参数变更后的本地重算 / 重分类（不联网）
# ======================================================================

def test_recompute_clears_water_level_when_below_min_bars(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**关键**：`min_bars` 调高后，样本不足的板块水位必须被**清成 NULL**。

    实测踩到的场景：首次全量回填后把 `min_bars_for_water_level` 从 60 提到 750，
    若不重算，库里仍留着按 60 根算出的水位 —— 那些"历史太短所以恒为 100%"的板块
    会继续误告警（实测 218 个 → 50 个）。
    """
    config = load_config()
    config.database.path = str(tmp_path / "rc.db")
    conn = db.get_db_connection(config)
    db.init_tables(conn)
    try:
        # 用很低的 min_bars 先算出水位并写库
        raw = _rows(100, share=0.01)
        computed = refresh.compute_series(raw, ma_window=5, min_bars=1)
        db.upsert_sector_crowding(conn, [
            {**item, "sector_code": "A.TI", "sector_name": "小样本"} for item in computed])
        assert db.query_sector_crowding(conn, "A.TI")[-1]["water_level"] is not None

        # 把闸门提到 750（> 100 行）后本地重算
        config.window.min_bars_for_water_level = 750
        stats = refresh.recompute_stored_water_levels(config=config, conn=conn)
        assert stats["skipped"] == 1 and stats["sectors"] == 0
        assert db.query_sector_crowding(conn, "A.TI")[-1]["water_level"] is None
    finally:
        conn.close()


def test_recompute_keeps_water_level_when_enough_bars(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config()
    config.database.path = str(tmp_path / "rc2.db")
    conn = db.get_db_connection(config)
    db.init_tables(conn)
    try:
        raw = _rows(80, share=0.01)
        computed = refresh.compute_series(raw, ma_window=5, min_bars=1)
        db.upsert_sector_crowding(conn, [
            {**item, "sector_code": "B.TI", "sector_name": "足样本"} for item in computed])
        config.window.min_bars_for_water_level = 60
        stats = refresh.recompute_stored_water_levels(config=config, conn=conn)
        assert stats["sectors"] == 1
        assert db.query_sector_crowding(conn, "B.TI")[-1]["water_level"] is not None
    finally:
        conn.close()


def test_reclassify_updates_is_concept_without_touching_watermark(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """重分类只改 `is_concept`，**不能**动 `last_update_date`（否则增量会重抓）。"""
    config = load_config()
    config.database.path = str(tmp_path / "rf.db")
    conn = db.get_db_connection(config)
    db.init_tables(conn)
    try:
        db.update_sector_meta(conn, sector_code="885800.TI", sector_name="半导体",
                              is_concept=True, last_update_date="20260915")
        db.update_sector_meta(conn, sector_code="861003.TI", sector_name="化学制品",
                              is_concept=True, last_update_date="20260915")

        monkeypatch.setattr(refresh.sources, "list_boards", lambda _config: [
            {"sector_code": "885800.TI", "sector_name": "半导体",
             "is_concept": True, "board_type": "N"},
            {"sector_code": "861003.TI", "sector_name": "化学制品",
             "is_concept": False, "board_type": ""},
        ])
        outcome = refresh.reclassify_boards(config=config, conn=conn)
        assert outcome["changed"] == 1
        meta = {row["sector_code"]: row for row in db.query_sector_meta(conn)}
        assert bool(meta["861003.TI"]["is_concept"]) is False
        assert bool(meta["885800.TI"]["is_concept"]) is True
        # 水位线没有被碰
        assert meta["861003.TI"]["last_update_date"] == "20260915"
        assert meta["885800.TI"]["last_update_date"] == "20260915"
    finally:
        conn.close()


# ======================================================================
# 成分股来源：优先用主线挖掘的提纯名单
# ======================================================================


def _pure_db(tmp_path: Path, rows: list[tuple[str, str, int]]) -> Path:
    """造一个最小 `ml_member_pure` 库（`(board_code, code, relevant)`）。"""
    path = tmp_path / "mainline_cache.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ml_member_pure("
                 "board_code TEXT, code TEXT, relevant INTEGER)")
    conn.executemany("INSERT INTO ml_member_pure VALUES(?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return path


def test_pure_members_take_priority_over_raw(tmp_path: Path) -> None:
    """提纯名单可用时，**不能**再去用 `sector_member` 的原始名单。

    这是本轮的真实缺陷：拥挤度的资金流列一直在用原始 `ths_member` 名单，
    而主线挖掘早已换成提纯名单 —— 同一时刻两个子系统在讲不同的故事。
    """
    from src.sector_crowding import metrics

    pure = _pure_db(tmp_path, [("885937.TI", f"00{i:04d}", 1) for i in range(11)])
    conn = db.get_db_connection(path=tmp_path / "crowding.db")
    db.init_tables(conn)
    try:
        db.upsert_members(conn, "885937.TI", [
            {"code": f"99{i:04d}", "name": "原始沾边股"} for i in range(20)])
        config = load_config()
        config.metrics.min_purified_members = 3
        pure_conn = metrics.open_pure_db(pure)
        members, source = metrics._sector_members_cached(  # noqa: SLF001
            conn, "885937.TI", config=config, pure_conn=pure_conn)
        assert source == "purified"
        assert len(members) == 11
        assert all(code.startswith("00") for code in members)
    finally:
        conn.close()


def test_pure_members_fall_back_when_degenerate(tmp_path: Path) -> None:
    """提纯退化（只留下 1 只）时必须回退原始名单。

    实测 885699 原始 256 只被压到 1 只 —— 那是提纯没跑完，不是"这个板块
    只有一只相关股"。拿单只股票算资金流，噪声远大于信号。
    """
    from src.sector_crowding import metrics

    pure = _pure_db(tmp_path, [("885699.TI", "000001", 1)])
    conn = db.get_db_connection(path=tmp_path / "crowding.db")
    db.init_tables(conn)
    try:
        db.upsert_members(conn, "885699.TI", [
            {"code": f"99{i:04d}", "name": "原始"} for i in range(4)])
        config = load_config()
        config.metrics.min_purified_members = 3
        members, source = metrics._sector_members_cached(  # noqa: SLF001
            conn, "885699.TI", config=config,
            pure_conn=metrics.open_pure_db(pure))
        assert source == "raw"
        assert len(members) == 4
    finally:
        conn.close()


def test_pure_members_disabled_uses_raw(tmp_path: Path) -> None:
    """`use_purified_members=False` 是给"提纯前后对照"用的开关。"""
    from src.sector_crowding import metrics

    pure = _pure_db(tmp_path, [("885937.TI", f"00{i:04d}", 1) for i in range(11)])
    conn = db.get_db_connection(path=tmp_path / "crowding.db")
    db.init_tables(conn)
    try:
        db.upsert_members(conn, "885937.TI", [
            {"code": f"99{i:04d}", "name": "原始"} for i in range(20)])
        config = load_config()
        config.metrics.use_purified_members = False
        members, source = metrics._sector_members_cached(  # noqa: SLF001
            conn, "885937.TI", config=config,
            pure_conn=metrics.open_pure_db(pure))
        assert source == "raw"
        assert len(members) == 20
    finally:
        conn.close()


def test_open_pure_db_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """主线库不存在时安静回退，不该让整轮周频任务炸掉。"""
    from src.sector_crowding import metrics

    assert metrics.open_pure_db(tmp_path / "nope.db") is None
    assert metrics.open_pure_db(tmp_path) is None       # 传目录也不行


def test_metric_row_records_member_source(conn: sqlite3.Connection) -> None:
    """`member_source` 必须落库 —— 否则无法回答"提纯名单到底生效没有"。"""
    written = db.upsert_metrics(conn, [{
        "sector_code": "885937.TI", "sector_name": "培育钻石",
        "compute_week": "2026-W38", "computed_at": "2026-09-21T10:00:00",
        "flow_base_date": "20260818", "flow_last_date": "20260915",
        "net_inflow": 1.0e8, "circ_mv_base": 5.0e10, "flow_ratio": 0.2,
        "member_source": "purified",
    }])
    assert written == 1
    row = conn.execute(
        f"SELECT member_source FROM {db.METRIC_TABLE}"
        " WHERE sector_code='885937.TI'").fetchone()
    assert row["member_source"] == "purified"


def test_member_source_column_is_added_to_legacy_table(tmp_path: Path) -> None:
    """老库（没有 `member_source` 列）要能靠 `_ADDABLE` 补列，不用重建表。"""
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.execute(f"CREATE TABLE {db.METRIC_TABLE} ("
                   "id INTEGER PRIMARY KEY AUTOINCREMENT, sector_code TEXT,"
                   " sector_name TEXT, compute_week TEXT, computed_at TEXT,"
                   " base_date_5d TEXT, chg_5d REAL, base_date_1m TEXT,"
                   " chg_1m REAL, base_date_2m TEXT, chg_2m REAL,"
                   " flow_base_date TEXT, flow_last_date TEXT,"
                   " net_inflow REAL, circ_mv_base REAL, flow_ratio REAL,"
                   " UNIQUE(sector_code, compute_week))")
    legacy.commit()
    legacy.close()
    conn = db.get_db_connection(path=path)
    try:
        db.init_tables(conn)
        columns = {row["name"] for row in conn.execute(
            f"PRAGMA table_info({db.METRIC_TABLE})")}
        assert "member_source" in columns
    finally:
        conn.close()


# ==================================================================
# 板块黑名单：三个范围共用一个开关（2026-09-22 用户第四批）
# ==================================================================


def _seed_daily(conn: sqlite3.Connection, code: str, days: int = 8) -> None:
    """给某板块写 `days` 天的水位（每天一个交易日，够算 5 日变化）。"""
    for index in range(days):
        conn.execute(
            f"INSERT INTO {db.DAILY_TABLE}(trade_date, sector_code,"
            " sector_name, water_level, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (f"202609{index + 1:02d}", code, code, 0.5 + index * 0.01,
             "2026-09-22", "2026-09-22"))
    conn.commit()


def test_water_changes_skip_blacklisted_sectors(monkeypatch,
                                                conn: sqlite3.Connection) -> None:
    """黑名单里的板块**不算拥挤度指标**。

    用户口径是「不计入板块拥挤度**检测**范围」。`refresh._drop_blacklisted()`
    只挡日更（不再写新行），而 `compute_water_changes` 是**全表扫描** ——
    不在这里过滤的话，黑名单板块会带着停更前的老数据继续参与指标计算。
    这条测试钉住"三个范围（主线池 / 日更 / 拥挤度指标）共用一个开关"。
    """
    from src.sector_crowding import metrics

    _seed_daily(conn, "KEEP.TI")
    _seed_daily(conn, "DROP.TI")
    monkeypatch.setattr(metrics, "_sector_blacklist",
                        lambda: frozenset({"DROP.TI"}))
    changes, latest = metrics.compute_water_changes(conn)
    assert "KEEP.TI" in changes
    assert "DROP.TI" not in changes, "黑名单板块不得参与拥挤度指标计算"
    assert latest == "20260908"


def test_water_changes_keep_all_when_blacklist_unavailable(
        monkeypatch, conn: sqlite3.Connection) -> None:
    """黑名单**读不到**时不能当成"排除全部" —— 那会一个板块都不算。

    与 `refresh._drop_blacklisted` 同一口径：不可用 = 不过滤。
    """
    from src.sector_crowding import metrics

    _seed_daily(conn, "A.TI")
    _seed_daily(conn, "B.TI")
    monkeypatch.setattr(metrics, "_sector_blacklist", lambda: frozenset())
    changes, _latest = metrics.compute_water_changes(conn)
    assert {"A.TI", "B.TI"} <= set(changes)


def test_sector_blacklist_helper_returns_empty_on_failure(monkeypatch) -> None:
    """两份清单加载器**都**抛异常时，`_sector_blacklist()` 返回空集而不是把任务打挂。

    ⚠️ 必须把**两份**都打坏：剔除来源有两个（主线批次点名 + 拥挤度剔除清单），
    只坏一份时另一份仍会贡献代码 —— 第一版只坏了主线那份，断言就失败了。
    """
    import src.mainline.config as mainline_config
    from src.sector_crowding import db, metrics

    def boom(*_a, **_kw):
        raise RuntimeError("清单坏了")

    monkeypatch.setattr(mainline_config, "load_removed_concepts", boom)
    monkeypatch.setattr(metrics, "load_crowding_exclusions", boom)
    monkeypatch.setattr(db, "load_crowding_exclusions", boom)
    assert metrics._sector_blacklist() == frozenset()
    assert db._sector_blacklist() == frozenset()


def test_query_metrics_hides_removed_boards(monkeypatch,
                                           conn: sqlite3.Connection) -> None:
    """**"删了还在"的回归测试**：批次剔除的板块不得出现在拥挤度读取结果里。

    用户剔除一个板块后，**当周已经算好的指标行还留在
    `sector_crowding_metric` 里**（黑名单过滤只对之后算的周生效），
    前端读这张表 → 板块"删了还在"。用户为此把同一批板块报了两遍。
    所以读取侧必须兜住，而不是去删行（历史行按 `refresh` 的口径要保留）。
    """
    from src.sector_crowding import db

    db.upsert_metrics(conn, [
        {"sector_code": "KEEP.TI", "sector_name": "保留", "compute_week": "2026-W39"},
        {"sector_code": "GONE.TI", "sector_name": "已剔", "compute_week": "2026-W39"},
    ])
    monkeypatch.setattr(db, "_sector_blacklist", lambda: frozenset({"GONE.TI"}))
    got = db.query_metrics(conn, week="2026-W39")
    assert "KEEP.TI" in got
    assert "GONE.TI" not in got, "批次剔除的板块不得出现在拥挤度指标读取结果里"


def test_seed_list_does_not_resurrect_blacklisted_boards(
        monkeypatch, conn: sqlite3.Connection) -> None:
    """**"删了又自己长回来"的回归测试**：`seed_list` 不得种回被剔除的板块。

    实测复现的回路（2026-09-23，参股银行 885835.TI）：

        用户删板块 → prune 置 visible=0 → 用户要求"彻底去掉"→ 删掉清单行
        → 前端下一次调 `/config_list` → `seed_list()` 看到
          "该板块在 `sector_meta` 里 bars=1456>0、且不在清单表里"
        → **重新插入 visible=1** → 板块回到前端可见列表（无任何日志）

    根因是判据混用：`bars>0` 是"**有没有数据**"的事实陈述，黑名单才是
    "**用户还要不要它**"的意图陈述。`sector_meta` 的历史**刻意保留**
    （不删历史是可逆性的前提），所以判据必须在读取侧。

    与 `test_query_metrics_hides_removed_boards` 同型：
    读取侧的每个入口都要兜住，只修一个入口就会有别的入口把删除抹掉。
    """
    from src.sector_crowding import db

    db.update_sector_meta(conn, sector_code="KEEP.TI", sector_name="保留", bars=100)
    db.update_sector_meta(conn, sector_code="GONE.TI", sector_name="已剔", bars=1456)
    monkeypatch.setattr(db, "_sector_blacklist", lambda: frozenset({"GONE.TI"}))
    added = db.seed_list(conn, concepts_only=False)
    codes = {str(row[0]) for row in conn.execute(
        f"SELECT sector_code FROM {db.LIST_TABLE}")}
    assert "KEEP.TI" in codes
    assert "GONE.TI" not in codes, "被剔除的板块不得被 seed_list 种回清单"
    assert added == len(codes), "新增行数应与实际落库的清单行一致"


def test_seed_list_still_seeds_when_blacklist_unavailable(
        monkeypatch, conn: sqlite3.Connection) -> None:
    """清单不可用时**照常种子化** —— 否则前端会一片空白（与其它读取侧同口径）。"""
    from src.sector_crowding import db

    db.update_sector_meta(conn, sector_code="A.TI", sector_name="A", bars=10)
    monkeypatch.setattr(db, "_sector_blacklist", lambda: frozenset())
    db.seed_list(conn, concepts_only=False)
    codes = {str(row[0]) for row in conn.execute(
        f"SELECT sector_code FROM {db.LIST_TABLE}")}
    assert "A.TI" in codes


def test_query_metrics_keeps_history_when_blacklist_unavailable(
        monkeypatch, conn: sqlite3.Connection) -> None:
    """清单不可用时**不过滤** —— 把"读不到"当成"排除全部"会让前端一片空白。"""
    from src.sector_crowding import db

    db.upsert_metrics(conn, [
        {"sector_code": "A.TI", "sector_name": "A", "compute_week": "2026-W39"},
        {"sector_code": "B.TI", "sector_name": "B", "compute_week": "2026-W39"},
    ])
    monkeypatch.setattr(db, "_sector_blacklist", lambda: frozenset())
    got = db.query_metrics(conn, week="2026-W39")
    assert {"A.TI", "B.TI"} <= set(got)


def test_removed_concepts_is_the_scoped_list_not_the_whole_blacklist() -> None:
    """`load_removed_concepts()` 只收**批次点名**那批，不是整份黑名单。

    整份黑名单 600+ 条里大头是历史遗留（865xxx 概念体系 / GICS 行业 /
    地域板块），而拥挤度模块**刻意保留**它们。按整份过滤会把最新一周的
    板块数从 1878 砍到 1541 —— 多砍的绝大多数是行业指数，那是过度过滤。
    """
    from src.mainline.config import load_removed_concepts, load_sector_blacklist

    removed = load_removed_concepts()
    whole = load_sector_blacklist()
    assert removed is not None and whole is not None
    assert set(removed) < set(whole), "批次点名必须是整份黑名单的真子集"
    assert len(removed) < 300, "批次点名不该把历史遗留那 400+ 条吃进来"
    # 用户点名的三个必须在里面（血氧仪 / 信创 / 光伏概念）
    assert {"886028.TI", "886013.TI", "885531.TI"} <= set(removed)
    # 没被点名的板块不该被误伤
    assert "885573.TI" not in removed          # 猪肉（用户特意保下来的）


def test_crowding_exclusions_shipped_config_loads() -> None:
    """线上那份拥挤度剔除清单必须真的被解析出来（否则等于没生效）。

    ⚠️ 代码是 `scripts/prune_crowding.py --emit-config` **从库里查出来的**，
    不是手写的 —— 第一版手写 6 个代码里有 4 个是错的（会把三胎概念 /
    食品安全 / 元宇宙删掉）。这条测试顺带钉住"清单里的代码必须真存在于
    `sector_crowding_list`"，防止有人再手写。
    """
    from src.sector_crowding.config import load_crowding_exclusions

    ex = load_crowding_exclusions()
    assert ex is not None, "线上清单必须可读"
    assert len(ex) > 50, f"清单不该这么少（实际 {len(ex)}）"
    # 用户点名的几个
    assert {"885521.TI", "886026.TI", "886021.TI", "886019.TI"} <= ex


def test_metrics_and_db_blacklist_include_crowding_exclusions() -> None:
    """三处机制（算指标 / 显示 / 停日更）必须共用同一份拥挤度清单。"""
    from src.sector_crowding import db, metrics, refresh
    from src.sector_crowding.config import load_crowding_exclusions

    ex = load_crowding_exclusions() or frozenset()
    sample = sorted(ex)[:5]
    for helper in (metrics._sector_blacklist, db._sector_blacklist):
        got = helper()
        assert set(sample) <= set(got), f"{helper.__module__} 没并入拥挤度清单"
    boards = [{"sector_code": c} for c in list(sample) + ["885700.TI"]]
    kept, blocked = refresh._drop_blacklisted(boards)
    assert blocked == len(sample), "停日更没有挡掉拥挤度剔除清单里的板块"
    assert [b["sector_code"] for b in kept] == ["885700.TI"]


def test_blacklist_helpers_survive_missing_files(monkeypatch) -> None:
    """两份清单都读不到时：返回空集 = **不过滤**，而不是"排除全部"。

    把"读不到"当成"排除全部"会让拥挤度一个板块都不算、前端一片空白 ——
    比"多算几个"严重得多。

    ⚠️ 要从**模块自己的绑定**上打补丁：`metrics` / `db` 用的是
    `from … import load_crowding_exclusions`，改源模块的属性不会影响
    它们已经绑定的名字（第一版就是这么写错的）。
    """
    import src.mainline.config as mc
    from src.sector_crowding import db, metrics

    monkeypatch.setattr(mc, "load_removed_concepts", lambda *a, **k: None)
    monkeypatch.setattr(metrics, "load_crowding_exclusions",
                        lambda *a, **k: None)
    monkeypatch.setattr(db, "load_crowding_exclusions", lambda *a, **k: None)
    assert metrics._sector_blacklist() == frozenset()
    assert db._sector_blacklist() == frozenset()
