"""护栏：列级数据资产发现与反向索引（B1）。

## 这个文件守的那类缺陷

用户连问三轮"数据在库里为什么取不到"，根因是同一个：

    系统的"我能拿到什么"由**人工维护的契约列表**决定
    （indicators.yaml / _CODE_PREFIXES / planner 目录 / 白名单），
    而**库里实际有什么**从来没有一份机器可读的清单。

实测代价（本轮）：

    `quant_daily_basic`  **11,837,850 行**、含 `dv_ratio`(股息率) / `total_mv` /
    `turnover_rate` —— **投研链路零命中**（SmartFetcher 与数据仓储里
    `quant_daily_basic` 出现 0 次）。"库里有 1180 万行，Agent 一条看不到"。
    而 `data_asset_catalog` 早就登记了**表**，只是**列**被塞在 extras_json 里没人用。

## 判据纪律

· **不依赖本机数据**：核心断言用临时库自造数据，避免"开发机绿、CI 红"。
· **假阴性比报错更危险**：第一版 `data/*.db` 不递归 → 只收到 1 个库 →
  反向索引在 dev/pilot 上**全部 0 命中**（看着像"库里没有"，其实是没扫到）。
  所以专门有一条断言"递归发现"。
· **顺序决定谁被挤掉**：第一版把 `mainline_cache_archive.db` 排在前面，
  主库被体积上限挡住 → 又一次假阴性。所以有断言"优先库排在前面"。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.infrastructure.catalog.column_index import (
    INVESTABLE_COLUMN_HINTS,
    PREFERRED_DB_NAMES,
    SKIP_DB_PATTERNS,
    SKIP_TABLE_PREFIXES,
    ColumnIndex,
    is_investable_column,
)


# ============================================================
# 夹具：自造一个"迷你版项目"（不碰真实数据）
# ============================================================


@pytest.fixture()
def mini_project(tmp_path: Path) -> Path:
    """造出 `data/` + 一个子目录里的库，验证**递归发现**。"""
    (tmp_path / "data" / "dev").mkdir(parents=True)
    (tmp_path / "src" / "infrastructure" / "connectors").mkdir(parents=True)

    # 主库：一张有投研列、且**没有任何源码提到它**的表
    main = tmp_path / "data" / "moss_finagent.db"
    con = sqlite3.connect(main)
    con.execute("""CREATE TABLE fact_data_points (
        data_id TEXT, indicator TEXT, value REAL, period_date TEXT)""")
    con.execute("INSERT INTO fact_data_points VALUES ('d1','CPI',0.8,'2026-09-01')")
    con.execute("""CREATE TABLE sector_crowding_daily (
        board_code TEXT, trade_date TEXT, raw_crowding REAL,
        sector_amount REAL)""")
    con.execute("INSERT INTO sector_crowding_daily VALUES "
                "('BK1','20260924',0.81,123.4)")
    con.execute("CREATE TABLE fact_session (token TEXT, user_id TEXT)")
    con.commit()
    con.close()

    # 子目录里的库（**只有递归才发现得了**）
    dev = tmp_path / "data" / "dev" / "moss_dev.db"
    con = sqlite3.connect(dev)
    con.execute("""CREATE TABLE quant_daily_basic (
        trade_date TEXT, code TEXT, dv_ratio REAL, dv_ttm REAL,
        total_mv REAL, turnover_rate REAL)""")
    con.execute("INSERT INTO quant_daily_basic VALUES "
                "('20231110','600036',5.0464,5.7626,6.2e11,0.31)")
    con.commit()
    con.close()

    # 一个"归档库"（必须被跳过）
    arc = tmp_path / "data" / "mainline_cache_archive.db"
    con = sqlite3.connect(arc)
    con.execute("CREATE TABLE jnk (a TEXT)")
    con.commit()
    con.close()
    return tmp_path


@pytest.fixture()
def idx(mini_project: Path) -> ColumnIndex:
    return ColumnIndex(project_root=mini_project).build()


# ============================================================
# ① 发现：递归 + 跳过归档 + 优先库排序
# ============================================================


def test_discovery_is_recursive(idx: ColumnIndex) -> None:
    """★ 必须扫到**子目录**里的库 —— 这是第一版的假阴性根因。"""
    names = {p.name for p in idx.discover_databases()}
    assert "moss_finagent.db" in names
    assert "moss_dev.db" in names, (
        "子目录里的库没被发现 → 反向索引会在该库上全部 0 命中，"
        "而症状看起来像『库里没有这个字段』（假阴性，比报错更难查）"
    )


def test_discovery_skips_archive_and_sidecars(idx: ColumnIndex) -> None:
    """归档/备份库要跳过；`-wal`/`-shm` 旁文件不能被当成库。"""
    names = {p.name for p in idx.discover_databases()}
    assert not any("archive" in n for n in names), "归档库混进扫描范围"
    assert not any(n.endswith(("-wal", "-shm")) for n in names)


def test_preferred_dbs_come_first(mini_project: Path) -> None:
    """★ 优先库必须排在前面 —— 顺序错了会被上限挤掉（实测踩过）。"""
    idx = ColumnIndex(project_root=mini_project)
    got = [p.name for p in idx.discover_databases()]
    assert got[0] == "moss_finagent.db", (
        f"优先库没排第一（实际 {got}）→ 数量/体积上限会先把主库挤掉"
    )
    assert PREFERRED_DB_NAMES[0] == "moss_finagent.db"


# ============================================================
# ② 反向索引：字段 → 物理位置
# ============================================================


def test_reverse_index_finds_column_in_subdirectory_db(idx: ColumnIndex) -> None:
    """★★★ 核心能力：`dv_ratio` 必须能定位到子目录库里的那张表。

    这条就是本轮报障的机器化复现 —— 修好之前，
    "股息率数据在哪"这个问题**系统答不出来**。
    """
    hits = idx.find_column("dv_ratio")
    assert hits, "反向索引找不到 dv_ratio"
    assert any(h.table == "quant_daily_basic" and h.db_name == "moss_dev.db"
               for h in hits)
    t = next(h for h in hits if h.table == "quant_daily_basic")
    assert t.row_count == 1
    assert t.entity_column == "code", "实体列没识别出来（跨票查询必需）"
    assert t.time_column == "trade_date", "时间列没识别出来（时间过滤必需）"
    assert t.latest_time == "20231110"


def test_reverse_index_is_case_insensitive_and_partial(idx: ColumnIndex) -> None:
    """大小写不敏感 + 支持部分匹配（`dv` → `dv_ratio`/`dv_ttm`）。"""
    assert idx.find_column("DV_RATIO"), "大小写敏感会让人以为字段不存在"
    partial = idx.find_column("dv")
    cols = {c for h in partial for c in h.columns if c.startswith("dv")}
    assert {"dv_ratio", "dv_ttm"} <= cols


def test_reverse_index_returns_empty_for_unknown_column(idx: ColumnIndex) -> None:
    """**没量到就是没量到**：查不到的字段返回空列表，不抛错、不编造。"""
    assert idx.find_column("this_column_does_not_exist") == []


# ============================================================
# ③ 覆盖检查：有数据但没人消费
# ============================================================


def test_unconsumed_reports_columns_no_source_mentions(idx: ColumnIndex) -> None:
    """★ 没人消费的投研相关列必须被报出来（本模块的核心产出）。

    `sector_crowding_daily.raw_crowding` 在夹具里**没有任何源码提到**，
    必须出现在清单里；否则"库里有数据但链路取不到"这类问题下次仍无人发现。
    """
    unc = idx.unconsumed_investable_columns()
    keys = {(d["table"], d["column"]) for d in unc}
    assert ("sector_crowding_daily", "raw_crowding") in keys
    assert ("quant_daily_basic", "dv_ratio") in keys, (
        "quant_daily_basic 的股息率列没被报成『未消费』—— 这正是报障现场"
    )
    # 每项都要带"够不够大、多新"，否则清单无法排序复核
    item = next(d for d in unc if d["column"] == "dv_ratio")
    assert item["row_count"] >= 1
    assert "latest_time" in item and "db" in item


def test_unconsumed_excludes_columns_mentioned_in_source(
    mini_project: Path,
) -> None:
    """★ 反向断言：**源码里提到了**的列不该被报成"没人消费"。

    没有这条，清单会退化成"把每张表的每列都列一遍"，
    人就不看了 —— 假阳性会毁掉这个能力的价值。
    """
    conn_file = mini_project / "src" / "infrastructure" / "connectors" / "x.py"
    conn_file.write_text(
        'IND = "indicator"\nVALUE = "value"\n', encoding="utf-8")
    idx = ColumnIndex(project_root=mini_project).build()
    keys = {(d["table"], d["column"]) for d in idx.unconsumed_investable_columns()}
    assert ("fact_data_points", "value") not in keys
    assert ("fact_data_points", "indicator") not in keys


def test_unconsumed_sorted_by_size_desc(idx: ColumnIndex) -> None:
    """清单按行数降序 —— 大表优先复核（人的注意力有限）。"""
    rows = [d["row_count"] for d in idx.unconsumed_investable_columns()]
    assert rows == sorted(rows, reverse=True)


# ============================================================
# ④ 判据本身
# ============================================================


def test_investable_judgement_rejects_noise() -> None:
    """投研相关列判据：认得出有用列，也挡得住运维/密钥列。"""
    for good in ("dv_ratio", "pe_ttm", "total_mv", "turnover_rate",
                 "资产负债率", "净息差"):
        assert is_investable_column(good), f"漏判有用列：{good}"
    for bad in ("user_id", "password_hash", "session_token", "tenant_id",
                "created_at", "col_1", ""):
        assert not is_investable_column(bad), f"误判无关列：{bad}"


def test_skip_rules_cover_known_noise() -> None:
    """跳过规则必须覆盖实测混进来过的噪音（归档库、会话表）。"""
    assert any("archive" in p for p in SKIP_DB_PATTERNS)
    assert "fact_session" in SKIP_TABLE_PREFIXES
    assert "sqlite_" in SKIP_TABLE_PREFIXES


def test_hints_are_non_trivial() -> None:
    """词表不许退化成空/单词（否则整个能力静默失效）。"""
    assert len(INVESTABLE_COLUMN_HINTS) >= 30
    for h in INVESTABLE_COLUMN_HINTS:
        assert str(h).strip(), "词表里有空串"


# ============================================================
# ⑤ 选库契约（dataset_registry / best_for_column）
# ============================================================


@pytest.fixture()
def shadow_project(tmp_path: Path) -> Path:
    """造出**同一列在两个库都有、但只有一个有数据**的现场。

    这是实测现场的形状：`quant_daily_basic.dv_ratio`
    只在 dev 库有 1184 万行，主库**连表都没有**（这里用"表在但空"表达同类）。
    """
    (tmp_path / "data" / "dev").mkdir(parents=True)
    # 主库：**有表、有列，但一行数据都没有**（更"权威"却不可用）
    con = sqlite3.connect(tmp_path / "data" / "moss_finagent.db")
    con.execute("CREATE TABLE quant_daily_basic (trade_date TEXT, code TEXT, "
                "dv_ratio REAL)")
    con.commit()
    con.close()
    # dev 库：有数据（层级更低，但可用）
    con = sqlite3.connect(tmp_path / "data" / "dev" / "moss_dev.db")
    con.execute("CREATE TABLE quant_daily_basic (trade_date TEXT, code TEXT, "
                "dv_ratio REAL)")
    con.executemany("INSERT INTO quant_daily_basic VALUES (?,?,?)",
                    [("20231109", "600036", 4.96),
                     ("20231110", "600036", 5.04)])
    con.commit()
    con.close()
    return tmp_path


def test_best_for_column_prefers_table_with_data_over_authority(
    shadow_project: Path,
) -> None:
    """★★★ 选库判据的第一位是**有没有数据**，不是"哪个库更权威"。

    实测教训：主库（更权威）里 `quant_daily_basic` **表在但空**，
    dev 库里有 1184 万行。若按"主库优先"选 → 取到 0 行 →
    用户又看到"缺数据"。**先看有没有，再看谁权威。**
    """
    idx = ColumnIndex(project_root=shadow_project).build()
    best = idx.best_for_column("dv_ratio")
    assert best is not None
    assert best.db_name == "moss_dev.db", (
        f"选中了 {best.db_name}（权威但空）—— 会让用户看到『缺数据』"
    )
    assert best.has_data


def test_registry_ranks_and_exposes_has_data(shadow_project: Path) -> None:
    """登记视图必须**两者都列出**，并带上 `has_data`，而不是只留一个。"""
    idx = ColumnIndex(project_root=shadow_project).build()
    ranked = idx.dataset_registry()["dv_ratio"]
    assert len(ranked) == 2, "同名表在两个库里都要登记（否则无法解释为何选它）"
    assert ranked[0]["has_data"] is True
    assert ranked[0]["db"] == "moss_dev.db"
    assert ranked[-1]["has_data"] is False
    for row in ranked:
        assert "authority" in row and "row_count" in row


def test_registry_prefers_fresher_when_both_have_data(tmp_path: Path) -> None:
    """两个库**都有数据**时，取**更新**的那个（不是取主库那个）。"""
    (tmp_path / "data" / "dev").mkdir(parents=True)
    for db, d in ((tmp_path / "data" / "moss_finagent.db", "20230101"),
                  (tmp_path / "data" / "dev" / "moss_dev.db", "20260924")):
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE t (trade_date TEXT, dv_ratio REAL)")
        con.execute("INSERT INTO t VALUES (?, ?)", (d, 1.0))
        con.commit()
        con.close()
    idx = ColumnIndex(project_root=tmp_path).build()
    best = idx.best_for_column("dv_ratio")
    assert best is not None
    assert best.latest_time == "20260924", (
        "在两个库都有数据时没取更新的那个 —— 会把旧值当现值用"
    )


def test_best_for_column_returns_none_for_unknown() -> None:
    """查不到的列返回 `None`（**没量到就是没量到**，不编造、不抛错）。"""
    idx = ColumnIndex(db_paths=[]).build()
    assert idx.best_for_column("no_such_column_xyz") is None


def test_registry_and_executor_share_one_ranking(shadow_project) -> None:
    """★ 选库只允许**一份实现**：执行器的解析结果必须等于 `best_for_column`。

    ## 为什么改成行为判据（第一版用 `inspect.getsource` 断言源码字符串）

    第一版断言"执行器源码里必须出现 `best_for_column`"。它在**单进程多模块加载**
    时假红：`inspect.getsource()` 返回了同一个类的**另一个方法**的源码
    （实测：返回的是 `_alias_candidates`）。源码字符串断言本来就脆
    （本项目已登记："判据只认机器可读的标识，不认文本"），
    所以改成**行为等价**：两条路径对同一输入必须给出同一个落点。

    这比源码断言更强：它不管你怎么实现，只要**结果一致**就算过；
    而"两套排序漂移"正是本用例要防的 —— 漂移必然表现为结果不一致。
    """
    from src.infrastructure.catalog.local_data import LocalDataExecutor

    idx = ColumnIndex(project_root=shadow_project).build()
    ex = LocalDataExecutor(index=idx)

    # 执行器解析出的表，必须与统一入口 best_for_column 给出的**同一张**
    table, column, _cands = ex.resolve("dv_ratio")
    best = idx.best_for_column(column)
    assert table is not None and best is not None
    assert table.asset_id == best.asset_id, (
        f"执行器选中 {table.asset_id}，而统一入口选中 {best.asset_id} —— "
        "两套排序已经漂移（本项目已登记过同类缺陷）"
    )
    # 且必须选中**有数据**的那张（而不是更权威但空的那张）
    assert table.db_name == "moss_dev.db"
