"""用户级自选池与个股口径档案（`(tenant, user, code, mode)` 主键）。

对应设计：`docs/PLATFORM_MULTI_TENANCY_DESIGN.md` §7.2.6①（三重配额）、
§7.2.6③（copy-on-write）、§6.1（做T模块的多用户化）。

## 为什么新开一个仓储而不是改 `dim_intraday_profile`

旧表 `dim_intraday_profile` 的主键是 **`code` 一列** —— "一只票全系统一份权重"。
改成 `(tenant_id, user_id, code, mode)` 属于**破坏性迁移**，而线上还在用它。
所以本轮：
  - **新表 `dim_intraday_profile_v2` 与旧表并存**（双写期，§10 回滚策略）；
  - 旧表**不删**，直到验收通过并观察一个完整交易日；
  - 迁移脚本（`scripts/`）按租户把旧行归属给管理员账号。

## copy-on-write：为什么"读的时候不要写"

生效口径按三级回退：**用户档案 → 租户/平台模板 → 内置默认**。
`effective_profile()` **只读不物化** —— 否则 400 用户 × 100 只会在首次访问时
膨胀出 4 万行空档案，而且"系统默认值升级"对已访问过的人**再也不生效**
（他们被钉在了旧默认值上）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

logger = logging.getLogger(__name__)

TABLE_POOL = "dim_user_pool"
TABLE_WATCH = "dim_user_watchlist_v2"
TABLE_PROFILE = "dim_intraday_profile_v2"

#: 建表/补列撞锁时的重试次数与退避基数（秒）。
#: 线上库 6.8 GB 且服务在写，`CREATE TABLE` 可能拿不到写锁；
#: 退避递增（0.5/1.0/1.5…）而不是固定间隔，避免与服务的写事务同步抖动。
_SCHEMA_RETRIES = 5
_SCHEMA_RETRY_WAIT = 0.5

Mode = Literal["intraday", "daily"]

_SCHEMA = f"""
-- 自选池（"池"是实体：每人最多 pool_limit 个）
CREATE TABLE IF NOT EXISTS {TABLE_POOL} (
    pool_id    TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'watch',   -- watch（自选池）| sector（自定义板块）
    sort_order INTEGER NOT NULL DEFAULT 0,
    pinned     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pool_owner
    ON {TABLE_POOL}(tenant_id, user_id, sort_order);

-- 自选条目（挂到池上；同一只票**可属多个池**，但总数按去重算）
CREATE TABLE IF NOT EXISTS {TABLE_WATCH} (
    tenant_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    pool_id    TEXT NOT NULL,
    code       TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    industry   TEXT NOT NULL DEFAULT '',
    boards_json   TEXT NOT NULL DEFAULT '[]',
    overseas_json TEXT NOT NULL DEFAULT '[]',
    peers_json    TEXT NOT NULL DEFAULT '[]',
    note       TEXT NOT NULL DEFAULT '',
    pinned     INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id, pool_id, code)
);
CREATE INDEX IF NOT EXISTS idx_watch_pool ON {TABLE_WATCH}(pool_id);

-- 个股口径档案 v2：★ 主键含 user_id（旧表只有 code）
CREATE TABLE IF NOT EXISTS {TABLE_PROFILE} (
    tenant_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    code       TEXT NOT NULL,
    mode       TEXT NOT NULL DEFAULT 'intraday',
    name       TEXT NOT NULL DEFAULT '',
    weights_json    TEXT NOT NULL DEFAULT '{{}}',
    thresholds_json TEXT NOT NULL DEFAULT '{{}}',
    levels_json     TEXT NOT NULL DEFAULT '{{}}',
    character_json  TEXT NOT NULL DEFAULT '{{}}',
    -- 关联板块 / 海外映射：旧表塞在口径表里，概念上是"这只票是什么"而非
    -- "怎么给它打分"，所以 v2 放同一行但独立列（见迁移脚本的落点表）。
    boards_json     TEXT NOT NULL DEFAULT '[]',
    overseas_json   TEXT NOT NULL DEFAULT '[]',
    template   TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'private',
    source     TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id, code, mode)
);
CREATE INDEX IF NOT EXISTS idx_profile_owner
    ON {TABLE_PROFILE}(tenant_id, user_id, updated_at);

-- 租户/平台模板（`user_id=''` = 平台默认；有 user_id = 租户级默认）
CREATE TABLE IF NOT EXISTS {TABLE_PROFILE}_template (
    tenant_id  TEXT NOT NULL,
    code       TEXT NOT NULL,
    mode       TEXT NOT NULL DEFAULT 'intraday',
    weights_json    TEXT NOT NULL DEFAULT '{{}}',
    thresholds_json TEXT NOT NULL DEFAULT '{{}}',
    levels_json     TEXT NOT NULL DEFAULT '{{}}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, code, mode)
);
"""

# 老库（已建过 v2 早期版本）缺列时补上 —— 与项目既有做法一致
# （`intraday_profile_sqlite_repo._ADDABLE_COLUMNS`、`event_sqlite_base._schema_sync`）。
# 没有它，升级到新版本时 `SELECT *` 会直接报 no such column。
_ADDABLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("name", "TEXT NOT NULL DEFAULT ''"),
    ("boards_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("overseas_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("visibility", "TEXT NOT NULL DEFAULT 'private'"),
    ("character_json", "TEXT NOT NULL DEFAULT '{}'"),
)

# 表名 → 该表的补列清单（两张表都要能升级）
_ADDABLE: dict[str, tuple[tuple[str, str], ...]] = {
    TABLE_PROFILE: _ADDABLE_COLUMNS,
    TABLE_WATCH: (
        ("industry", "TEXT NOT NULL DEFAULT ''"),
        ("pinned", "INTEGER NOT NULL DEFAULT 0"),
        ("peers_json", "TEXT NOT NULL DEFAULT '[]'"),
    ),
    TABLE_POOL: (
        ("pinned", "INTEGER NOT NULL DEFAULT 0"),
        ("kind", "TEXT NOT NULL DEFAULT 'watch'"),
    ),
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _loads(raw: object, default: Any) -> Any:
    """JSON 列解析（脏数据返回默认值并告警，不让一行坏数据打断整表）。"""
    if not raw:
        return default
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        logger.warning("自选/档案表含无法解析的 JSON 列，已按默认值处理：%s",
                       str(raw)[:120])
        return default
    return value


def _dumps(payload: object) -> str:
    return json.dumps(payload if payload is not None else {},
                      ensure_ascii=False, sort_keys=True)


#: 北交所前缀：本项目所有连接器都不支持（`exchange_symbol` 直接拒绝）。
#: 旧实现把这条规则放在 `WatchConfig._tradeable_code` 的 pydantic 校验里。
#: 池子从 YAML 搬进库之后，**校验必须跟着搬** —— 否则用户能加进一只
#: 每次快照都取数失败的票，而失败发生在取数层（看起来像数据源故障）。
_BSE_PREFIXES = ("8", "4", "920")


class PoolValidationError(ValueError):
    """入池校验失败（代码非法 / 配额满）。带机器可读的 `code`。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def normalize_code(value: object) -> str:
    """标的一律收敛为 **6 位 ASCII 数字**串；不合规直接拒。

    ## 为什么**不能**用 `zfill(6)` 兜底（旧实现的做法）

    `zfill` 会把 `"519"` 变成 **`"000519"`** —— 一只与用户本意完全无关的票。
    而且它**加得进去、取得到数、不报错**：用户以为加的是"519"，
    实际盯着的是另一只票的行情。缺位补零不是"容错"，是**静默改语义**。

    ## 为什么用 `str.isdigit()` 也不够

    `"８３０７９９"`（全角）的 `isdigit()` 是 True，`int()` 也认，
    但它的 `startswith("8")` 是 False —— 于是**北交所校验被绕过**，
    而 SQLite 会把它当作一个与 `"830799"` 不同的键存下来。
    所以这里逐字符判 ASCII，而不是用 `isdigit()`。
    """
    code = str(value).strip()
    if len(code) != 6 or any(ch not in "0123456789" for ch in code):
        raise PoolValidationError(
            "bad_code", f"标的代码须为 6 位数字（如 600519）：{value!r}")
    if code.startswith(_BSE_PREFIXES):
        raise PoolValidationError(
            "unsupported_exchange", f"北交所标的暂不支持（数据源未覆盖）：{code}")
    return code


# ======================================================================
# 三重配额（纯函数，可穷举单测）
# ======================================================================

@dataclass(frozen=True)
class PoolQuota:
    """套餐的池级配额（与 `dim_tier` 对应）。"""

    pool_limit: int = 5          # 可建几个池
    pool_size_limit: int = 100   # 单池最多几只
    watchlist_limit: int = 100   # **去重后**总只数上限
    sector_limit: int = 20       # 自定义板块数
    sector_size_limit: int = 200  # 单板块成员上限


@dataclass(frozen=True)
class QuotaCheck:
    ok: bool
    reason: str = ""
    code: str = ""


def check_add_stock(
    *, quota: PoolQuota, pool_id: str, code: str,
    pool_counts: dict[str, int], distinct_codes: set[str],
    pool_kind: str = "watch",
) -> QuotaCheck:
    """加一只票进某个池的**三重校验**（§7.2.6①）。

    三层缺任一条都能被绕过：
      - 只查池数 → 一个池塞 2000 只；
      - 只查单池 → 5 个池各 100 只（VIP 超 5 倍）；
      - 只查总数 → 单池无上限，前端渲染与取数都被拖垮。
    """
    if pool_kind == "sector":
        size_limit, total_limit = quota.sector_size_limit, None
    else:
        size_limit, total_limit = quota.pool_size_limit, quota.watchlist_limit

    # ① 总数上限（**按去重算**：同一只票放多个池不重复计数）
    #
    # ★ 为什么排在单池前面：当两个上限同时触顶时，"自选总数已满"才是
    #   用户真正该处理的事。若先报"单池满"，用户会去清理那个池 ——
    #   清完发现还是加不进去，是一次白跑。
    if total_limit is not None and code not in distinct_codes:
        if len(distinct_codes) >= total_limit:
            return QuotaCheck(
                False,
                f"自选总数上限 {total_limit} 只（去重计，当前 {len(distinct_codes)} 只）",
                "watchlist_full")

    # ② 单池上限
    if pool_counts.get(pool_id, 0) >= size_limit:
        return QuotaCheck(False, f"单池上限 {size_limit} 只", "pool_full")

    return QuotaCheck(True)


def check_new_pool(*, quota: PoolQuota, existing_pools: int,
                   kind: str = "watch") -> QuotaCheck:
    """新建一个池/板块的校验。"""
    limit = quota.sector_limit if kind == "sector" else quota.pool_limit
    if existing_pools >= limit:
        label = "自定义板块" if kind == "sector" else "自选池"
        return QuotaCheck(False, f"{label}数量上限 {limit} 个", "pool_limit")
    return QuotaCheck(True)


# ======================================================================
# 数据对象
# ======================================================================

@dataclass(frozen=True)
class PoolRecord:
    pool_id: str
    tenant_id: str
    user_id: str
    name: str
    kind: str = "watch"
    sort_order: int = 0
    pinned: bool = False
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class WatchRecord:
    tenant_id: str
    user_id: str
    pool_id: str
    code: str
    name: str = ""
    industry: str = ""
    boards: tuple[str, ...] = ()
    overseas: tuple[str, ...] = ()
    peers: tuple[str, ...] = ()
    note: str = ""
    pinned: bool = False
    sort_order: int = 0


@dataclass(frozen=True)
class ProfileRecord:
    """个股口径档案。`from_user=True` 表示**用户自己调过**（而非继承默认）。

    `name` / `boards` / `overseas` 是"这只票是什么"，权重阈值是"怎么给它打分" ——
    两者都在本行，但概念不同：前者可在用户之间共享（同一只票的板块归属客观），
    后者**绝不能**共享。`caliber_key` 只由后者决定，正是这个区分的落点。
    """

    tenant_id: str
    user_id: str
    code: str
    mode: str
    name: str = ""
    weights: dict[str, float] = field(default_factory=dict)
    thresholds: dict[str, float] = field(default_factory=dict)
    levels: dict[str, float] = field(default_factory=dict)
    character: dict[str, Any] = field(default_factory=dict)
    boards: tuple[str, ...] = ()
    overseas: tuple[str, ...] = ()
    template: str = ""
    visibility: str = "private"
    source: str = "manual"
    from_user: bool = False
    updated_at: str = ""

    @property
    def caliber_key(self) -> str:
        """口径指纹（缓存键用）。

        ★ 这是"计算可共享、参数不可共享"的落点：
        同一只票、**口径相同**的两个用户可以共用一次计算；
        权重/阈值不同就必须各算 —— 用指纹而不是 user_id 做键，
        才不会把本该共享的重复计算掉。
        """
        import hashlib

        raw = "|".join([
            self.mode,
            json.dumps(self.weights, sort_keys=True),
            json.dumps(self.thresholds, sort_keys=True),
            json.dumps(self.levels, sort_keys=True),
        ])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ======================================================================
# 仓储
# ======================================================================

class UserPoolSqliteRepository:
    """用户自选池 + 个股口径档案（同步实现 + 异步端口）。"""

    def __init__(self, db_path: str | None = None) -> None:
        # 默认取**本环境**的应用库 —— `AGENTS.md`：默认值即护栏，安全的一侧做成默认。
        # 原先写死 `data/moss_finagent.db`（三档隔离**共用**的遗留主库）：
        # 一次漏传 `db_path` 就让隔离档写到共享库上，而没有任何地方声明过（CHG-0069）。
        from src.infrastructure.catalog.data_stores import default_app_db

        self._db_path = db_path or default_app_db()
        self._synced = False

    # ---------------- 连接与建表 ----------------

    def _connect(self) -> sqlite3.Connection:
        directory = os.path.dirname(self._db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_schema(self) -> None:
        """建表 + 老库补列。

        ★ **必须能扛住与在线服务并发**：本项目的库是**单个 6.8 GB 文件**，
        服务进程正在往里写。`CREATE TABLE` 与 `ALTER TABLE` 都要拿写锁，
        而 SQLite 拿不到锁时**直接抛 `database is locked`**（`timeout` 主要对
        等锁生效，DDL 仍可能失败）。迁移脚本常常是在服务运行中执行的，
        所以这里重试几次再放弃 —— 否则运维看到的是"迁移脚本随机失败，
        重跑一次又好了"，而这类随机失败会让人不敢在生产上执行迁移。
        """
        last: sqlite3.OperationalError | None = None
        for attempt in range(_SCHEMA_RETRIES):
            try:
                with self._connect() as conn:
                    conn.executescript(_SCHEMA)
                    for table, columns in _ADDABLE.items():
                        self._add_missing_columns(conn, table, columns)
                self._synced = True
                return
            except sqlite3.OperationalError as exc:
                text = str(exc).lower()
                if "locked" not in text and "busy" not in text:
                    raise
                last = exc
                wait = _SCHEMA_RETRY_WAIT * (attempt + 1)
                logger.warning("建表/补列遇到锁（第 %d/%d 次），%.1f 秒后重试：%s",
                               attempt + 1, _SCHEMA_RETRIES, wait, exc)
                time.sleep(wait)
        assert last is not None
        raise last

    @staticmethod
    def _add_missing_columns(conn: sqlite3.Connection, table: str,
                             columns: tuple[tuple[str, str], ...]) -> None:
        """老库补列（`CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列）。

        sqlite3 只允许一次 ALTER 加一列，所以逐列判断、逐列执行。
        """
        present = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
        if not present:
            return
        for name, ddl in columns:
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                logger.info("%s 补列：%s（老库升级）", table, name)

    def _ready(self) -> None:
        if not self._synced:
            self.ensure_schema()

    def clear_all(self) -> None:
        """清空数据（**仅测试用**：避免用例间状态污染）。"""
        self.ensure_schema()
        with self._connect() as conn:
            for table in (TABLE_POOL, TABLE_WATCH, TABLE_PROFILE,
                          f"{TABLE_PROFILE}_template"):
                conn.execute(f"DELETE FROM {table}")  # noqa: S608 表名来自本模块常量

    # ---------------- 池 ----------------

    def create_pool(self, *, pool_id: str, tenant_id: str, user_id: str,
                    name: str, kind: str = "watch", sort_order: int = 0,
                    quota: PoolQuota | None = None) -> PoolRecord:
        """建池（含 `pool_limit` 校验）。"""
        self._ready()
        if quota is not None:
            existing = len(self.list_pools(tenant_id, user_id, kind=kind))
            check = check_new_pool(quota=quota, existing_pools=existing, kind=kind)
            if not check.ok:
                # ★ 必须抛 `PoolValidationError`（而不是裸 `ValueError`）：
                #   HTTP 层要按 `exc.code` 决定状态码（`pool_limit` → 429 而不是 500）。
                #   裸 ValueError 会让限额满变成"服务器内部错误"，前端也没法提示。
                raise PoolValidationError(check.code, check.reason)
        now = _now()
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_POOL} "
                f"(pool_id, tenant_id, user_id, name, kind, sort_order, "
                f" created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pool_id, tenant_id, user_id, name, kind, sort_order, now, now))
        return PoolRecord(pool_id=pool_id, tenant_id=tenant_id, user_id=user_id,
                          name=name, kind=kind, sort_order=sort_order,
                          created_at=now, updated_at=now)

    def list_pools(self, tenant_id: str, user_id: str, *,
                   kind: str = "") -> list[PoolRecord]:
        """**必须带 (tenant_id, user_id)** —— 只按 pool_id 查就是越权入口。"""
        self._ready()
        sql = (f"SELECT * FROM {TABLE_POOL} WHERE tenant_id = ? AND user_id = ?")
        params: list[Any] = [tenant_id, user_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        with self._connect() as conn:
            rows = conn.execute(
                sql + " ORDER BY pinned DESC, sort_order ASC, created_at ASC",
                params).fetchall()
        return [PoolRecord(
            pool_id=str(r["pool_id"]), tenant_id=str(r["tenant_id"]),
            user_id=str(r["user_id"]), name=str(r["name"]),
            kind=str(r["kind"]), sort_order=int(r["sort_order"]),
            pinned=bool(r["pinned"]), created_at=str(r["created_at"]),
            updated_at=str(r["updated_at"])) for r in rows]

    # ---------------- 自选条目 ----------------

    def add_stock(self, *, tenant_id: str, user_id: str, pool_id: str,
                  code: str, name: str = "", industry: str = "",
                  boards: object = (), overseas: object = (),
                  peers: object = (), note: str = "", pinned: bool = False,
                  quota: PoolQuota | None = None) -> WatchRecord:
        """加一只票进池。

        两道关卡，顺序刻意如此：
          ① **标的合法性**（6 位数字、非北交所）—— 与配额无关，任何情况下都查；
          ② **配额**（单池 / 去重总数）—— 只有传了 `quota` 才查。

        为什么合法性校验不能被 `quota=None` 跳过：即使某个调用点暂时没接上
        套餐，也不该让一只取不到数的票进池 —— 它的失败会在**取数层**
        暴露成"数据源故障"，排查成本极高。
        """
        self._ready()
        clean_code = normalize_code(code)
        if quota is not None:
            check = check_add_stock(
                quota=quota, pool_id=pool_id, code=clean_code,
                pool_counts=self.pool_counts(tenant_id, user_id),
                distinct_codes=self.distinct_codes(tenant_id, user_id),
                pool_kind=self._pool_kind(tenant_id, user_id, pool_id))
            if not check.ok:
                raise PoolValidationError(check.code, check.reason)
        now = _now()
        record = WatchRecord(
            tenant_id=tenant_id, user_id=user_id, pool_id=pool_id,
            code=clean_code, name=name, industry=industry,
            boards=tuple(str(x).strip() for x in (boards or ()) if str(x).strip()),
            overseas=tuple(str(x).strip() for x in (overseas or ()) if str(x).strip()),
            peers=tuple(str(x).strip() for x in (peers or ()) if str(x).strip()),
            note=note, pinned=pinned)
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_WATCH} "
                f"(tenant_id, user_id, pool_id, code, name, industry, "
                f" boards_json, overseas_json, peers_json, note, pinned, "
                f" created_at, updated_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                f"ON CONFLICT(tenant_id, user_id, pool_id, code) DO UPDATE SET "
                f"name=excluded.name, industry=excluded.industry, "
                f"boards_json=excluded.boards_json, "
                f"overseas_json=excluded.overseas_json, "
                f"peers_json=excluded.peers_json, note=excluded.note, "
                f"pinned=excluded.pinned, updated_at=excluded.updated_at",
                (tenant_id, user_id, pool_id, clean_code, name, industry,
                 _dumps(list(record.boards)), _dumps(list(record.overseas)),
                 _dumps(list(record.peers)), note, int(bool(pinned)), now, now))
        return record

    def add_stocks_bulk(self, *, tenant_id: str, user_id: str, pool_id: str,
                        codes: object, quota: PoolQuota | None = None,
                        names: dict[str, str] | None = None,
                        ) -> tuple[list[WatchRecord], list[str], list[str]]:
        """批量加自选 → `(已加入, 被拒代码, 原因)`。

        ## 为什么必须批量（实测数据，`scripts/_probe_pool_hotpath.py`）

        `add_stock` 单次 ≈ 10 ms，其中 **7.6 ms 是那条 INSERT 的 fsync**，
        连接开销只占 0.1 ms。所以逐条循环加 20 只票 ≈ 200 ms —— 而这正是
        「一键全部加自选」的真实路径（选股结果一键入池最多几十只）。

        本方法把 N 条 INSERT 收进**一个事务**：一次 fsync 覆盖 N 行。

        ## 配额语义：**边加边算**

        不能先照单全收再校验 —— 那样会写进一半才发现超限。
        这里每成功一条就把该代码记进 `seen`，于是：
          - 同批次内的重复代码只算一次去重名额（与单条路径一致）；
          - 一旦某项配额用完，**后续继续被拒但已加入的保留** ——
            这比"全有或全无"更符合预期：用户点"全部加自选"时，
            显然希望能加的都加上，而不是因为第 26 只超限就一只都不加。
        """
        self._ready()
        wanted: list[str] = []
        rejected: list[str] = []
        reasons: list[str] = []
        for raw in (codes or ()):
            try:
                wanted.append(normalize_code(raw))
            except PoolValidationError as exc:
                rejected.append(str(raw).strip())
                reasons.append(f"{raw}: {exc.message}")

        accepted: list[str] = []
        if quota is not None:
            pool_counts = dict(self.pool_counts(tenant_id, user_id))
            # ① 总数：把**已入库**的与**本批已接受**的合起来看去重名额
            seen = set(self.distinct_codes(tenant_id, user_id))
            kind = self._pool_kind(tenant_id, user_id, pool_id)
            size_limit = (quota.sector_size_limit if kind == "sector"
                          else quota.pool_size_limit)
            total_limit = None if kind == "sector" else quota.watchlist_limit
            used_in_pool = pool_counts.get(pool_id, 0)

            for code in wanted:
                # 同批重复：第二次起直接跳过（既不重复占名额，也不重复写）
                if code in accepted:
                    continue
                if total_limit is not None and code not in seen \
                        and len(seen) >= total_limit:
                    rejected.append(code)
                    reasons.append(
                        f"{code}: 自选总数上限 {total_limit} 只"
                        f"（去重计，当前 {len(seen)} 只）")
                    continue
                if used_in_pool >= size_limit:
                    rejected.append(code)
                    reasons.append(f"{code}: 单池上限 {size_limit} 只")
                    continue
                accepted.append(code)
                seen.add(code)          # ★ 边加边算：下一轮就能看到它
                used_in_pool += 1
        else:
            accepted = wanted

        if not accepted:
            return [], rejected, reasons

        now = _now()
        label = names or {}
        with self._connect() as conn:
            # 单事务：N 条 INSERT 只付一次 fsync（这是本方法存在的全部理由）
            conn.executemany(
                f"INSERT INTO {TABLE_WATCH} "
                f"(tenant_id, user_id, pool_id, code, name, created_at, updated_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?) "
                f"ON CONFLICT(tenant_id, user_id, pool_id, code) DO UPDATE SET "
                f"name=excluded.name, updated_at=excluded.updated_at",
                [(tenant_id, user_id, pool_id, code, label.get(code, ""),
                  now, now) for code in accepted])

        rows = self.list_stocks(tenant_id, user_id, pool_id=pool_id)
        by_code = {r.code: r for r in rows}
        return ([by_code[c] for c in accepted if c in by_code],
                rejected, reasons)

    def remove_stock(self, *, tenant_id: str, user_id: str, pool_id: str,
                     code: str) -> bool:
        self._ready()
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM {TABLE_WATCH} WHERE tenant_id = ? AND user_id = ? "
                f"AND pool_id = ? AND code = ?",
                (tenant_id, user_id, pool_id, normalize_code(code)))
            return bool(cursor.rowcount)

    def set_pinned(self, *, tenant_id: str, user_id: str, code: str,
                   pinned: bool) -> int:
        """置顶/取消置顶（**跨该用户所有池**，与旧 YAML 的全局置顶语义一致）。"""
        self._ready()
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE {TABLE_WATCH} SET pinned = ?, updated_at = ? "
                f"WHERE tenant_id = ? AND user_id = ? AND code = ?",
                (int(bool(pinned)), _now(), tenant_id, user_id,
                 normalize_code(code)))
            return int(cursor.rowcount)

    def list_stocks(self, tenant_id: str, user_id: str, *,
                    pool_id: str = "") -> list[WatchRecord]:
        self._ready()
        sql = (f"SELECT * FROM {TABLE_WATCH} WHERE tenant_id = ? AND user_id = ?")
        params: list[Any] = [tenant_id, user_id]
        if pool_id:
            sql += " AND pool_id = ?"
            params.append(pool_id)
        with self._connect() as conn:
            rows = conn.execute(
                sql + " ORDER BY pinned DESC, sort_order ASC, code ASC",
                params).fetchall()
        return [WatchRecord(
            tenant_id=str(r["tenant_id"]), user_id=str(r["user_id"]),
            pool_id=str(r["pool_id"]), code=str(r["code"]),
            name=str(r["name"] or ""), industry=str(r["industry"] or ""),
            boards=tuple(_loads(r["boards_json"], [])),
            overseas=tuple(_loads(r["overseas_json"], [])),
            peers=tuple(_loads(r["peers_json"], [])),
            note=str(r["note"] or ""),
            pinned=bool(r["pinned"]),
            sort_order=int(r["sort_order"])) for r in rows]

    # ---------------- 自选条目：**按 user_id 定位**（2026-10-08，CHG-0227） ----------------
    #
    # 上面那组 `list_stocks/remove_stock/set_pinned` 是 `(tenant_id, user_id)` 双条件，
    # 而这个平台的 `tenant_id` 装的是**套餐等级**（`applied_tier`，见
    # `src/api/session_ctx.py:104`）—— 于是用户升/降一次套餐，他那份自选就"查不到"了
    # （既有隐患，PRD §50.7 缺口 4 已登记）。做T自选是**最显眼的那份用户数据**，
    # 所以它从第一天就走"只按 user_id 定位"：
    #   · 读/删/置顶：WHERE user_id = ?（+ pool_id/code），**不带 tenant**；
    #   · 写：先按 (user_id, pool_id, code) 找已有行 → 有则 UPDATE（**保留原有 tenant_id**），
    #     没有才 INSERT。这样同一个 (user, pool, code) 永远只有一行，
    #     套餐怎么变都不会分叉出第二行。
    # `tenant_id` 仍然写下来（它是"这行是什么时候、在哪个档位建的"的来源登记），
    # 只是不参与过滤。

    @staticmethod
    def _watch_row_to_record(r: Any) -> WatchRecord:
        return WatchRecord(
            tenant_id=str(r["tenant_id"]), user_id=str(r["user_id"]),
            pool_id=str(r["pool_id"]), code=str(r["code"]),
            name=str(r["name"] or ""), industry=str(r["industry"] or ""),
            boards=tuple(_loads(r["boards_json"], [])),
            overseas=tuple(_loads(r["overseas_json"], [])),
            peers=tuple(_loads(r["peers_json"], [])),
            note=str(r["note"] or ""), pinned=bool(r["pinned"]),
            sort_order=int(r["sort_order"]))

    def list_watch_by_owner(self, user_id: str, *,
                            pool_id: str = "") -> list[WatchRecord]:
        """某账号的自选条目（**只按 user_id**；顺序=置顶在前 + 新加的在前）。"""
        self._ready()
        sql = f"SELECT * FROM {TABLE_WATCH} WHERE user_id = ?"
        params: list[Any] = [str(user_id or "")]
        if pool_id:
            sql += " AND pool_id = ?"
            params.append(pool_id)
        with self._connect() as conn:
            rows = conn.execute(
                sql + " ORDER BY pinned DESC, sort_order ASC, code ASC",
                params).fetchall()
        return [self._watch_row_to_record(r) for r in rows]

    def upsert_watch_by_owner(self, *, user_id: str, tenant_id: str,
                              pool_id: str, code: str, name: str = "",
                              industry: str = "", boards: object = (),
                              overseas: object = (), peers: object = (),
                              note: str = "",
                              pinned: bool = False) -> WatchRecord:
        """加/更新一只自选（唯一键语义 = `(user_id, pool_id, code)`）。

        **新条目的 `sort_order` 取当前最小值 − 1** ⇒ 列表里"新加的在最上面"
        （用户口径 2026-09-23；YAML 时代的 `prepend=True` 就是这个语义）。
        """
        self._ready()
        clean_code = normalize_code(code)
        clean_user = str(user_id or "")
        if not clean_user:
            raise ValueError("自选必须绑定账号：user_id 不能为空")
        boards_json = _dumps([str(x).strip() for x in (boards or ()) if str(x).strip()])
        overseas_json = _dumps([str(x).strip() for x in (overseas or ()) if str(x).strip()])
        peers_json = _dumps([str(x).strip() for x in (peers or ()) if str(x).strip()])
        now = _now()
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT tenant_id, sort_order FROM {TABLE_WATCH}"
                " WHERE user_id = ? AND pool_id = ? AND code = ?"
                " ORDER BY updated_at DESC", (clean_user, pool_id, clean_code)
            ).fetchall()
            if rows:
                # 保留最早那一行的 tenant_id；历史分叉（同键多行）顺手收敛掉
                keep_tenant = str(rows[0]["tenant_id"])
                sort_order = int(rows[0]["sort_order"])
                if len(rows) > 1:
                    conn.execute(
                        f"DELETE FROM {TABLE_WATCH} WHERE user_id = ? AND pool_id = ?"
                        " AND code = ? AND tenant_id <> ?",
                        (clean_user, pool_id, clean_code, keep_tenant))
                conn.execute(
                    f"UPDATE {TABLE_WATCH} SET name = ?, industry = ?,"
                    " boards_json = ?, overseas_json = ?, peers_json = ?,"
                    " note = ?, pinned = ?, updated_at = ?"
                    " WHERE user_id = ? AND pool_id = ? AND code = ?",
                    (name, industry, boards_json, overseas_json, peers_json,
                     note, int(bool(pinned)), now, clean_user, pool_id, clean_code))
                return WatchRecord(
                    tenant_id=keep_tenant, user_id=clean_user, pool_id=pool_id,
                    code=clean_code, name=name, industry=industry,
                    boards=tuple(_loads(boards_json, [])),
                    overseas=tuple(_loads(overseas_json, [])),
                    peers=tuple(_loads(peers_json, [])), note=note,
                    pinned=bool(pinned), sort_order=sort_order)
            head = conn.execute(
                f"SELECT MIN(sort_order) AS m FROM {TABLE_WATCH}"
                " WHERE user_id = ? AND pool_id = ?", (clean_user, pool_id)
            ).fetchone()
            sort_order = (int(head["m"]) - 1) if head and head["m"] is not None else 0
            conn.execute(
                f"INSERT INTO {TABLE_WATCH}"
                "(tenant_id, user_id, pool_id, code, name, industry, boards_json,"
                " overseas_json, peers_json, note, pinned, sort_order,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(tenant_id or ""), clean_user, pool_id, clean_code, name,
                 industry, boards_json, overseas_json, peers_json, note,
                 int(bool(pinned)), sort_order, now, now))
        return WatchRecord(
            tenant_id=str(tenant_id or ""), user_id=clean_user, pool_id=pool_id,
            code=clean_code, name=name, industry=industry,
            boards=tuple(_loads(boards_json, [])),
            overseas=tuple(_loads(overseas_json, [])),
            peers=tuple(_loads(peers_json, [])), note=note,
            pinned=bool(pinned), sort_order=sort_order)

    def remove_watch_by_owner(self, *, user_id: str, pool_id: str,
                              code: str) -> bool:
        self._ready()
        with self._connect() as conn:
            cur = conn.execute(
                f"DELETE FROM {TABLE_WATCH}"
                " WHERE user_id = ? AND pool_id = ? AND code = ?",
                (str(user_id or ""), pool_id, normalize_code(code)))
            return bool(cur.rowcount)

    def pin_watch_by_owner(self, *, user_id: str, pool_id: str, code: str,
                           pinned: bool) -> int:
        self._ready()
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE {TABLE_WATCH} SET pinned = ?, updated_at = ?"
                " WHERE user_id = ? AND pool_id = ? AND code = ?",
                (int(bool(pinned)), _now(), str(user_id or ""), pool_id,
                 normalize_code(code)))
            return int(cur.rowcount)

    def owner_watch_counts(self, pool_id: str = "") -> dict[str, int]:
        """每个账号几只自选（**全体并集用的口径**：按 user_id 分组计数）。"""
        self._ready()
        sql = f"SELECT user_id, COUNT(DISTINCT code) AS n FROM {TABLE_WATCH}"
        params: list[Any] = []
        if pool_id:
            sql += " WHERE pool_id = ?"
            params.append(pool_id)
        with self._connect() as conn:
            return {str(r["user_id"]): int(r["n"]) for r in
                    conn.execute(sql + " GROUP BY user_id", params).fetchall()}

    def owner_watch_codes(self, pool_id: str = "") -> list[str]:
        """**全体账号**自选代码的并集（去重排序）。

        后台作业（扫描/预热/报价）要的是"所有人在看的票"—— 它们没有身份，
        也不该只看某一个人的清单。
        """
        self._ready()
        sql = f"SELECT DISTINCT code FROM {TABLE_WATCH}"
        params: list[Any] = []
        if pool_id:
            sql += " WHERE pool_id = ?"
            params.append(pool_id)
        with self._connect() as conn:
            return [str(r["code"]) for r in
                    conn.execute(sql + " ORDER BY code", params).fetchall()]

    def rename_pool(self, tenant_id: str, user_id: str, pool_id: str,
                    name: str) -> bool:
        """重命名池。

        **WHERE 必须带 `tenant_id + user_id`**：只按 `pool_id` 更新就是越权入口
        —— `pool_id` 会出现在前端 URL 里，用户看到别人的 id 毫不奇怪。
        """
        self._ready()
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE {TABLE_POOL} SET name = ?, updated_at = ? "
                f"WHERE pool_id = ? AND tenant_id = ? AND user_id = ?",
                (str(name).strip(), _now(), pool_id, tenant_id, user_id))
            return bool(cur.rowcount)

    def delete_pool(self, tenant_id: str, user_id: str, pool_id: str) -> int:
        """删池，返回被一并删掉的条目数。

        条目没有 CASCADE（SQLite 默认不开外键），所以**必须手动删** ——
        否则会留下"孤儿条目"：池不见了，但那些票仍占着去重总数，
        用户会发现"我删了池，额度却没还回来"。
        """
        self._ready()
        with self._connect() as conn:
            cur = conn.execute(
                f"DELETE FROM {TABLE_WATCH} WHERE pool_id = ? "
                f"AND tenant_id = ? AND user_id = ?",
                (pool_id, tenant_id, user_id))
            removed = int(cur.rowcount)
            conn.execute(
                f"DELETE FROM {TABLE_POOL} WHERE pool_id = ? "
                f"AND tenant_id = ? AND user_id = ?",
                (pool_id, tenant_id, user_id))
        return removed

    def pool_counts(self, tenant_id: str, user_id: str) -> dict[str, int]:
        """每个池的条目数（单池校验用）。"""
        self._ready()
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT pool_id, COUNT(*) AS n FROM {TABLE_WATCH} "
                f"WHERE tenant_id = ? AND user_id = ? GROUP BY pool_id",
                (tenant_id, user_id)).fetchall()
        return {str(r["pool_id"]): int(r["n"]) for r in rows}

    def distinct_codes(self, tenant_id: str, user_id: str) -> set[str]:
        """**去重后**的票集合（总数校验用）。

        为什么按去重算：同一只票放进 5 个池不该算 5 只 ——
        数据侧确实只需要取一次（§7.2.6① 的说明）。
        普通池与板块池要分开统计（板块不进实时宇宙）。
        """
        self._ready()
        watch_pools = {p.pool_id for p in
                       self.list_pools(tenant_id, user_id, kind="watch")}
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT code, pool_id FROM {TABLE_WATCH} "
                f"WHERE tenant_id = ? AND user_id = ?",
                (tenant_id, user_id)).fetchall()
        return {str(r["code"]) for r in rows if str(r["pool_id"]) in watch_pools}

    def _pool_kind(self, tenant_id: str, user_id: str, pool_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT kind FROM {TABLE_POOL} WHERE tenant_id = ? AND "
                f"user_id = ? AND pool_id = ?",
                (tenant_id, user_id, pool_id)).fetchone()
        return "watch" if row is None else str(row["kind"])

    # ---------------- 个股口径档案（copy-on-write） ----------------

    def effective_profile(self, *, tenant_id: str, user_id: str, code: str,
                          mode: str = "intraday") -> ProfileRecord:
        """生效口径：**用户档案 → 租户模板 → 空**（三级回退，**只读不写**）。

        ★ 三级顺序就是需求"有自定义就用，没有就用系统默认"的落地。
        回退结果带 `from_user=False`，让调用方与前端能一眼看出
        "这只票他是调的，还是跟着系统默认走的"。
        """
        self._ready()
        clean_code = normalize_code(code)

        own = self._get_profile_sync(tenant_id, user_id, clean_code, mode)
        if own is not None:
            return own

        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {TABLE_PROFILE}_template "
                f"WHERE tenant_id = ? AND code = ? AND mode = ?",
                (tenant_id, clean_code, mode)).fetchone()
        if row is not None:
            return ProfileRecord(
                tenant_id=tenant_id, user_id=user_id, code=clean_code, mode=mode,
                weights=_loads(row["weights_json"], {}),
                thresholds=_loads(row["thresholds_json"], {}),
                levels=_loads(row["levels_json"], {}),
                from_user=False, source="tenant_template",
                updated_at=str(row["updated_at"]))

        # ① 都取不到时用**内置默认**（由调用方 `DEFAULT_WEIGHTS` 兜底），
        #    这里返回"空口径"而不是伪造一份 —— 空 = "没配过，用全局"
        return ProfileRecord(tenant_id=tenant_id, user_id=user_id,
                             code=clean_code, mode=mode, from_user=False,
                             source="default")

    def _get_profile_sync(self, tenant_id: str, user_id: str, code: str,
                          mode: str) -> ProfileRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {TABLE_PROFILE} WHERE tenant_id = ? AND "
                f"user_id = ? AND code = ? AND mode = ?",
                (tenant_id, user_id, code, mode)).fetchone()
        if row is None:
            return None
        return self._row_to_profile(row)

    @staticmethod
    def _row_to_profile(row: sqlite3.Row) -> ProfileRecord:
        return ProfileRecord(
            tenant_id=str(row["tenant_id"]), user_id=str(row["user_id"]),
            code=str(row["code"]), mode=str(row["mode"]),
            name=str(row["name"] or ""),
            weights=_loads(row["weights_json"], {}),
            thresholds=_loads(row["thresholds_json"], {}),
            levels=_loads(row["levels_json"], {}),
            character=_loads(row["character_json"], {}),
            boards=tuple(_loads(row["boards_json"], [])),
            overseas=tuple(_loads(row["overseas_json"], [])),
            template=str(row["template"] or ""),
            visibility=str(row["visibility"] or "private"),
            source=str(row["source"] or "manual"),
            from_user=True, updated_at=str(row["updated_at"]))

    def save_profile(self, *, tenant_id: str, user_id: str, code: str,
                     weights: dict[str, float] | None = None,
                     thresholds: dict[str, float] | None = None,
                     levels: dict[str, float] | None = None,
                     character: dict[str, Any] | None = None,
                     mode: str = "intraday", template: str = "",
                     visibility: str = "private", name: str = "",
                     boards: object = (), overseas: object = ()) -> ProfileRecord:
        """**只在用户真正改动时**才写库（copy-on-write 的物化点）。

        注意 `user_id` 是必传且进主键 —— 这正是旧表 `code` 单主键的错误所在。
        """
        self._ready()
        if not str(user_id or "").strip():
            raise ValueError("user_id 必填：档案必须绑定到具体用户")
        clean_code = normalize_code(code)
        now = _now()
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_PROFILE} "
                f"(tenant_id, user_id, code, mode, name, weights_json, "
                f" thresholds_json, levels_json, character_json, boards_json, "
                f" overseas_json, template, visibility, source, "
                f" created_at, updated_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?) "
                f"ON CONFLICT(tenant_id, user_id, code, mode) DO UPDATE SET "
                f"name=excluded.name, "
                f"weights_json=excluded.weights_json, "
                f"thresholds_json=excluded.thresholds_json, "
                f"levels_json=excluded.levels_json, "
                f"character_json=excluded.character_json, "
                f"boards_json=excluded.boards_json, "
                f"overseas_json=excluded.overseas_json, "
                f"template=excluded.template, visibility=excluded.visibility, "
                f"updated_at=excluded.updated_at",
                (tenant_id, user_id, clean_code, mode, name,
                 _dumps(weights), _dumps(thresholds), _dumps(levels),
                 _dumps(character), _dumps(list(boards or ())),
                 _dumps(list(overseas or ())), template, visibility, now, now))
        return self.effective_profile(tenant_id=tenant_id, user_id=user_id,
                                      code=clean_code, mode=mode)

    def delete_profile(self, *, tenant_id: str, user_id: str, code: str,
                       mode: str = "intraday") -> bool:
        """删除用户自己的档案 → 自动回退到系统默认（copy-on-write 的"还原"）。"""
        self._ready()
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM {TABLE_PROFILE} WHERE tenant_id = ? AND "
                f"user_id = ? AND code = ? AND mode = ?",
                (tenant_id, user_id, normalize_code(code), mode))
            return bool(cursor.rowcount)

    def set_template(self, *, tenant_id: str, code: str,
                     weights: dict[str, float] | None = None,
                     thresholds: dict[str, float] | None = None,
                     levels: dict[str, float] | None = None,
                     mode: str = "intraday") -> None:
        """设租户/平台模板（第二级回退）。**不影响**已有用户档案。"""
        self._ready()
        now = _now()
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_PROFILE}_template "
                f"(tenant_id, code, mode, weights_json, thresholds_json, "
                f" levels_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                f"ON CONFLICT(tenant_id, code, mode) DO UPDATE SET "
                f"weights_json=excluded.weights_json, "
                f"thresholds_json=excluded.thresholds_json, "
                f"levels_json=excluded.levels_json, "
                f"updated_at=excluded.updated_at",
                (tenant_id, normalize_code(code), mode, _dumps(weights),
                 _dumps(thresholds), _dumps(levels), now))

    def list_profiles(self, tenant_id: str, user_id: str, *,
                      mode: str = "") -> list[ProfileRecord]:
        """**只列该用户自己调过的**（不含继承项）。"""
        self._ready()
        sql = f"SELECT * FROM {TABLE_PROFILE} WHERE tenant_id = ? AND user_id = ?"
        params: list[Any] = [tenant_id, user_id]
        if mode:
            sql += " AND mode = ?"
            params.append(mode)
        with self._connect() as conn:
            rows = conn.execute(
                sql + " ORDER BY updated_at DESC", params).fetchall()
        return [self._row_to_profile(r) for r in rows]

    # ---------------- 异步端口 ----------------

    async def a_ensure_schema(self) -> None:
        await asyncio.to_thread(self.ensure_schema)

    async def a_create_pool(self, **kwargs: Any) -> PoolRecord:
        return await asyncio.to_thread(lambda: self.create_pool(**kwargs))

    async def a_list_pools(self, tenant_id: str, user_id: str,
                           **kwargs: Any) -> list[PoolRecord]:
        return await asyncio.to_thread(
            lambda: self.list_pools(tenant_id, user_id, **kwargs))

    async def a_add_stock(self, **kwargs: Any) -> WatchRecord:
        return await asyncio.to_thread(lambda: self.add_stock(**kwargs))

    async def a_add_stocks_bulk(self, **kwargs: Any):
        return await asyncio.to_thread(lambda: self.add_stocks_bulk(**kwargs))

    async def a_remove_stock(self, **kwargs: Any) -> bool:
        return await asyncio.to_thread(lambda: self.remove_stock(**kwargs))

    async def a_list_stocks(self, tenant_id: str, user_id: str,
                            **kwargs: Any) -> list[WatchRecord]:
        return await asyncio.to_thread(
            lambda: self.list_stocks(tenant_id, user_id, **kwargs))

    async def a_distinct_codes(self, tenant_id: str, user_id: str) -> set[str]:
        return await asyncio.to_thread(self.distinct_codes, tenant_id, user_id)

    async def a_effective_profile(self, **kwargs: Any) -> ProfileRecord:
        return await asyncio.to_thread(
            lambda: self.effective_profile(**kwargs))

    async def a_save_profile(self, **kwargs: Any) -> ProfileRecord:
        return await asyncio.to_thread(lambda: self.save_profile(**kwargs))

    async def a_delete_profile(self, **kwargs: Any) -> bool:
        return await asyncio.to_thread(lambda: self.delete_profile(**kwargs))

    async def a_list_profiles(self, tenant_id: str, user_id: str,
                              **kwargs: Any) -> list[ProfileRecord]:
        return await asyncio.to_thread(
            lambda: self.list_profiles(tenant_id, user_id, **kwargs))


#: 进程内单例（与 `get_auth_service()` / `get_platform_config()` 同风格）。
_REPO: UserPoolSqliteRepository | None = None
_REPO_PATH = ""


def get_profile_repo() -> UserPoolSqliteRepository:
    """**个股口径**的仓储实例（进程内单例，路径变化时重建）。

    ## 为什么需要这个工厂

    自选池功能已删除（用户口径 2026-09-23），但这个仓储里还有一张
    **仍在用**的表：`dim_intraday_profile_v2`（每个用户各自的权重/阈值/档位）。
    以前调用方是绕道 `get_quota_service()._repo` 拿到它 —— 配额服务只是
    顺手当了仓储句柄，语义上很绕。现在给仓储一个正经的入口。

    ## 为什么要比对路径再重建

    与 `get_watchlist_provider()` 同一个坑：测试会改 `MOSS_SQLITE_PATH`
    换隔离库，而单例一旦把**装配时**的路径固化进去，第二个用例就会读写
    上一个用例的库 —— 症状是"看不到刚存的口径"，看起来像保存失败。
    """
    global _REPO, _REPO_PATH  # noqa: PLW0603
    from src.core.config import get_settings

    path = str(get_settings().sqlite_path)
    if _REPO is None or _REPO_PATH != path:
        _REPO = UserPoolSqliteRepository(path)
        _REPO.ensure_schema()
        _REPO_PATH = path
    return _REPO


def reset_profile_repo() -> None:
    """清掉单例（测试用；也让路径变化时能强制重建）。"""
    global _REPO, _REPO_PATH  # noqa: PLW0603
    _REPO = None
    _REPO_PATH = ""


__all__ = [
    "TABLE_POOL",
    "TABLE_PROFILE",
    "TABLE_WATCH",
    "PoolQuota",
    "PoolRecord",
    "PoolValidationError",
    "ProfileRecord",
    "QuotaCheck",
    "UserPoolSqliteRepository",
    "WatchRecord",
    "check_add_stock",
    "check_new_pool",
    "get_profile_repo",
    "normalize_code",
    "reset_profile_repo",
]
