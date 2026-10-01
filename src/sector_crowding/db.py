"""板块概念拥挤度：SQL 存储层。

## 表结构（建在**主库** `data/moss_finagent.db`，与做T/量化选股同库）

    sector_crowding_daily   每日每板块一行（UNIQUE(trade_date, sector_code)）
    sector_meta             板块元数据 + 增量刷新的水位线（last_update_date）
    sector_member           板块成分股（参考数据，不参与拥挤度计算）

## 为什么 `sector_meta.last_update_date` 是增量刷新的水位线

手动"一键刷新"要回答的问题是"从哪天开始补"。答案必须是**幂等且可恢复**的：

- 该板块为空 → 从近 6 年起始日全量回填；
- 非空 → 从 `last_update_date` 的**下一个交易日**拉到最新交易日；
- 按 `UNIQUE(trade_date, sector_code)` UPSERT，重复触发不产生重复行；
- 全部成功后才推进 `last_update_date`（否则失败那天的数据永远补不回来）。

最后一条是关键：如果拉取成功但写入失败，或中途异常早退，
水位线**不能**前进 —— 否则下次刷新会从错误的日期开始，那段时间成为永久空洞。

## 成交额口径

板块成交额取 `ths_daily` 的 `vol × avg_price`（同花顺板块指数日线里没有 amount 列，
而这两列的乘积就是成交额）。实测与"成分股成交额求和"同量级，且**不受成分股
历史变更影响**（成分股接口 `ths_member` 只给当前快照，用它回算 6 年历史会有
前视偏差 —— 见 docs/SECTOR_CROWDING.md 的已知限制）。
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from src.sector_crowding.config import (
    SectorCrowdingConfig,
    load_config,
    load_crowding_exclusions,
)

logger = logging.getLogger(__name__)

DAILY_TABLE = "sector_crowding_daily"
META_TABLE = "sector_meta"
MEMBER_TABLE = "sector_member"
WATCH_TABLE = "sector_crowding_watch"
#: 看板清单（总览散点图 + 告警面板共用一份）：visible/pinned/manual 持久化
LIST_TABLE = "sector_crowding_list"
#: 周频异动指标（近5日/1月/2月水位变化 + 1月净流入占比），见 metrics.py
METRIC_TABLE = "sector_crowding_metric"
#: 周频指标的运行台账（判断"本周算过没有"）
METRIC_META_TABLE = "sector_crowding_metric_meta"
#: 自定义告警阈值（每板块一条：高于/低于某个水位就告警）
ALERT_TABLE = "sector_crowding_alert"
# ★★★ 2026-09-27 第八轮：max_ma5 物化表（−1.5s 首屏）
# 原 `max_ma5_map` 每次都 GROUP BY 扫 217 万行（实测 0.4-1.7s）。
# 新增专用表：refresh / recompute 时增量更新；查询 O(N)（按主键扫一次）
MAX_MA5_TABLE = "sector_crowding_max_ma5"

# ======================================================================
# 建表 DDL：**按库拆成两半**（★ `CHG-0143`）
# ======================================================================
#
# ## 为什么必须拆
#
# 拥挤度有两类生命周期完全不同的数据，此前被塞进**同一个库**：
#
#   · **市场参考数据**（无用户维度，三个环境读同一份）→ 共享库 `crowding_shared`
#       `sector_crowding_daily` / `sector_meta` / `sector_member` / `max_ma5`
#   · **用户配置**（每人/每租户一份）→ 本环境库 `app_db`
#       `sector_crowding_list` / `sector_crowding_watch` / `sector_crowding_alert`
#
# 混在一起的后果（实测）：`sector_crowding_list` 只有 **1,226 行、且只在共享
# 遗留主库里**，于是 **dev 与 pilot 共用同一份板块清单与告警阈值** ——
# 开发时点掉的板块，客户那边也消失。
#
# 拆开之后**各库只建自己那一半**，避免在两侧各留一堆空表
# （空表会让"这张表在哪"的探测产生歧义，`platform_data_connector` 的
#  `_table_store()` 就是按"表存在"来选库的）。
_REFERENCE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {DAILY_TABLE} (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date   TEXT    NOT NULL,
    sector_code  TEXT    NOT NULL,
    sector_name  TEXT    NOT NULL DEFAULT '',
    sector_amount REAL,
    market_amount REAL,
    raw_crowding REAL,
    ma5_crowding REAL,
    water_level  REAL,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    UNIQUE(trade_date, sector_code)
);
CREATE INDEX IF NOT EXISTS idx_crowding_sector_date
    ON {DAILY_TABLE}(sector_code, trade_date);
CREATE INDEX IF NOT EXISTS idx_crowding_date
    ON {DAILY_TABLE}(trade_date);

CREATE TABLE IF NOT EXISTS {META_TABLE} (
    sector_code       TEXT PRIMARY KEY,
    sector_name       TEXT NOT NULL DEFAULT '',
    is_concept        INTEGER NOT NULL DEFAULT 1,
    board_type        TEXT NOT NULL DEFAULT '',
    first_trade_date  TEXT NOT NULL DEFAULT '',
    last_update_date  TEXT NOT NULL DEFAULT '',
    bars              INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS {MEMBER_TABLE} (
    sector_code TEXT NOT NULL,
    stock_code  TEXT NOT NULL,
    stock_name  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (sector_code, stock_code)
);

-- ★★★ 2026-09-27 第八轮：max_ma5 物化表（−1.5s 首屏）
-- 替代 `MAX(ma5_crowding) GROUP BY sector_code` 扫 217 万行（实测 0.4-1.7s）。
-- refresh/recompute 完成后增量 UPSERT；查询 = 全表 SELECT（主键已建索引）。
CREATE TABLE IF NOT EXISTS {MAX_MA5_TABLE} (
    sector_code  TEXT PRIMARY KEY,
    max_ma5      REAL NOT NULL,
    updated_at   TEXT NOT NULL
);
"""

#: **用户配置**表 —— 建在**本环境应用库**（`config_db_path`），见上面的说明。
_CONFIG_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {WATCH_TABLE} (
    sector_code TEXT PRIMARY KEY,
    sector_name TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    added_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS {LIST_TABLE} (
    sector_code  TEXT PRIMARY KEY,
    sector_name  TEXT NOT NULL DEFAULT '',
    visible      INTEGER NOT NULL DEFAULT 1,
    pinned       INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL DEFAULT 'manual',
    sort_order   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crowding_list_visible
    ON {LIST_TABLE}(visible, pinned DESC, sort_order, sector_code);

CREATE TABLE IF NOT EXISTS {METRIC_TABLE} (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sector_code       TEXT NOT NULL,
    sector_name       TEXT NOT NULL DEFAULT '',
    --: 指标口径的"本周"键（YYYY-MM-Www）与产出时刻
    compute_week      TEXT NOT NULL,
    computed_at       TEXT NOT NULL,
    --: 各窗口的基准日与变化百分比（chg = 末端水位 / 基准水位 - 1，单位 %）
    base_date_5d      TEXT NOT NULL DEFAULT '',
    chg_5d            REAL,
    base_date_1m      TEXT NOT NULL DEFAULT '',
    chg_1m            REAL,
    base_date_2m      TEXT NOT NULL DEFAULT '',
    chg_2m            REAL,
    --: 近1月资金净流入 / 基准日流通市值（%）
    flow_base_date    TEXT NOT NULL DEFAULT '',
    flow_last_date    TEXT NOT NULL DEFAULT '',
    net_inflow        REAL,
    circ_mv_base      REAL,
    flow_ratio        REAL,
    UNIQUE(sector_code, compute_week)
);
CREATE INDEX IF NOT EXISTS idx_crowding_metric_week
    ON {METRIC_TABLE}(compute_week);

CREATE TABLE IF NOT EXISTS {METRIC_META_TABLE} (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 自定义告警阈值：与"看板清单"分表，因为告警是独立的关注维度 ——
-- 板块可以在清单里可见但不设告警，也可以设了告警而暂时隐藏。
CREATE TABLE IF NOT EXISTS {ALERT_TABLE} (
    sector_code TEXT PRIMARY KEY,
    --: above = 水位高于阈值告警；below = 低于阈值告警
    mode        TEXT NOT NULL DEFAULT 'above',
    --: 阈值 [0, 1]
    threshold   REAL NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

#: 兼容旧引用（`_SCHEMA`）：它是两份的并集。
#: ⚠️ **不要**用它建表（会把配置表也建进共享库、把参考表建进应用库）；
#: 建表请用 `init_tables()`，它按库分别执行上面两份。
_SCHEMA = _REFERENCE_SCHEMA + _CONFIG_SCHEMA

#: `sector_crowding_list.source` 取值：manual = 用户在前端新增；
#: default = 首屏从"全量概念板块"种子化写入（用户删除后置 visible=0）；
#: hidden = 系统自动隐藏的**空壳板块**（`bars=0`，从未刷到过数据）。
#: 与用户手动删除（source 仍是 default/manual、visible=0）区分开，
#: 这样"恢复被系统隐藏的板块"能只挑出该恢复的那批，不会把用户主动删的也放回来。
SOURCE_MANUAL = "manual"
SOURCE_DEFAULT = "default"
SOURCE_HIDDEN = "hidden"

_ADDABLE: dict[str, dict[str, str]] = {
    DAILY_TABLE: {},
    META_TABLE: {
        "board_type": "TEXT NOT NULL DEFAULT ''",
        "bars": "INTEGER NOT NULL DEFAULT 0",
    },
    MEMBER_TABLE: {},
    WATCH_TABLE: {},
    LIST_TABLE: {},
    METRIC_TABLE: {
        # 这一行的资金流用的是哪份成分股名单：`purified`（主线提纯，`relevant=1`）
        # 或 `raw`（`sector_member` 原始名单 / 现场抓取）。没有这一列就无法
        # 回答"提纯名单到底有没有生效"，只能靠日志猜。
        "member_source": "TEXT NOT NULL DEFAULT ''",
    },
    METRIC_META_TABLE: {},
    ALERT_TABLE: {},
}

#: 自定义告警的方向
ALERT_ABOVE = "above"   #: 水位高于阈值 → 告警（拥挤了，风险）
ALERT_BELOW = "below"   #: 水位低于阈值 → 告警（跌到冷清区，可能有机会）
ALERT_MODES = (ALERT_ABOVE, ALERT_BELOW)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ======================================================================
# 连接
# ======================================================================

def assert_writable(store: str = "crowding_shared") -> None:
    """写之前问一句"本实例能写这条存储吗"，不能就 **fail-closed 抛错**。

    ## 为什么必须有它（`CHG-0143`）

    写者闸门 `data_stores.writable_here()` 原来是**从登记派生的**，而
    拥挤度的表**根本没登记** ⇒ 前端的「一键刷新」往共享库里 UPSERT 218 万行
    **不受任何闸门管**。

    而且**光登记还不够**：全仓库只有 `QuantWarehouse` 真正调 `writable_here()`
    做拦截。拥挤度写的是自己的连接，所以要在**自己的写路径**上补这一道。

    ## 为什么不做成"默认拦截"

    拥挤度**读**的是共享参考数据（pilot 必须能读），而
    `refresh.py` / `metrics.py` 是**同一个连接既读又写**。
    无条件拦会把读也挡掉 ⇒ 所以拦的是**调用方显式声明的写意图**
    （`get_db_connection(..., writable=True)`），默认 `False` = 安全侧。
    """
    from src.infrastructure.catalog.data_stores import writable_here

    decision = writable_here(store)
    if not decision.allowed:
        raise PermissionError(
            f"本实例不能写 {store}：{decision.reason}。"
            f"（拥挤度参考数据是**共享**的，写者由 "
            f"configs/data_stores.yaml 的 writer 字段声明；"
            f"要改归属就改那一个字段）")


#: `ATTACH` 用户配置库时用的库别名。
#: SQL 里写 `app_db.sector_crowding_list` —— 与 `data_stores.yaml` 的存储名同名，
#: 便于一眼看出"这张表在哪个库"。
CONFIG_SCHEMA_ALIAS = "app_db"

#: 必须加别名前缀的**用户配置表**（`CHG-0143` 之后它们不在主库了）。
_CONFIG_TABLES: tuple[str, ...] = (
    LIST_TABLE, WATCH_TABLE, ALERT_TABLE,
    METRIC_TABLE, METRIC_META_TABLE,
)


def config_table(table: str) -> str:
    """配置表的**限定名**（`app_db.sector_crowding_list`）。

    给跨库 JOIN 用 —— `query_list_view` / `query_alerts` /
    `query_all_latest_water_level` 等 10 处 SQL 同句引用参考表与配置表，
    而 SQLite 支持 `ATTACH` 后的跨库 JOIN，所以只需给配置表加别名前缀。
    """
    return f"{CONFIG_SCHEMA_ALIAS}.{table}"


def get_db_connection(config: SectorCrowdingConfig | None = None,
                      *, path: str | Path | None = None,
                      writable: bool = False) -> sqlite3.Connection:
    """打开拥挤度库连接（WAL + busy_timeout，与项目其它仓储同范式）。

    为什么每次新建连接而不是长连接：一键刷新在**后台线程**里跑，
    SQLite 连接不能跨线程共享；短连接 + WAL 让"刷新写"与"前端读"互不阻塞。

    ## ★ 用户配置库用 `ATTACH` 并进来（`CHG-0143`）

    `db.py` 里有 **10 处 SQL 同句引用参考表与配置表**
    （`query_list_view` / `query_alerts` / `query_all_latest_water_level` /
    `seed_list` / `hide_dead_boards` / `query_hidden_boards` / …）。
    若拆成两条连接在 Python 里合并，要重写这 10 处查询 + 5 个导出函数，
    其中 `seed_list`（`INSERT ... SELECT` 跨库）与 `hide_dead_boards`
    （`UPDATE ... WHERE EXISTS` 跨库）SQLite **明确不支持**。

    所以沿用项目既有范式（`data_stores.open_readonly()` 同样是 `ATTACH` 多库）：
    主库 = **共享参考数据**，`ATTACH` 上**本环境用户配置库**，
    配置表在 SQL 里写 `app_db.<table>`（见 `config_table()`）。
    这样 10 处 JOIN 全部保持 SQLite 原生执行，Python 侧零合并逻辑。

    `config_db_path` 与 `db_path` 相同（单测的临时库、或主实例布局）时
    **不 ATTACH** —— 那是同一条路径，重复 ATTACH 会报错。

    ## `writable` 默认 `False`（★ 默认值即护栏）

    `writable=True` 时先过 `assert_writable()` —— 非写者实例**当场抛错**，
    而不是"连上去、写到一半被 SQLite 拒绝"。

    ⚠️ **默认必须是 `False`**：读路径（pilot 读共享参考数据）绝不能因此变红，
    而 `refresh.py` / `metrics.py` 是同一个连接既读又写。混淆两者的代价是
    "读也写不了" —— 那正是 `data_stores.py` 里
    「硬拒会让它们当场失去写者」那段注释警告过的形状。

    **新增写调用点必须显式传 `writable=True`**；判据
    `test_refresh_write_paths_declare_writable` 用语法树钉住这一点
    （漏传 = 静默只读，不报错）。
    """
    config = config or load_config()
    # ★ `path=` 是**显式指定单库**（单测的临时库、离线脚本的工作副本）——
    #   此时不派生配置库、也不 ATTACH：调用方要的就是"这一个文件"。
    #   生产路径都不传 `path`，所以这条不影响真实部署。
    explicit_single = path is not None
    target = Path(path) if path is not None else config.db_path
    if writable:
        assert_writable("crowding_shared")
    os.makedirs(target.parent, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")

    # ★ 把本环境的**用户配置库**并进来。
    #
    # ⚠️ **必须无条件 ATTACH，包括"与主库同一个文件"的情形** ——
    # 所有运行时 SQL 都写 `app_db.<table>`（见 `config_table()`），
    # 所以别名 `app_db` 必须存在；否则查询会报
    # `no such table: app_db.sector_crowding_watch`。
    #
    # 同一个文件 ATTACH 两次在 SQLite 里是合法的（两条连接句柄指向同一文件），
    # 也正是**单测临时库**（只有一个文件）与**主实例布局**
    # （`app_db` 与 `crowding_shared` 同指 `data/moss_finagent.db`）的形态。
    # `path=` 显式单库时：配置库跟随该路径，同样满足"别名必须存在"。
    config_target = target if explicit_single else config.config_db_path
    os.makedirs(config_target.parent, exist_ok=True)
    conn.execute("ATTACH DATABASE ? AS " + CONFIG_SCHEMA_ALIAS,
                 (str(config_target),))
    return conn


#: 每个库该建哪些表（`CHG-0143`）—— `init_tables()` 按这张表分别执行。
#: key = `SectorCrowdingConfig` 上取路径的属性名，value = 该库的 DDL。
_SCHEMA_BY_DB: tuple[tuple[str, str], ...] = (
    ("db_path", "_REFERENCE_SCHEMA"),          # 共享参考数据
    ("config_db_path", "_CONFIG_SCHEMA"),      # 本环境用户配置
)


def ensure_config_tables(config: SectorCrowdingConfig | None = None) -> None:
    """只建**本环境用户配置库**那一半（幂等，恒可写）。

    ## 为什么请求路径要用它，而不是 `init_tables()`

    `init_tables()` 会**两个库都建**，其中包括共享参考库的 DDL。
    而 `CHG-0143` 给共享库点名了写者（`writer: dev`）⇒ pilot 上那是**只读**的
    （`decided=True`，fail-closed）。若请求路径（`routes._ensure_tables()`）
    对共享库执行 `CREATE TABLE IF NOT EXISTS`：

      · 表已存在时 SQLite 不写盘，但 `executescript` 仍会开写事务
        ⇒ pilot 上会**报错或挂锁**；
      · 更要命的是这会把"读实例去写共享库"变成一个正常动作 ——
        正是写者闸门要防的事。

    ⇒ 请求路径只负责**自己的库**（`per_env + writer: own`，恒可写）；
    共享参考库的建表交给**写者实例**的 `init_tables()`（刷新前/部署时）。

    ⚠️ **必须显式 `commit()`**（2026-09-30 实测踩到）：`executescript()` 只保证
    "执行前先提交挂起事务"，**它自己不提交** DDL。少了这一句，表只活在当前
    连接的隐式事务里，连接一关就**全部消失** —— 表现是"接口能跑、库里却没有表"，
    而且 `list` 查询会静默走 `app_db` 的空表（`query_list_view` 仍返回 262 行，
    因为它是 `LEFT JOIN META`，**看不出异常**）。
    """
    config = config or load_config()
    target = config.config_db_path
    os.makedirs(target.parent, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=30.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(_CONFIG_SCHEMA)
        _ensure_columns(conn)
        conn.commit()
    finally:
        conn.close()


def init_tables(conn: sqlite3.Connection | None = None,
                config: SectorCrowdingConfig | None = None) -> None:
    """建表 + PRAGMA 补列（幂等）。

    ## ★ 按库分建（`CHG-0143`）

    `conn` 为 `None` 时**两个库都建自己那一半**：
    参考表进共享库、用户配置表进本环境应用库。理由见 `_REFERENCE_SCHEMA`
    上面的说明（混建会让"这张表在哪"的探测产生歧义）。

    ⚠️ 用户配置库那一趟用 `writable=True` 打开 —— 它是 `per_env + writer: own`，
    恒为"本环境私有"，所以过闸门不会有副作用；而共享库那一趟**不**传
    `writable`（默认只读口径），因为建表不该被当成"我要刷数据"。

    `conn` 给定时（调用方自己开好连接，如单测的临时库）**只做补列**，
    不切库 —— 保持既有调用语义不变。
    """
    if conn is not None:
        # 调用方自带连接（单测的临时库、或 `get_db_connection` 之外的手工连接）：
        # 两份 DDL 都建在它上面 —— 保持既有调用语义不变（临时库只有一个文件）。
        conn.executescript(_REFERENCE_SCHEMA)
        conn.executescript(_CONFIG_SCHEMA)
        _ensure_columns(conn)
        return
    config = config or load_config()
    for attr, schema_name in _SCHEMA_BY_DB:
        target = getattr(config, attr)
        own = sqlite3.connect(str(target), timeout=30.0)
        try:
            own.row_factory = sqlite3.Row
            own.execute("PRAGMA journal_mode=WAL")
            own.execute("PRAGMA busy_timeout=30000")
            own.executescript(globals()[schema_name])
            _ensure_columns(own)
            own.commit()
        finally:
            own.close()


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """`PRAGMA` 补列（幂等）—— 只动**这个库里存在**的表，不会凭空建表。"""
    for table, columns in _ADDABLE.items():
        if not columns:
            continue
        existing = {row["name"] for row in conn.execute(
            f"PRAGMA table_info({table})")}
        if not existing:
            continue        # 这张表不属于本库 → 跳过（不越界建表）
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.commit()


# ======================================================================
# 写入
# ======================================================================

def upsert_sector_crowding(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]],
                           *, commit: bool = True) -> int:
    """UPSERT 每日拥挤度（幂等）。返回写入行数。

    `UNIQUE(trade_date, sector_code)` 冲突时更新数值列与 `updated_at`，
    `created_at` 保持不变（保留"这行最早是什么时候写进来的"）。

    `commit=False`：提交交给调用方（批量重算时用）。实测差异极大 ——
    逐板块提交 217 万行要 **656 秒**，整批一次提交降到 **80 秒**
    （每个 commit 都是一次 WAL 落盘）。
    """
    payload = list(rows)
    if not payload:
        return 0
    stamp = _now()
    sql = f"""
    INSERT INTO {DAILY_TABLE}
        (trade_date, sector_code, sector_name, sector_amount, market_amount,
         raw_crowding, ma5_crowding, water_level, created_at, updated_at)
    VALUES (:trade_date, :sector_code, :sector_name, :sector_amount, :market_amount,
            :raw_crowding, :ma5_crowding, :water_level, :created_at, :updated_at)
    ON CONFLICT(trade_date, sector_code) DO UPDATE SET
        sector_name   = excluded.sector_name,
        sector_amount = excluded.sector_amount,
        market_amount = excluded.market_amount,
        raw_crowding  = excluded.raw_crowding,
        ma5_crowding  = excluded.ma5_crowding,
        water_level   = excluded.water_level,
        updated_at    = excluded.updated_at
    """
    prepared = []
    for row in payload:
        prepared.append({
            "trade_date": str(row.get("trade_date") or ""),
            "sector_code": str(row.get("sector_code") or ""),
            "sector_name": str(row.get("sector_name") or ""),
            "sector_amount": _num(row.get("sector_amount")),
            "market_amount": _num(row.get("market_amount")),
            "raw_crowding": _num(row.get("raw_crowding")),
            "ma5_crowding": _num(row.get("ma5_crowding")),
            "water_level": _num(row.get("water_level")),
            "created_at": stamp,
            "updated_at": stamp,
        })
    conn.executemany(sql, prepared)
    if commit:
        conn.commit()
    return len(prepared)


def update_sector_meta(conn: sqlite3.Connection, *, sector_code: str,
                       sector_name: str = "", is_concept: bool = True,
                       board_type: str = "", first_trade_date: str = "",
                       last_update_date: str = "", bars: int | None = None,
                       advance_watermark: bool = True) -> None:
    """写板块元数据。

    `advance_watermark=False` 时**不动** `last_update_date` —— 只有整段成功
    才允许推进水位线（中途失败还推进会让那段日期成为永久空洞）。
    """
    stamp = _now()
    code = str(sector_code).strip()
    if not code:
        return
    conn.execute(
        f"""
        INSERT INTO {META_TABLE}
            (sector_code, sector_name, is_concept, board_type, first_trade_date,
             last_update_date, bars, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sector_code) DO UPDATE SET
            sector_name = CASE WHEN excluded.sector_name <> ''
                               THEN excluded.sector_name
                               ELSE {META_TABLE}.sector_name END,
            is_concept  = excluded.is_concept,
            board_type  = CASE WHEN excluded.board_type <> ''
                               THEN excluded.board_type
                               ELSE {META_TABLE}.board_type END,
            first_trade_date = CASE WHEN excluded.first_trade_date <> ''
                                    THEN excluded.first_trade_date
                                    ELSE {META_TABLE}.first_trade_date END,
            last_update_date = CASE WHEN ? = 1 AND excluded.last_update_date <> ''
                                    THEN excluded.last_update_date
                                    ELSE {META_TABLE}.last_update_date END,
            bars        = COALESCE(?, {META_TABLE}.bars),
            updated_at  = excluded.updated_at
        """,
        (code, str(sector_name or ""), 1 if is_concept else 0, str(board_type or ""),
         str(first_trade_date or ""), str(last_update_date or ""),
         int(bars) if bars is not None else 0, stamp,
         1 if advance_watermark else 0,
         int(bars) if bars is not None else None),
    )
    conn.commit()


def upsert_members(conn: sqlite3.Connection, sector_code: str,
                   members: Iterable[dict[str, Any]]) -> int:
    """覆盖写板块成分股（参考数据）。返回写入条数。"""
    rows = [(str(sector_code), str(item.get("code") or ""),
             str(item.get("name") or ""), _now())
            for item in members if str(item.get("code") or "").strip()]
    if not rows:
        return 0
    conn.executemany(
        f"INSERT OR REPLACE INTO {MEMBER_TABLE}"
        "(sector_code, stock_code, stock_name, updated_at) VALUES (?,?,?,?)", rows)
    conn.commit()
    return len(rows)


# ======================================================================
# 读取
# ======================================================================

def get_last_update_date(conn: sqlite3.Connection, sector_code: str) -> str:
    """该板块已入库到的最后交易日（'' = 从未入库 → 需要全量回填）。"""
    row = conn.execute(
        f"SELECT last_update_date FROM {META_TABLE} WHERE sector_code = ?",
        (str(sector_code),)).fetchone()
    return str(row["last_update_date"] or "") if row else ""


# ======================================================================
# 前端展示用的**瘦身投影**（★ 2026-09-30，`CHG-0137`）
# ======================================================================
#
# ## 为什么需要它（用户报障原文）
#
# > 「板块拥挤度 里，打开单个概念的历史拥挤度数据的图，十**几秒才出数据**，
# >   看下什么原因，加载这么慢」
#
# 实测（1268 根日线，`data/moss_finagent.db` 2.4 GB）：
#
#     后端全链路（meta 5.9ms + 行情 3.3ms + 组装 0.2ms）   ≈ 13 ms
#     明文 424 KB / gzip 66 KB
#     @51 KB/s 隧道 1.3~1.5 s；@4.6 KB/s 劣化档 **14.1~16.1 s**
#
# **SQLite 侧没有可优化项**（索引已存在且被命中）—— 慢的是**传字节**。
# 所以这里做两件事，且**只对展示路径生效**（口径见 `docs/PRD.md` §22）。
#
# ## ① 字段白名单：不发前端从不读的列
#
# 原先 `SELECT *` 把 11 列全发出去，而前端 `CrowdingRow`
# （`web/src/sectorCrowdingApi.ts`）只消费 8 个。实测白传：
#
#     created_at 51,988 B (12.5%) + updated_at 51,988 B (12.5%) + id 16,491 B (4.0%)
#     = 120,467 B = **28.9% 的明文**
#
# 其中两个时间戳在 1268 行里**逐字相同**（同一批刷新的产物），纯冗余。
#
# ## ② 浮点按**显示精度**取整：这是收益最大的一步（反直觉）
#
# 前端图表只显示到 3 位小数，原先却按完整 double 序列化
# （`0.00024339722008355307`，19 位；实测 **2348 个值带 18~20 位小数**）。
#
# ⚠️ **单看"去字段"只降 9%** —— 因为 gzip 本来就能压掉重复值。
# 真正让体积掉下来的是**取整让 gzip 重新变得有效**：
#
#     gzip 66,507 B  →  33,491 B（**砍半**）
#     @4.6 KB/s      →  14.1 s  →  7.1 s
#
# 所以精度**既是体积问题、也是压缩率问题**：高熵的随机尾数不可压缩。
#
# ## ⚠️ 为什么是 `slim=` 参数而不是直接改函数
#
# 本函数有**两个内部调用方**，都**依赖完整列**：
#   * `refresh.py:317` `recompute_stored_water_levels()` —— 重算历史水位
#   * `refresh.py:449` `_refresh_one_sector()` —— 拉完整历史再算水位
# 直接改这里会**静默打断水位重算**（那是最难发现的一类故障）。
# 默认 `False` ⇒ **默认值即护栏**：新调用方不传就是安全的一侧，
# 只有明确知道自己在做"展示"的接口才 opt-in。

#: 展示路径真正下发的列。与前端 `CrowdingRow` 类型定义**一一对应** ——
#: 改这里必须同步改 `web/src/sectorCrowdingApi.ts`，否则前端会读到 undefined。
SLIM_COLUMNS: tuple[str, ...] = (
    "trade_date",
    "sector_code",
    "sector_name",
    "sector_amount",
    "market_amount",
    "raw_crowding",
    "ma5_crowding",
    "water_level",
)

#: 各列的**下发精度**（小数位）。口径：够画图与读数即可，不保留原始尾数。
#:
#: * `water_level` 3 位 —— 前端就显示成 `86.3%`（3 位已超出显示精度）；
#: * `raw_crowding` / `ma5_crowding` 6 位 —— 量级 1e-4，6 位有效数字足够
#:   区分相邻曲线（原先是 19~20 位）；
#: * `sector_amount` / `market_amount` 1 位 —— 单位是元，量级 1e8~1e12，
#:   小数部分对图与读数都没有意义。
SLIM_PRECISION: dict[str, int] = {
    "sector_amount": 1,
    "market_amount": 1,
    "raw_crowding": 6,
    "ma5_crowding": 6,
    "water_level": 3,
}


def _slim_row(row: Any) -> dict[str, Any]:
    """把一行完整记录裁成"展示用"投影 + 按精度取整（纯函数）。

    `None` 原样保留 —— 水位为 `None` 是**有语义的**（"数据不足"），
    取整不能把它变成 0，否则界面会把"不知道"画成"不拥挤"。
    """
    out: dict[str, Any] = {}
    for key in SLIM_COLUMNS:
        value = row[key]
        digits = SLIM_PRECISION.get(key)
        if digits is not None and isinstance(value, float):
            value = round(value, digits)
        out[key] = value
    return out


def query_sector_crowding(conn: sqlite3.Connection, sector_code: str, *,
                          start_date: str = "", end_date: str = "",
                          slim: bool = False) -> list[dict[str, Any]]:
    """单板块历史序列（按交易日升序）。

    `slim=True` → **展示用瘦身投影**（`SLIM_COLUMNS` + `SLIM_PRECISION`），
    响应体积 gzip 后减半。**只给前端展示接口用**；算水位必须用默认的完整行
    —— 理由与实测见本节顶部注释与 `docs/PRD.md` §22。
    """
    columns = ", ".join(SLIM_COLUMNS) if slim else "*"
    sql = f"SELECT {columns} FROM {DAILY_TABLE} WHERE sector_code = ?"
    params: list[Any] = [str(sector_code)]
    if start_date:
        sql += " AND trade_date >= ?"
        params.append(str(start_date))
    if end_date:
        sql += " AND trade_date <= ?"
        params.append(str(end_date))
    sql += " ORDER BY trade_date"
    rows = conn.execute(sql, params)
    if slim:
        return [_slim_row(row) for row in rows]
    return [dict(row) for row in rows]


def query_all_latest_water_level(conn: sqlite3.Connection, *,
                                 concepts_only: bool = False,
                                 trade_date: str = "",
                                 sector_codes: list[str] | None = None
                                 ) -> list[dict[str, Any]]:
    """全板块**最新交易日**的水位（前端散点总览用）。

    `trade_date` 留空时取全库最大交易日 —— 不能让每个板块各取自己的最新日：
    停牌/退市的板块最新日会更早，混在一起画散点会出现"今天的图里混着上周的点"。

    `sector_codes` 非 None 时只返回这些板块（前端持久化清单的过滤口径）。

    ⚠️ 这里同样过滤板块黑名单（2026-09-22 补）。前端"持久化清单"若来自
    `list_visible_codes` 本已不含被剔板块，但**只要有一个调用方传的是全量
    代码**（或前端自己在缓存里留着旧清单），被剔板块就会重新出现在散点图上。
    读取侧兜住这一层，与 `query_metrics` 同一个理由。
    """
    target = str(trade_date or "").strip()
    if not target:
        row = conn.execute(
            f"SELECT MAX(trade_date) AS d FROM {DAILY_TABLE}").fetchone()
        target = str(row["d"] or "") if row else ""
    if not target:
        return []
    sql = f"""
    SELECT d.sector_code, d.sector_name, d.trade_date, d.sector_amount,
           d.market_amount, d.raw_crowding, d.ma5_crowding, d.water_level,
           COALESCE(m.is_concept, 1) AS is_concept, m.bars,
           COALESCE(l.pinned, 0) AS pinned
    FROM {DAILY_TABLE} d
    LEFT JOIN {META_TABLE} m ON m.sector_code = d.sector_code
    LEFT JOIN {config_table(LIST_TABLE)} l ON l.sector_code = d.sector_code
    WHERE d.trade_date = ?
    """
    params: list[Any] = [target]
    if concepts_only:
        sql += " AND COALESCE(m.is_concept, 1) = 1"
    if sector_codes is not None:
        if not sector_codes:
            return []
        sql += f" AND d.sector_code IN ({','.join('?' * len(sector_codes))})"
        params.extend(str(code) for code in sector_codes)
    sql += " ORDER BY COALESCE(l.pinned, 0) DESC, d.water_level IS NULL, d.water_level DESC"
    blocked = _sector_blacklist()
    return [dict(row) for row in conn.execute(sql, params)
            if str(row["sector_code"]) not in blocked]


def query_alerts(conn: sqlite3.Connection, *, threshold: float = 0.8,
                 concepts_only: bool = True,
                 trade_date: str = "",
                 sector_codes: list[str] | None = None) -> list[dict[str, Any]]:
    """水位 ≥ 阈值的板块（按水位降序）。

    水位为 NULL（数据不足）的**不参与告警** —— "不知道"和"不拥挤"是两件事，
    把 NULL 当成 0 或当成触发都是错的。

    `sector_codes` 非 None 时只在这些板块里判定告警（与散点图同一份清单）。
    """
    target = str(trade_date or "").strip()
    if not target:
        row = conn.execute(
            f"SELECT MAX(trade_date) AS d FROM {DAILY_TABLE}").fetchone()
        target = str(row["d"] or "") if row else ""
    if not target:
        return []
    sql = f"""
    SELECT d.sector_code, d.sector_name, d.trade_date, d.sector_amount,
           d.market_amount, d.raw_crowding, d.ma5_crowding, d.water_level,
           COALESCE(m.is_concept, 1) AS is_concept,
           COALESCE(l.pinned, 0) AS pinned, m.bars,
           (SELECT MAX(x.ma5_crowding) FROM {DAILY_TABLE} x
             WHERE x.sector_code = d.sector_code) AS max_ma5_crowding
    FROM {DAILY_TABLE} d
    LEFT JOIN {META_TABLE} m ON m.sector_code = d.sector_code
    LEFT JOIN {config_table(LIST_TABLE)} l ON l.sector_code = d.sector_code
    WHERE d.trade_date = ? AND d.water_level IS NOT NULL AND d.water_level >= ?
    """
    params: list[Any] = [target, float(threshold)]
    if concepts_only:
        sql += " AND COALESCE(m.is_concept, 1) = 1"
    if sector_codes is not None:
        if not sector_codes:
            return []
        sql += f" AND d.sector_code IN ({','.join('?' * len(sector_codes))})"
        params.extend(str(code) for code in sector_codes)
    sql += (" ORDER BY COALESCE(l.pinned, 0) DESC, "
            "d.water_level IS NULL, d.water_level DESC")
    return [dict(row) for row in conn.execute(sql, params)]


def query_sector_meta(conn: sqlite3.Connection, *,
                      concepts_only: bool = False,
                      sector_code: str = "") -> list[dict[str, Any]]:
    """板块元数据。

    `sector_code` 非空时**按主键直查一行** —— 详情接口原先读整表（2517 行）
    再在 Python 里线性查找，纯属浪费（`CHG-0137`）。返回仍是列表，
    保持既有调用方的形态不变。
    """
    sql = f"SELECT * FROM {META_TABLE}"
    params: list[Any] = []
    if sector_code:
        sql += " WHERE sector_code = ?"
        params.append(str(sector_code))
        if concepts_only:
            sql += " AND is_concept = 1"
    elif concepts_only:
        sql += " WHERE is_concept = 1"
    sql += " ORDER BY sector_code"
    return [dict(row) for row in conn.execute(sql, params)]


# ======================================================================
# 自选池
# ======================================================================

def list_watchlist(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """自选池板块（带上各自最新水位，前端一屏就能看全）。"""
    latest = latest_trade_date(conn)
    rows = conn.execute(
        f"SELECT w.sector_code, w.sector_name, w.note, w.added_at, "
        f"       d.water_level, d.ma5_crowding, d.raw_crowding, "
        f"       d.sector_amount, d.market_amount, d.trade_date "
        f"FROM {config_table(WATCH_TABLE)} w "
        f"LEFT JOIN {DAILY_TABLE} d ON d.sector_code = w.sector_code "
        f"     AND d.trade_date = ? "
        f"ORDER BY w.added_at DESC", (latest,)).fetchall()
    return [dict(row) for row in rows]


def add_to_watchlist(conn: sqlite3.Connection, sector_code: str, *,
                     sector_name: str = "", note: str = "") -> bool:
    """加入自选（幂等）。返回 True 表示新增，False 表示已在池里（仍更新名称）。"""
    code = str(sector_code).strip()
    if not code:
        raise ValueError("sector_code 不能为空")
    existed = conn.execute(
        f"SELECT 1 FROM {config_table(WATCH_TABLE)} WHERE sector_code = ?", (code,)).fetchone()
    conn.execute(
        f"INSERT INTO {config_table(WATCH_TABLE)}(sector_code, sector_name, note, added_at) "
        f"VALUES (?,?,?,?) ON CONFLICT(sector_code) DO UPDATE SET "
        # 不写 `表名.列`：UPSERT 里目标表已限定为 `app_db.<table>`，
        # 再拼一次会变成 `app_db.<table>.<col>`（**非法 SQL**）。
        # `DO UPDATE` 的未限定列名本来就解析到目标表。
        f"sector_name = CASE WHEN excluded.sector_name <> '' "
        f"                    THEN excluded.sector_name "
        f"                    ELSE sector_name END, "
        f"note = CASE WHEN excluded.note <> '' THEN excluded.note "
        f"             ELSE note END",
        (code, str(sector_name or ""), str(note or ""), _now()))
    conn.commit()
    return existed is None


def remove_from_watchlist(conn: sqlite3.Connection, sector_code: str) -> bool:
    cursor = conn.execute(f"DELETE FROM {config_table(WATCH_TABLE)} WHERE sector_code = ?",
                          (str(sector_code).strip(),))
    conn.commit()
    return bool(cursor.rowcount)


def watchlist_codes(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute(
        f"SELECT sector_code FROM {config_table(WATCH_TABLE)}")}


# ======================================================================
# 看板清单（总览散点图 + 告警面板共用）
# ======================================================================
#
# 语义：**行存在 = 用户已表态；行不存在 = 从未配置过（默认可见）**。
# 因此"用户删掉全部板块"（全部 visible=0）与"库里一行都没有"是两种状态：
# 前者界面应为空，后者才回落到"默认显示全部概念板块"。
#
# 一旦用户在界面上新增过一个板块，我们会把当前默认可见的板块种子化落库
# （`seed_list`），此后可见性完全由库里决定 —— 这是"上次的增删和置顶配置不变"
# 能成立的前提。

def query_list_view(conn: sqlite3.Connection, *,
                    concepts_only: bool = True) -> list[dict[str, Any]]:
    """清单视图：每个候选板块一行，带 `visible / pinned / missing / in_watchlist`。

    候选集 = `sector_meta` 里的概念板块（或全部）**并集**已经写过配置行的板块。
    成交额/水位取**最新交易日**，与散点总览同口径。

    `visible` 的默认值（"没有配置行"时怎么算）：

    | 板块 | 默认 |
    |---|---|
    | 有配置行 | 用行上的 `visible`（用户的删除/恢复说了算） |
    | 概念 + 有数据 | 可见 |
    | 非概念 + 有数据 | **不可见** —— 从没被种子化过。若默认可见，
      取消勾选"只看概念板块"时会凭空冒出一千多个从未看过的板块 |
    | `bars=0`（空壳） | **不可见** —— 同花顺 865xxx 段那类从未刷到数据的
      空壳，放出来只占位置显示"—" |

    空壳一律默认不可见是**关键**：否则每 60 秒轮询的接口每次都要多传几百行无用数据。
    """
    latest = latest_trade_date(conn)
    # `l.sector_code IS NULL` = 该板块没有配置行（用户从未表态）
    blank_visible = ("CASE WHEN l.sector_code IS NULL THEN "
                     "CASE WHEN COALESCE(m.is_concept, 0) = 1 "
                     "AND COALESCE(m.bars, 0) > 0 THEN 1 ELSE 0 END "
                     "ELSE l.visible END")
    # UNION ALL 的结果集里不能直接用表达式排序（SQLite：`3rd ORDER BY term does
    # not match any column in the result set`），所以整段包一层子查询再排序。
    inner = f"""
    SELECT m.sector_code,
           COALESCE(NULLIF(l.sector_name, ''), m.sector_name, '') AS sector_name,
           m.is_concept, m.board_type, m.bars, m.last_update_date,
           {blank_visible}  AS visible,
           COALESCE(l.pinned, 0)   AS pinned,
           COALESCE(l.source, '')  AS source,
           COALESCE(l.sort_order, 0) AS sort_order,
           CASE WHEN l.sector_code IS NULL THEN 1 ELSE 0 END AS missing,
           CASE WHEN w.sector_code IS NULL THEN 0 ELSE 1 END AS in_watchlist,
           COALESCE(a.mode, '')      AS alert_mode,
           a.threshold               AS alert_threshold,
           d.trade_date, d.sector_amount, d.market_amount,
           d.raw_crowding, d.ma5_crowding, d.water_level
    FROM {META_TABLE} m
    LEFT JOIN {config_table(LIST_TABLE)} l ON l.sector_code = m.sector_code
    LEFT JOIN {config_table(WATCH_TABLE)} w ON w.sector_code = m.sector_code
    LEFT JOIN {config_table(ALERT_TABLE)} a ON a.sector_code = m.sector_code
    LEFT JOIN {DAILY_TABLE} d
           ON d.sector_code = m.sector_code AND d.trade_date = ?
    """
    params: list[Any] = [latest]
    if concepts_only:
        inner += " WHERE COALESCE(m.is_concept, 1) = 1"
    inner += f"""
    UNION ALL
    SELECT l.sector_code,
           COALESCE(NULLIF(l.sector_name, ''), m2.sector_name, l.sector_code),
           COALESCE(m2.is_concept, 1), COALESCE(m2.board_type, ''),
           COALESCE(m2.bars, 0), COALESCE(m2.last_update_date, ''),
           l.visible, l.pinned, l.source, l.sort_order,
           0, CASE WHEN w2.sector_code IS NULL THEN 0 ELSE 1 END,
           COALESCE(a2.mode, ''), a2.threshold,
           d2.trade_date, d2.sector_amount, d2.market_amount,
           d2.raw_crowding, d2.ma5_crowding, d2.water_level
    FROM {config_table(LIST_TABLE)} l
    LEFT JOIN {META_TABLE} m2 ON m2.sector_code = l.sector_code
    LEFT JOIN {config_table(WATCH_TABLE)} w2 ON w2.sector_code = l.sector_code
    LEFT JOIN {config_table(ALERT_TABLE)} a2 ON a2.sector_code = l.sector_code
    LEFT JOIN {DAILY_TABLE} d2
           ON d2.sector_code = l.sector_code AND d2.trade_date = ?
    WHERE m2.sector_code IS NULL
    """
    params.append(latest)
    # ⚠️ 黑名单过滤必须作用在**两个分支上**（2026-09-23 修复，实测踩坑）
    #
    # 只过滤 `sector_meta` 那一段是不够的：删掉清单行之后，**另一段**（清单表里
    # 有、`sector_meta` 里没有的行）仍会把它带出来。反过来也一样。
    # 两段都过滤才是"这个板块在任何情况下都不出现在读结果里"。
    #
    # 更要紧的是下面这个反直觉后果 —— 它正是本轮问题的真正根因：
    #
    #   `blank_visible` 的兜底规则是「**没有配置行** + `is_concept=1` +
    #   `bars>0` → 可见」。而 `sector_meta` 里历史数据**刻意保留**
    #   （不删历史是可逆性的前提），所以对一个"已剔除"的板块：
    #
    #       置 visible=0        → 前端不显示              ✅
    #       把配置行**删掉**     → 兜底规则判它**可见**     ❌ 反而露出来了
    #
    #   实测：删行后 `/config_list` 返回 `visible=true, source='', missing=1`。
    #   也就是说「删得更彻底」这个直觉在这里是**反的**，必须靠黑名单显式挡住，
    #   而不能依赖"没有配置行所以看不见"。
    #
    # 用与 `seed_list` / `query_metrics` / `refresh` 同一份 `_sector_blacklist()`
    # （主线点名批次 ∪ 拥挤度剔除清单），四处口径一致。
    blocked = _sector_blacklist()
    if blocked:
        marks = ",".join("?" * len(blocked))
        inner = (f"SELECT * FROM ({inner}) WHERE sector_code NOT IN ({marks})")
        params.extend(sorted(blocked))
    sql = (f"SELECT * FROM ({inner}) sub "
           f"ORDER BY pinned DESC, visible DESC, "
           f"water_level IS NULL, water_level DESC, sector_code")
    return [dict(row) for row in conn.execute(sql, params)]


def list_visible_codes(conn: sqlite3.Connection, *,
                       concepts_only: bool = True) -> list[str]:
    """当前**可见**的清单板块代码（散点图与告警面板据此过滤）。"""
    return [str(row["sector_code"]) for row in query_list_view(
        conn, concepts_only=concepts_only) if int(row["visible"])]


def upsert_list_item(conn: sqlite3.Connection, sector_code: str, *,
                     sector_name: str = "", visible: bool | None = None,
                     pinned: bool | None = None, source: str = "",
                     sort_order: int | None = None) -> dict[str, Any]:
    """写一条清单配置（幂等）。只更新显式传入的字段。

    返回 `{created, sector_code, visible, pinned}`。
    """
    code = str(sector_code or "").strip()
    if not code:
        raise ValueError("sector_code 不能为空")
    stamp = _now()
    existed = conn.execute(
        f"SELECT 1 FROM {config_table(LIST_TABLE)} WHERE sector_code = ?", (code,)).fetchone()
    conn.execute(
        f"""
        INSERT INTO {config_table(LIST_TABLE)}
            (sector_code, sector_name, visible, pinned, source, sort_order,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sector_code) DO UPDATE SET
            -- 不写 `表名.列`：目标表已限定为 `app_db.<table>`，
            -- 再拼一次会变成 `app_db.<table>.<col>`（**非法 SQL**）。
            -- `DO UPDATE` 的未限定列名本来就解析到目标表。
            sector_name = CASE WHEN excluded.sector_name <> ''
                               THEN excluded.sector_name
                               ELSE sector_name END,
            visible = CASE WHEN ? = 1 THEN excluded.visible
                           ELSE visible END,
            pinned  = CASE WHEN ? = 1 THEN excluded.pinned
                           ELSE pinned END,
            source  = CASE WHEN excluded.source <> '' THEN excluded.source
                           ELSE source END,
            sort_order = CASE WHEN ? = 1 THEN excluded.sort_order
                              ELSE sort_order END,
            updated_at = excluded.updated_at
        """,
        (code, str(sector_name or ""), 1 if (visible is None or visible) else 0,
         1 if pinned else 0, str(source or ""),
         int(sort_order) if sort_order is not None else 0, stamp, stamp,
         1 if visible is not None else 0, 1 if pinned is not None else 0,
         1 if sort_order is not None else 0),
    )
    conn.commit()
    row = conn.execute(
        f"SELECT visible, pinned FROM {config_table(LIST_TABLE)} WHERE sector_code = ?",
        (code,)).fetchone()
    return {"created": existed is None, "sector_code": code,
            "visible": bool(row["visible"]) if row else True,
            "pinned": bool(row["pinned"]) if row else False}


def seed_list(conn: sqlite3.Connection, *, concepts_only: bool = True) -> int:
    """把当前默认可见的板块落库成显式配置行（`visible=1, pinned=0`）。

    只在"从未配置过"时调用一次：一旦落库，用户的删除/置顶就不会被默认值覆盖。
    返回新增行数。

    **空的板块（`bars=0`，从未刷到过数据）不落库** —— 它们放进来只会占位置、
    显示"—"，还会和同名的有效板块撞名（实测 250 个空壳里 183 个是 865xxx 段，
    造成"黄金/猪肉/5G"这类名字在列表里出现 2~3 次）。不落库即"默认不可见"，
    以后真要补数据了也能从「已隐藏板块」恢复。

    ## ⚠️ 必须尊重黑名单（2026-09-23 修复，实测踩坑）

    原来这里没有任何黑名单判断，于是出现一个**把删除操作直接抹掉**的回路：

        prune_concepts 置 `visible=0` → 用户要求"彻底去掉" → 删掉清单行
        → 下一次 `/config_list` 调 `seed_list`
        → 它看到"该板块在 `sector_meta` 里 `bars=1456>0` 且不在清单表里"
        → **重新插入 `visible=1`** → 板块回到前端可见列表

    实测复现：删行后调一次 `seed_list` → `新增 1 行`，目标行数从 0 变 1。
    用户会看到"删了又自己长回来"，而且**没有任何日志**。

    为什么判据是黑名单而不是 `bars`：`bars>0` 是"有没有数据"的事实陈述，
    黑名单才是"用户还要不要它"的意图陈述。两者混用就会让**数据的存在**
    冒充**用户的决定**（与 `apply_pure_pool` 那条"未评估 ≠ 不相关"的教训同型）。
    `sector_meta` 里的历史数据**刻意保留**（不删历史是可逆性的前提），
    所以判据必须在读取侧，不能靠把 `bars` 改小来"修数据"。

    用与 `query_metrics` / `refresh` 同一份 `_sector_blacklist()`
    （主线点名批次 ∪ 拥挤度剔除清单），三处口径一致，不会漂移。
    """
    existing = {str(row[0]) for row in conn.execute(
        f"SELECT sector_code FROM {config_table(LIST_TABLE)}")}
    blocked = _sector_blacklist()
    sql = f"SELECT sector_code, sector_name FROM {META_TABLE} WHERE bars > 0"
    if concepts_only:
        sql += " AND is_concept = 1"
    stamp = _now()
    payload = [(str(row["sector_code"]), str(row["sector_name"] or ""),
                1, 0, SOURCE_DEFAULT, 0, stamp, stamp)
               for row in conn.execute(sql)
               if str(row["sector_code"]) not in existing
               and str(row["sector_code"]) not in blocked]
    if not payload:
        return 0
    conn.executemany(
        f"INSERT INTO {config_table(LIST_TABLE)} (sector_code, sector_name, visible, pinned, "
        f"source, sort_order, created_at, updated_at) "
        f"VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(sector_code) DO NOTHING", payload)
    conn.commit()
    return len(payload)


def hide_dead_boards(conn: sqlite3.Connection, *, force: bool = False) -> int:
    """把**可见的**空壳板块（`bars=0`）软删并标记 `source='hidden'`。

    `force=False`（默认，启动时自动跑一次）只处理从未被标记过的行，所以
    "用户手动恢复过"的板块不会被又自动藏回去 —— 自动清理只做一次，
    之后由用户在界面上决定。`force=True`（接口显式调用）则重新隐藏当前
    所有可见的空壳板块。

    返回本次隐藏的板块数。
    """
    guard = "" if force else f" AND COALESCE(source, '') <> '{SOURCE_HIDDEN}'"
    # 注意：SQLite 的 UPDATE 不接受 `SET 别名.列`（那只在 SELECT 里成立），
    # 所以这里不写 `UPDATE ... AS l SET l.visible=0`，直接写列名。
    cursor = conn.execute(f"""
        UPDATE {config_table(LIST_TABLE)}
           SET visible = 0, source = '{SOURCE_HIDDEN}', updated_at = ?
         WHERE visible = 1
           AND COALESCE(source, '') <> '{SOURCE_MANUAL}'
           AND EXISTS (SELECT 1 FROM {META_TABLE} m
                        WHERE m.sector_code = {config_table(LIST_TABLE)}.sector_code
                          AND m.bars = 0)
           {guard}
    """, (_now(),))
    conn.commit()
    return int(cursor.rowcount or 0)


def has_hideable_dead_boards(conn: sqlite3.Connection, *,
                             force: bool = False) -> bool:
    """是否存在 `hide_dead_boards(force)` 会命中的行（同 WHERE 的 SELECT 版）。

    ★ 2026-09-27：`/config_list`（前端 60s 轮询）每次读取前想知道"要不要
    跑清理 UPDATE"。UPDATE 即使 0 行命中也要开写事务、抢一次写锁 ——
    在后台刷新线程批量写入期间，这就是"读路径藏写"的锁竞争窗口
    （事件告警 500 故障同款）。通常没有候选行 → 跳过 UPDATE，纯读。
    """
    guard = "" if force else f" AND COALESCE(source, '') <> '{SOURCE_HIDDEN}'"
    row = conn.execute(f"""
        SELECT 1 FROM {config_table(LIST_TABLE)}
         WHERE visible = 1
           AND COALESCE(source, '') <> '{SOURCE_MANUAL}'
           AND EXISTS (SELECT 1 FROM {META_TABLE} m
                        WHERE m.sector_code = {config_table(LIST_TABLE)}.sector_code
                          AND m.bars = 0)
           {guard}
         LIMIT 1
    """).fetchone()
    return row is not None


def query_hidden_boards(conn: sqlite3.Connection, *, keyword: str = "",
                        limit: int = 0) -> list[dict[str, Any]]:
    """被系统隐藏（或用户删除）的板块，供「已隐藏板块」恢复列表用。

    不放进 `/config_list`：那是个每 60 秒轮询的接口，把几百个不可见行一起塞进去
    会让每次轮询都白传一大截数据；这里按需单独取。
    """
    sql = f"""
    SELECT l.sector_code,
           COALESCE(NULLIF(l.sector_name, ''), m.sector_name, l.sector_code)
             AS sector_name,
           COALESCE(l.source, '') AS source,
           l.pinned, l.updated_at,
           COALESCE(m.bars, 0) AS bars,
           COALESCE(m.is_concept, 1) AS is_concept,
           COALESCE(m.last_update_date, '') AS last_update_date,
           (SELECT MAX(d.trade_date) FROM {DAILY_TABLE} d
             WHERE d.sector_code = l.sector_code) AS last_trade_date
      FROM {config_table(LIST_TABLE)} l
      LEFT JOIN {META_TABLE} m ON m.sector_code = l.sector_code
     WHERE l.visible = 0
    """
    params: list[Any] = []
    text = str(keyword or "").strip()
    if text:
        sql += " AND (l.sector_code LIKE ? OR COALESCE(m.sector_name, '') LIKE ?" \
               "      OR l.sector_name LIKE ?)"
        params.extend([f"%{text}%"] * 3)
    sql += " ORDER BY COALESCE(m.bars, 0), l.sector_code"
    if limit > 0:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(row) for row in conn.execute(sql, params)]


def count_hidden_dead(conn: sqlite3.Connection) -> int:
    """被系统标为 hidden 且仍未刷到数据的板块数（界面提示用）。"""
    row = conn.execute(f"""
        SELECT COUNT(*) AS n FROM {config_table(LIST_TABLE)} l
          LEFT JOIN {META_TABLE} m ON m.sector_code = l.sector_code
         WHERE l.visible = 0 AND l.source = '{SOURCE_HIDDEN}'
           AND COALESCE(m.bars, 0) = 0
    """).fetchone()
    return int(row["n"] or 0) if row else 0


def max_ma5_map(conn: sqlite3.Connection) -> dict[str, float]:
    """每个板块的历史最高平滑拥挤度（"近6年最高"那一列）。

    ★★★ 2026-09-27 第八轮：走**物化表** `sector_crowding_max_ma5`（O(N) 主键读）。
    原实现是 GROUP BY 扫 217 万行，实测 0.4-1.7s；改为读物化表 < 5ms。
    物化表由 `rebuild_max_ma5_table()` 在 refresh/recompute 完成后增量更新；
    冷启动（首次跑 / 表为空）回退到 GROUP BY 一次性回填。

    **不要**用它替代 `query_alerts` 里的相关子查询场景：那里只需要几十行，
    而这里要显示整张表的"近6年最高"列时调用。
    """
    # 1. 物化表为空 → 一次性回填（兼容老库；理论上首次启动后表就有数据）
    has_data = conn.execute(
        f"SELECT 1 FROM {MAX_MA5_TABLE} LIMIT 1").fetchone()
    if not has_data:
        rebuild_max_ma5_table(conn)

    rows = conn.execute(
        f"SELECT sector_code, max_ma5 FROM {MAX_MA5_TABLE}").fetchall()
    return {str(row["sector_code"]): float(row["max_ma5"])
            for row in rows if row["max_ma5"] is not None}


def rebuild_max_ma5_table(conn: sqlite3.Connection, *,
                          sector_codes: list[str] | None = None) -> int:
    """重建 max_ma5 物化表。

    Args:
        sector_codes: 只更新这些板块（refresh 时增量）；None = 全量重建。

    Returns:
        写入行数。
    """
    stamp = _now()
    if sector_codes is None:
        # 全量：GROUP BY 一次性扫日线表（只在首次或 recompute 时跑一次）
        rows = conn.execute(
            f"SELECT sector_code, MAX(ma5_crowding) AS mx FROM {DAILY_TABLE} "
            f"WHERE ma5_crowding IS NOT NULL GROUP BY sector_code").fetchall()
        payload = [(str(r["sector_code"]), float(r["mx"]), stamp)
                   for r in rows if r["mx"] is not None]
    else:
        # 增量：按板块代码分别取（少量板块，避免扫全表）
        payload = []
        for code in sector_codes:
            row = conn.execute(
                f"SELECT MAX(ma5_crowding) AS mx FROM {DAILY_TABLE} "
                f"WHERE sector_code = ? AND ma5_crowding IS NOT NULL",
                (code,)).fetchone()
            if row and row["mx"] is not None:
                payload.append((code, float(row["mx"]), stamp))
    if not payload:
        return 0
    conn.executemany(
        f"INSERT INTO {MAX_MA5_TABLE}(sector_code, max_ma5, updated_at) "
        f"VALUES (?, ?, ?) "
        f"ON CONFLICT(sector_code) DO UPDATE SET "
        f"max_ma5 = excluded.max_ma5, updated_at = excluded.updated_at",
        payload)
    conn.commit()
    return len(payload)


def count_list_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {config_table(LIST_TABLE)}").fetchone()[0])


# ======================================================================
# 自定义告警阈值（每板块一条）
# ======================================================================

def alert_triggered(mode: str, threshold: float | None,
                    water: float | None) -> bool:
    """是否触发告警。水位缺失（数据不足）**不算触发** —— "不知道" != "不拥挤"，
    与面板里水位 NULL 不参与默认告警是同一口径。"""
    if threshold is None or water is None:
        return False
    if not math.isfinite(float(water)) or not math.isfinite(float(threshold)):
        return False
    value, cut = float(water), float(threshold)
    if mode == ALERT_BELOW:
        return value < cut
    return value > cut


def upsert_alert(conn: sqlite3.Connection, sector_code: str, *,
                 mode: str, threshold: float, note: str = "") -> dict[str, Any]:
    """设置/更新某板块的告警阈值。

    校验放在这里而不是只放接口层：阈值必须是 `[0, 1]` 的有限数、方向必须是
    已知值 —— 静默写入一个 `NaN` 或越界值会让"触发判定"永远为假，比报错更难查。
    """
    code = str(sector_code or "").strip()
    if not code:
        raise ValueError("sector_code 不能为空")
    direction = str(mode or "").strip().lower()
    if direction not in ALERT_MODES:
        raise ValueError(f"告警方向必须是 {' / '.join(ALERT_MODES)}")
    try:
        value = float(threshold)
    except (TypeError, ValueError) as exc:
        raise ValueError("告警阈值必须是数字") from exc
    if not math.isfinite(value):
        raise ValueError("告警阈值必须是有限数字")
    if not 0.0 <= value <= 1.0:
        raise ValueError("告警阈值必须在 [0, 1] 之间")
    stamp = _now()
    conn.execute(
        f"""
        INSERT INTO {config_table(ALERT_TABLE)}(sector_code, mode, threshold, note,
                                  created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(sector_code) DO UPDATE SET
            mode = excluded.mode, threshold = excluded.threshold,
            note = excluded.note, updated_at = excluded.updated_at
        """, (code, direction, value, str(note or ""), stamp, stamp))
    conn.commit()
    return {"sector_code": code, "mode": direction, "threshold": value}


def delete_alert(conn: sqlite3.Connection, sector_code: str) -> bool:
    cursor = conn.execute(f"DELETE FROM {config_table(ALERT_TABLE)} WHERE sector_code = ?",
                          (str(sector_code or "").strip(),))
    conn.commit()
    return bool(cursor.rowcount)


def query_alerts_config(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """全部自定义告警配置，按 `sector_code` 索引（前端整表渲染用）。"""
    return {str(row["sector_code"]): dict(row)
            for row in conn.execute(f"SELECT * FROM {config_table(ALERT_TABLE)}")}


def count_alerts_config(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        f"SELECT COUNT(*) FROM {config_table(ALERT_TABLE)}").fetchone()[0])


def reset_list(conn: sqlite3.Connection) -> int:
    """清空清单配置 → 回到"默认显示全部板块"。

    **不要**紧接着调 `seed_list()`：清空 + 重新种子化等于把用户的删除/置顶
    全部抹掉，那是"重置"而不是"清空"。清空后靠 `query_list_view` 的
    `COALESCE(l.*, 默认)` 回落即可（新用户本来就该看到全量）。
    """
    cursor = conn.execute(f"DELETE FROM {config_table(LIST_TABLE)}")
    conn.commit()
    return int(cursor.rowcount or 0)


# ======================================================================
# 周频异动指标（metrics.py 产出，前端 4 列）
# ======================================================================

def upsert_metrics(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]],
                   *, commit: bool = True) -> int:
    """写某周的各板块异动指标（幂等，按 `(sector_code, compute_week)` UPSERT）。"""
    payload = []
    for row in rows:
        code = str(row.get("sector_code") or "").strip()
        week = str(row.get("compute_week") or "").strip()
        if not code or not week:
            continue
        payload.append({
            "sector_code": code,
            "sector_name": str(row.get("sector_name") or ""),
            "compute_week": week,
            "computed_at": str(row.get("computed_at") or _now()),
            "base_date_5d": str(row.get("base_date_5d") or ""),
            "chg_5d": _num(row.get("chg_5d")),
            "base_date_1m": str(row.get("base_date_1m") or ""),
            "chg_1m": _num(row.get("chg_1m")),
            "base_date_2m": str(row.get("base_date_2m") or ""),
            "chg_2m": _num(row.get("chg_2m")),
            "flow_base_date": str(row.get("flow_base_date") or ""),
            "flow_last_date": str(row.get("flow_last_date") or ""),
            "net_inflow": _num(row.get("net_inflow")),
            "circ_mv_base": _num(row.get("circ_mv_base")),
            "flow_ratio": _num(row.get("flow_ratio")),
            "member_source": str(row.get("member_source") or ""),
        })
    if not payload:
        return 0
    conn.executemany(
        f"""
        INSERT INTO {config_table(METRIC_TABLE)}
            (sector_code, sector_name, compute_week, computed_at,
             base_date_5d, chg_5d, base_date_1m, chg_1m,
             base_date_2m, chg_2m, flow_base_date, flow_last_date,
             net_inflow, circ_mv_base, flow_ratio, member_source)
        VALUES (:sector_code, :sector_name, :compute_week, :computed_at,
                :base_date_5d, :chg_5d, :base_date_1m, :chg_1m,
                :base_date_2m, :chg_2m, :flow_base_date, :flow_last_date,
                :net_inflow, :circ_mv_base, :flow_ratio, :member_source)
        ON CONFLICT(sector_code, compute_week) DO UPDATE SET
            sector_name    = excluded.sector_name,
            computed_at    = excluded.computed_at,
            base_date_5d   = excluded.base_date_5d,
            chg_5d         = excluded.chg_5d,
            base_date_1m   = excluded.base_date_1m,
            chg_1m         = excluded.chg_1m,
            base_date_2m   = excluded.base_date_2m,
            chg_2m         = excluded.chg_2m,
            flow_base_date = excluded.flow_base_date,
            flow_last_date = excluded.flow_last_date,
            net_inflow     = excluded.net_inflow,
            circ_mv_base   = excluded.circ_mv_base,
            flow_ratio     = excluded.flow_ratio,
            member_source  = excluded.member_source
        """, payload)
    if commit:
        conn.commit()
    return len(payload)


def latest_metric_week(conn: sqlite3.Connection) -> str:
    """库里最新一周的指标键（'' = 从未算过）。"""
    row = conn.execute(
        f"SELECT MAX(compute_week) AS w FROM {config_table(METRIC_TABLE)}").fetchone()
    return str(row["w"] or "") if row else ""


def query_metrics(conn: sqlite3.Connection, *, week: str = ""
                  ) -> dict[str, dict[str, Any]]:
    """某一周的指标，按 `sector_code` 索引（`week` 留空 → 最新一周）。

    返回 {} 表示还没算过 —— 前端此时这几列显示"—"，不会显示 0（"没算"和
    "没变化"必须区分得开，与水位 NULL 的处理口径一致）。

    ## ⚠️ 读取时也要过滤板块黑名单（2026-09-22 补，前端"删不掉"的根因）

    `compute_all_metrics` 的黑名单过滤**只对之后算的周生效**。用户剔除一个板块后，
    **当周已经算好的行还留在 `sector_crowding_metric` 里**，前端读这张表，
    于是板块"删了还在" —— 实测用户为此把同一批板块报了两次。

    这里在**读取**侧过滤，而不是去删行：与 `refresh._drop_blacklisted` 的
    "只挡更新、不删历史"同一口径（历史行留着可回溯），但**展示与检测范围**
    立刻生效，不必等下一周重算。

    黑名单不可用时**不过滤**（与日更、指标计算三处口径一致）：
    把"读不到清单"当成"排除全部"会让前端一片空白。
    """
    target = str(week or "").strip() or latest_metric_week(conn)
    if not target:
        return {}
    rows = conn.execute(
        f"SELECT * FROM {config_table(METRIC_TABLE)} WHERE compute_week = ?", (target,))
    blocked = _sector_blacklist()
    return {str(row["sector_code"]): dict(row) for row in rows
            if str(row["sector_code"]) not in blocked}


def _sector_blacklist() -> frozenset[str]:
    """不显示在拥挤度看板上的板块 = 主线批次剔除 ∪ 拥挤度剔除清单。

    ⚠️ 用两份**用户点名**的清单，而不是整份 `sector_blacklist.yaml`（604 条）：
    后者含大量历史遗留的 `865xxx` / GICS 行业 / 地域板块，而拥挤度模块
    **刻意保留**它们（"以后想看行业拥挤度不用重跑 6 年"）。按整份过滤会把
    最新一周 1878 个板块砍到 1541，多砍的绝大多数是行业指数 —— 过度过滤。

    与 `metrics._sector_blacklist` 故意各写一份：`db.py` 是底层读接口，
    不应反向依赖 `metrics.py`（循环导入）。两份都"任一不可用就只用可用的那份"。
    """
    out: set[str] = set()
    try:
        from src.mainline.config import load_removed_concepts

        loaded = load_removed_concepts()
        if loaded:
            out |= set(loaded)
    except Exception as exc:  # noqa: BLE001 清单坏了不该让前端打不开
        logger.warning("主线批次剔除清单读取失败，本轮不含这部分：%s",
                       type(exc).__name__)
    try:
        extra = load_crowding_exclusions()
        if extra:
            out |= set(extra)
    except Exception as exc:  # noqa: BLE001
        logger.warning("拥挤度剔除清单读取失败，本轮不含这部分：%s",
                       type(exc).__name__)
    return frozenset(out)


def get_metric_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {str(row["key"]): str(row["value"])
            for row in conn.execute(f"SELECT key, value FROM {config_table(METRIC_META_TABLE)}")}


def set_metric_meta(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    stamp = _now()
    conn.executemany(
        f"INSERT INTO {config_table(METRIC_META_TABLE)}(key, value, updated_at) VALUES (?,?,?) "
        f"ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        f"updated_at = excluded.updated_at",
        [(str(k), str(v), stamp) for k, v in values.items()])
    conn.commit()


def reset_metrics(conn: sqlite3.Connection) -> int:
    """删掉全部周频指标（重算前清场用，避免历史周堆积）。"""
    cursor = conn.execute(f"DELETE FROM {config_table(METRIC_TABLE)}")
    conn.commit()
    return int(cursor.rowcount or 0)


def query_sector_members(conn: sqlite3.Connection,
                         sector_code: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        f"SELECT stock_code, stock_name FROM {MEMBER_TABLE} "
        f"WHERE sector_code = ? ORDER BY stock_code", (str(sector_code),))]


def search_sectors(conn: sqlite3.Connection, keyword: str, *,
                   limit: int = 20) -> list[dict[str, Any]]:
    """按名称/代码搜板块（前端"输入板块名称查询"用）。"""
    text = str(keyword or "").strip()
    if not text:
        return []
    rows = conn.execute(
        f"SELECT sector_code, sector_name, is_concept, board_type, bars, "
        f"       last_update_date FROM {META_TABLE} "
        f"WHERE sector_name LIKE ? OR sector_code LIKE ? "
        f"ORDER BY bars DESC, sector_code LIMIT ?",
        (f"%{text}%", f"%{text}%", max(1, int(limit)))).fetchall()
    return [dict(row) for row in rows]


def list_sectors_needing_refresh(conn: sqlite3.Connection, *,
                                 concepts_only: bool = False) -> list[dict[str, Any]]:
    """所有可刷新的板块（含 `last_update_date`，供增量起点计算）。"""
    return query_sector_meta(conn, concepts_only=concepts_only)


def latest_trade_date(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        f"SELECT MAX(trade_date) AS d FROM {DAILY_TABLE}").fetchone()
    return str(row["d"] or "") if row else ""


def count_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {DAILY_TABLE}").fetchone()[0])


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


__all__ = [
    "ALERT_ABOVE",
    "ALERT_BELOW",
    "ALERT_MODES",
    "ALERT_TABLE",
    "DAILY_TABLE",
    "LIST_TABLE",
    "MEMBER_TABLE",
    "META_TABLE",
    "METRIC_META_TABLE",
    "METRIC_TABLE",
    "SOURCE_DEFAULT",
    "SOURCE_HIDDEN",
    "SOURCE_MANUAL",
    "WATCH_TABLE",
    "add_to_watchlist",
    "alert_triggered",
    "count_alerts_config",
    "count_hidden_dead",
    "count_list_rows",
    "count_rows",
    "delete_alert",
    "ensure_config_tables",
    "get_db_connection",
    "get_last_update_date",
    "get_metric_meta",
    "hide_dead_boards",
    "init_tables",
    "latest_metric_week",
    "latest_trade_date",
    "list_sectors_needing_refresh",
    "list_visible_codes",
    "list_watchlist",
    "max_ma5_map",
    "query_alerts",
    "query_alerts_config",
    "query_all_latest_water_level",
    "query_hidden_boards",
    "query_list_view",
    "query_metrics",
    "query_sector_crowding",
    "query_sector_members",
    "query_sector_meta",
    "remove_from_watchlist",
    "reset_list",
    "reset_metrics",
    "search_sectors",
    "seed_list",
    "set_metric_meta",
    "SLIM_COLUMNS",
    "SLIM_PRECISION",
    "update_sector_meta",
    "upsert_alert",
    "upsert_list_item",
    "upsert_members",
    "upsert_metrics",
    "upsert_sector_crowding",
    "watchlist_codes",
]
