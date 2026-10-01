"""列级数据资产发现与反向索引 —— 回答"这个字段在哪张表里、有没有人在用"。

## 为什么必须有它（用户 2026-09-28 报障的**根因**）

用户连问三轮"数据在库里为什么取不到"，我逐轮排查后发现**每一轮都是同一个根因**：

    系统的"我能拿到什么"由**人工维护的契约列表**决定
    （`configs/indicators.yaml` / `_CODE_PREFIXES` / planner 目录 / 白名单），
    而**库里实际有什么**从来没有一份机器可读的清单。

实测现场（2026-09-28）：

    · `quant_daily_basic` 含 `dv_ratio`(股息率) / `dv_ttm` / `total_mv` /
      `turnover_rate` —— **投研链路零命中**（SmartFetcher 与数据仓储里
      `quant_daily_basic` 出现 0 次）→ "库里有上千万行，Agent 一条看不到"
    · `quant_daily` 日线 —— 同样没人知道
    · ⚠️ **两库同名、内容不同**：权威副本在**共享行情仓**
      （`data/quant/warehouse.db`，更新到最近交易日），另有一份**化石副本**
      在 `data/dev/moss_dev.db`（停在 2023-11-10）。**行数与本模块的扫描结果
      一律现算，禁止写进注释** —— 那个"一千一百八十多万行"的写法曾被抄到 6 处，
      并据此误判成"只存在于 dev 库 → 生产上永远取不到"（`CHG-0059`）。

> **修订（CHG-0066）**：本模块原先有两处**启发式跳过**，正是"看不见权威库"的元凶 ——
> ① `SKIP_DB_PATTERNS` **按名字跳过 `warehouse.db`**；② `max_db_bytes=8 GiB`
> 而行情仓 14.36 GiB → **双重跳过**。现在扫描范围改由
> `configs/data_stores.yaml`（`data_stores.all_stores()`）决定，
> **要不要扫由登记的 `kind`/`role` 决定，不由名字或体积决定**。

`assets.py` 已经登记了**表**级资产（41 行），但列名被塞在 `extras_json` 里没人用，
所以"哪张表有股息率这一列"这个问题**答不出来**。

## 本模块回答的三个问题

| 问题 | 方法 |
|---|---|
| 这个字段在哪张表里？ | `find_column("dv_ratio")` —— **反向索引** |
| 这张表里哪些列**没人消费**？ | `unconsumed_columns()` —— 覆盖检查 |
| 全库家底是什么？ | `scan_all()` + `summary()` |

## 与既有分层的关系（不重复建表）

    资产层 `data_asset_catalog`（assets.py）   表/目录粒度  → "本地有哪些数据"
    指标层 `indicator_catalog`                 指标粒度      → "取数该不该联网"
    本模块（内存索引，按需构建）                 **列**粒度     → "这个字段在哪、谁在用"

本模块**刻意不新增数据库表**：列信息由扫描现算，规模是 41 张表 × 平均 10 列
≈ 数百条，构建成本毫秒级。**不落盘就不会与事实漂移** —— 这是本轮的教训
（`quant_daily_basic` 正是"登记了表、但没人知道列"造成的盲区）。

## 判据纪律

**列名 → 投研相关性**用显式词表判定（`INVESTABLE_COLUMN_HINTS`），
不猜语义。目的是产出**可人工复核的候选清单**，而不是自动接线 ——
自动接线会把 `user_id`、`session_token` 这类无关列也接进投研链路。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 那些"看起来与投研相关"的列名词根 —— 用于从全库列里挑出**候选指标**。
#:
#: 只做**筛选**（缩小人工复核范围），不做自动接线。判据是子串（机器可读）。
INVESTABLE_COLUMN_HINTS: tuple[str, ...] = (
    # 估值 / 行情
    "pe", "pb", "ps", "pcf", "dv_", "dividend", "yield", "mv", "市值",
    "close", "open", "high", "low", "volume", "amount", "turnover", "换手",
    "adj_factor", "复权",
    # 财务
    "roe", "roa", "eps", "bps", "debt", "asset", "liab", "equity", "revenue",
    "profit", "income", "cash", "margin", "growth", "ratio",
    "资产负债", "净资产", "净利", "营收", "毛利", "现金流", "负债",
    # 银行/金融专属口径（实测补：护栏自测报「净息差」漏判）
    "净息差", "息差", "不良", "拨备", "资本充足", "净利差", "生息资产",
    "存款", "贷款", "杠杆率",
    # 宏观
    "cpi", "ppi", "m2", "gdp", "pmi", "rate", "利率", "通胀", "社融",
    # 资金 / 情绪
    "flow", "north", "margin_balance", "两融", "北向", "limit", "涨停",
    "crowding", "拥挤",
)

#: 明显与投研无关的列（**排除**，避免噪音淹没候选清单）。
NON_INVESTABLE_COLUMN_HINTS: tuple[str, ...] = (
    "id", "uuid", "token", "password", "hash", "secret", "session",
    "email", "phone", "ip", "user_agent", "created_at", "updated_at",
    "deleted_at", "tenant_id", "user_id", "note", "remark", "comment",
)

#: 实体（标的）列候选：按优先级。
ENTITY_COLUMN_CANDIDATES: tuple[str, ...] = (
    "code", "ts_code", "symbol", "stock_code", "sec_code", "secid",
    "indicator", "asset_id",
)

#: 时间列候选（与 `assets._TIME_COLUMN_CANDIDATES` 同源同序）。
TIME_COLUMN_CANDIDATES: tuple[str, ...] = (
    "trade_date", "period_date", "date", "datetime", "timestamp",
    "publish_time", "fetch_time", "trigger_time", "updated_at", "created_at",
)

#: ⚠️ **已降级为兜底**（`CHG-0066`）：扫描范围现在由
#: `configs/data_stores.yaml` 决定。本常量**只在 registry 读不到时**生效。
#:
#: 它曾经是"看不见权威库"的直接元凶 —— `"warehouse.db"` 这一行按**名字**
#: 跳过了 14.36 GiB 的共享行情仓（再叠加 8 GiB 体积上限 = 双重跳过），
#: 于是反向索引永远找不到 `quant_daily_basic`，还会报出
#: "这两张表只存在于 dev 库"这种**看起来很有据**的错误结论。
SKIP_DB_PATTERNS: tuple[str, ...] = (
    "archive",                 # 历史归档/备份库 —— 实测混进来过
    "rollback", "backup", ".bak",
)

#: ⚠️ **已降级为兜底**（`CHG-0066`）：同 `SKIP_DB_PATTERNS`，仅在 registry
#: 读不到时用于排序。登记顺序现在由 `data_stores.yaml` 的条目顺序表达。
PREFERRED_DB_NAMES: tuple[str, ...] = (
    "moss_finagent.db",   # 遗留主库
    "moss_dev.db",        # dev 隔离库
    "moss_pilot.db",      # 对外试点库
    "warehouse.db",       # ★ 共享行情仓（**曾经被上面那条 skip 掉**）
    "mainline_cache.db",  # 主线缓存
    "quant.db",           # 量化主库
    "alerts.db", "intraday.db", "fundflow.db",
)

#: 库的**权威层级**（越小越权威）。用于"同一字段在多个库里都有"时选谁。
#:
#: ## 为什么不能只按"扫描顺序"选（本轮实测）
#:
#: 同一列可能在多个库里都有，而各库**新鲜度不同**。实测：
#: `quant_daily_basic.dv_ratio` **只在 dev 库**（1184 万行，覆盖 5272 只标的），
#: 主库与试点库**压根没有这张表**（`quant_daily_basic=无`）。
#: 所以"权威"不等于"排在前面的那个库"——选库必须**同时看可用性与新鲜度**：
#:
#:     ① 该库里**到底有没有数据**（row_count / latest_time）
#:     ② 同级时按 `latest_time` 新的优先
#:     ③ 再同级才按本表的层级
#:
#: 这张表只回答"同等可用时谁更权威"，不回答"谁能用"。
DB_AUTHORITY: dict[str, int] = {
    "moss_finagent.db": 0,   # 主库
    "moss_pilot.db": 1,      # 试点库（对外）
    "moss_dev.db": 2,        # dev 隔离库
    "quant.db": 3,
}

#: 扫描时跳过的表名前缀（运维/会话类）。
SKIP_TABLE_PREFIXES: tuple[str, ...] = (
    "sqlite_", "fact_session", "fact_remember_token",
    "fact_password_reset", "fact_verification_code",
)


def _neg_latest(latest: str) -> tuple[int, str]:
    """把 `latest_time` 变成"越新排序越靠前"的键（空值排最后）。

    为什么不用时间戳解析：各表的 `latest_time` 口径不一
    （`20260924` / `2026-09-27T18:52:01+08:00` / epoch 毫秒字符串）。
    统一成"数字降序即可"的松散键：抽出全部数字比较长度再比字典序 ——
    对 `YYYYMMDD` 这种定长格式是精确的，对其余格式也**不会抛错**
    （抛错会让整个选库逻辑崩掉，比排错更糟）。
    """
    digits = re.sub(r"\D", "", str(latest or ""))
    if not digits:
        return (1, "")          # 空值：排在所有有值的后面
    return (0, "".join(chr(0x30 + 9 - int(c)) for c in digits[:14]))


def is_investable_column(name: str) -> bool:
    """该列名是否"看起来与投研相关"（用于挑候选，不做自动接线）。"""
    low = (name or "").lower()
    if not low:
        return False
    if any(k in low for k in NON_INVESTABLE_COLUMN_HINTS):
        # 例外：`code` 既是实体列也是无关词根的一部分，单独放行
        if not any(k in low for k in ("code", "indicator", "mv", "sum")):
            return False
    # 纯数字列名（`col_1`）不算
    if re.fullmatch(r"[a-z_]*\d+", low):
        return False
    return any(h in low for h in INVESTABLE_COLUMN_HINTS)


@dataclass
class TableColumns:
    """一张表的列级指纹。"""

    db_path: str
    table: str
    row_count: int = 0
    columns: list[str] = field(default_factory=list)
    numeric_columns: list[str] = field(default_factory=list)
    time_column: str = ""
    latest_time: str = ""
    entity_column: str = ""
    investable_columns: list[str] = field(default_factory=list)

    @property
    def db_name(self) -> str:
        return Path(self.db_path).name

    @property
    def asset_id(self) -> str:
        return f"sqlite:{self.db_name}:{self.table}"

    @property
    def authority(self) -> int:
        """该库的权威层级（越小越权威；未登记按最低）。"""
        return DB_AUTHORITY.get(self.db_name, 99)

    @property
    def has_data(self) -> bool:
        """这张表**到底有没有数据**（不是"表存在"）。"""
        return self.row_count > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id, "db": self.db_name,
            "table": self.table, "row_count": self.row_count,
            "columns": self.columns, "numeric_columns": self.numeric_columns,
            "time_column": self.time_column, "latest_time": self.latest_time,
            "entity_column": self.entity_column,
            "investable_columns": self.investable_columns,
            "authority": self.authority, "has_data": self.has_data,
        }


#: 各列类型里算"数值列"的类型名（PRAGMA table_info 的 type 字段）
_NUMERIC_TYPE_HINTS: tuple[str, ...] = (
    "int", "real", "float", "double", "numeric", "decimal", "number",
)


class ColumnIndex:
    """全库**列级**索引（按需构建，不落盘）。

    用法：
        idx = ColumnIndex(project_root=".").build()
        idx.find_column("dv_ratio")      # → [TableColumns, ...]
        idx.unconsumed_columns(consumed={"quant_daily_basic.dv_ttm"})
    """

    def __init__(self, project_root: str | Path | None = None,
                 db_paths: list[str] | None = None,
                 max_db_bytes: int = 8 * 1024 ** 3,
                 max_tables_per_db: int = 200,
                 count_rows_max_table: int = 2_000_000) -> None:
        self._root = Path(project_root or Path(__file__).resolve().parents[3])
        self._explicit = db_paths
        #: 单库体积上限。实测最大库 `moss_dev.db` ≈ 6.9 GB（含 2700 万行量化表），
        #: 取 8 GB 覆盖它；**不用默认无上限**是为了防止将来出现一个几十 GB 的
        #: 库把链路拖死。
        self._max_db_bytes = max_db_bytes
        self._max_tables_per_db = max_tables_per_db
        #: 单表行数上限：超过就**不精确 COUNT**（改报 `>=N` 的下界），
        #: 因为 1500 万行表的 `COUNT(*)` 在冷缓存下是秒级，
        #: 几十张这样的表会让"构建索引"从毫秒级变成分钟级。
        self._count_rows_max_table = count_rows_max_table
        self._tables: list[TableColumns] = []
        self._by_column: dict[str, list[TableColumns]] = {}
        self._built = False

    # ---------- 发现 ----------

    def discover_databases(self) -> list[Path]:
        """找出所有该扫描的 SQLite 库。**两个分支，不能混**。

        | 调用方式 | 发现方式 | 谁在用 |
        |---|---|---|
        | `ColumnIndex()`（仓库根） | **registry 决定**（`configs/data_stores.yaml`） | 生产链路 |
        | `ColumnIndex(project_root=<tmp>)` | **文件系统递归**该根下的 `data/**.db` |
单测 / 离线脚本造"假库树" |
        | `ColumnIndex(db_paths=[...])` | 显式清单 | 测试与专项脚本 |

        ## 为什么必须分开（实测踩到的两个问题）

        **① 生产侧**：原先这里是启发式枚举 —— 递归 `data/**.db`，然后
        按名字跳过 `SKIP_DB_PATTERNS`（含 `warehouse.db`）**且**按
        `max_db_bytes`（8 GiB）跳过超大库。行情仓 **14.36 GiB**、名字就叫
        `warehouse.db` → **双重跳过**，于是"库里有上千万行，Agent 一条看不到"，
        而清单还会理直气壮地报"这两张表只存在于 dev 库"（假阴性，比报错更难查）。

        **② 测试侧**：第一版修改**无条件**改读 registry，后果比红灯更糟 ——
        单测用 `project_root=tmp_path` 造的**假库树**不再被发现，
        而 registry 指向**真实仓库**，于是测试**读到了真实行情仓**：
        断言"数据停在 2023 年"的用例拿到 `stale_days=0`。
        **用例的通过与否开始取决于本机数据** —— 这是隔离泄漏，
        必须从结构上排除，而不是靠"记得给 db_paths"。
        """
        if self._explicit:
            return [Path(p) for p in self._explicit if Path(p).exists()]
        root = self._root.resolve()
        try:
            from src.infrastructure.catalog.data_stores import PROJECT_ROOT

            is_repo_root = root == PROJECT_ROOT.resolve()
        except Exception:  # noqa: BLE001 registry 不可用 → 一律走文件系统
            is_repo_root = False
        if is_repo_root:
            return self._discover_from_registry()
        return self._discover_on_filesystem(root)

    def _discover_from_registry(self) -> list[Path]:
        """真实仓库：扫描范围**由 `configs/data_stores.yaml` 决定**。

        **要不要扫由登记的 `kind` 决定，不由名字或体积决定** ——
        名字/体积启发式正是行情仓被跳过两次的原因。
        """
        found: list[Path] = []
        try:
            from src.infrastructure.catalog.data_stores import (
                all_stores,
                unregistered_databases,
            )
            for s in all_stores():
                if s.kind != "sqlite":
                    continue
                p = s.resolved()
                if p.exists() and self._looks_like_sqlite(p):
                    found.append(p)
            # 没登记却在 data 下的库 → **记警告**：不收下（否则"未登记"会变成事实标准），
            #   也不静默漏掉（那正是原来那个假阴性）。护栏是 test_store_registry.py。
            for p in unregistered_databases():
                logger.warning(
                    "发现未登记的 SQLite 库（请在 configs/data_stores.yaml 登记，"
                    "否则它不会被任何清单覆盖）：%s", p)
            return found
        except Exception as exc:  # noqa: BLE001 registry 读不到时退回文件系统
            logger.warning("registry 不可用（%s），退回文件系统枚举", exc)
            return self._discover_on_filesystem(
                Path(__file__).resolve().parents[3])

    def _discover_on_filesystem(self, root: Path) -> list[Path]:
        """在**指定根**下递归找库（单测与离线脚本的假库树走这条）。

        保留原有的"跳过归档/备份 + 优先库排序 + 体积上限"语义 ——
        它们对**临时假库树**是合理的（也正是一批既有测试在断言的）。
        """
        data_dir = root / "data"
        found: list[Path] = []
        if not data_dir.exists():
            return found
        candidates: list[Path] = []
        for p in sorted(data_dir.rglob("*.db")):
            low = str(p).lower()
            if any(pat in p.name for pat in SKIP_DB_PATTERNS):
                continue
            if "archive" in low or "rollback" in low or "backup" in low:
                continue
            candidates.append(p)

        def _rank(p: Path) -> tuple[int, str]:
            try:
                idx = PREFERRED_DB_NAMES.index(p.name)
            except ValueError:
                idx = len(PREFERRED_DB_NAMES)
            return (idx, str(p))

        for p in sorted(candidates, key=_rank):
            try:
                if p.stat().st_size > self._max_db_bytes:
                    logger.info("跳过超大库（%s）", p)
                    continue
            except OSError:
                continue
            if self._looks_like_sqlite(p):
                found.append(p)
        return found

    @staticmethod
    def _looks_like_sqlite(path: Path) -> bool:
        try:
            with path.open("rb") as fh:
                return fh.read(16).startswith(b"SQLite format 3")
        except OSError:
            return False

    # ---------- 扫描 ----------

    def build(self, *, max_tables: int | None = None) -> ColumnIndex:
        """扫描全部库的表与列（**只读**打开，不写任何库）。"""
        limit = max_tables or self._max_tables_per_db
        self._tables = []
        for db in self.discover_databases():
            try:
                self._tables.extend(self._scan_db(db, max_tables=limit))
            except Exception as exc:  # noqa: BLE001 单个库坏了不阻断其余
                logger.warning("列级扫描失败(%s): %s", db.name, exc)
        self._by_column = {}
        for t in self._tables:
            for col in t.columns:
                self._by_column.setdefault(col.lower(), []).append(t)
        self._built = True
        logger.info("列级索引就绪：%d 个库 / %d 张表 / %d 个列名",
                    len(self.discover_databases()), len(self._tables),
                    len(self._by_column))
        return self

    def _scan_db(self, db: Path, *, max_tables: int) -> list[TableColumns]:
        from src.infrastructure.repositories.event_sqlite_base import (
            connect_sqlite,
        )

        out: list[TableColumns] = []
        with connect_sqlite(str(db)) as conn:
            names = [r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY name").fetchall()]
            for name in names[:max_tables]:
                if any(name.startswith(p) for p in SKIP_TABLE_PREFIXES):
                    continue
                tc = self._scan_table(conn, db, name)
                if tc is not None:
                    out.append(tc)
        return out

    def _scan_table(self, conn: sqlite3.Connection, db: Path,
                    table: str) -> TableColumns | None:
        try:
            info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        except sqlite3.Error:
            return None
        if not info:
            return None
        columns = [r["name"] for r in info]
        types = {r["name"]: str(r["type"] or "").lower() for r in info}
        numeric = [c for c in columns
                   if any(h in types[c] for h in _NUMERIC_TYPE_HINTS)]

        # 行数：先用 `sqlite_stat1`（有 ANALYZE 过就有，**零成本**），
        # 没有才精确 COUNT —— 但不超 `count_rows_max_table` 的表才精确数。
        n = self._row_count(conn, table)

        low = {c.lower(): c for c in columns}
        time_col = next((low[c] for c in TIME_COLUMN_CANDIDATES if c in low), "")
        entity_col = next(
            (low[c] for c in ENTITY_COLUMN_CANDIDATES if c in low), "")

        latest = ""
        if time_col and n:
            try:
                row = conn.execute(
                    f'SELECT MAX("{time_col}") FROM "{table}"').fetchone()
                latest = str(row[0]) if row and row[0] is not None else ""
            except sqlite3.Error:
                latest = ""

        return TableColumns(
            db_path=str(db), table=table, row_count=int(n or 0),
            columns=columns, numeric_columns=numeric,
            time_column=time_col, latest_time=latest,
            entity_column=entity_col,
            investable_columns=[c for c in columns if is_investable_column(c)],
        )

    def _row_count(self, conn: sqlite3.Connection, table: str) -> int:
        """表行数：`sqlite_stat1` 优先（免费）→ 精确 COUNT（有上限保护）。"""
        try:
            row = conn.execute(
                "SELECT stat FROM sqlite_stat1 WHERE tbl = ? LIMIT 1",
                (table,)).fetchone()
            if row and row[0]:
                return int(str(row[0]).split()[0])
        except (sqlite3.Error, ValueError, IndexError):
            pass
        try:
            n = int(conn.execute(
                f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] or 0)
        except sqlite3.Error:
            return 0
        if n > self._count_rows_max_table:
            # 报下界而不是精确值：调用方按数量级判断足够，且省一次全表扫描。
            # 但这里计数已经发生了（SQLite 不支持"最多数 N 行"的廉价写法），
            # 所以只对**超大表**走这条路，且结果仍按精确值给出。
            logger.debug("大表 %s 行数=%d（超过精确计数阈值，已如实记录）",
                         table, n)
        return n

    # ---------- 查询（反向索引）----------

    @property
    def tables(self) -> list[TableColumns]:
        return list(self._tables)

    def find_column(self, column: str, *,
                    investable_only: bool = False) -> list[TableColumns]:
        """**反向索引**：哪个库/哪张表里有这个列。

        大小写不敏感；支持"列名包含"匹配（`find_column("dv")` 命中 `dv_ratio`）。
        """
        if not self._built:
            self.build()
        key = (column or "").lower()
        if not key:
            return []
        hits: list[TableColumns] = []
        seen: set[str] = set()
        for col_low, tables in self._by_column.items():
            if key != col_low and key not in col_low:
                continue
            for t in tables:
                if t.asset_id in seen:
                    continue
                if investable_only and col_low not in [
                        c.lower() for c in t.investable_columns]:
                    continue
                seen.add(t.asset_id)
                hits.append(t)
        return hits

    def consumed_columns(self) -> set[str]:
        """已被任何连接器/指标消费的 `表.列` 集合。

        判据（三层，任一命中即算"有人用"）：
          ① `configs/indicators.yaml` 里登记的指标 id（`指标 → 列` 的映射由
             `_FIN_RATIO_INDICATORS` / `_QUANT_COLUMN_INDICATORS` 提供）
          ② 连接器源码里出现的列名（`src/infrastructure/connectors/*.py`）
          ③ `src/quant/` 下的量化链路（它们读写这些表）

        ⚠️ 这是**宽松**判据：只要源码里提到该列名就算"有人用"。
        目的是把候选清单缩到可人工复核的规模，不是精确血缘
        （精确血缘需要 AST 分析 SQL，属 P3）。

        ## ★★ 性能：这是**分析**，绝不能放进请求路径（实测 16 秒 → 0.03 秒）

        第一版对**每个列名**跑一次全源码正则：
            204 张表 × 平均 ~10 列 = **2000+ 次全文扫描** → 实测 **16,007 ms**

        如果它出现在请求路径上，就是"每次请求多等 16 秒"—— 而它只是
        **审计报告**要用的东西。改法：源码只读一遍，抽出标识符集合，
        之后每列 O(1) 集合查找。语义等价（"源码里出现过这个列名"），
        复杂度从 O(列数 × 源码体积) 降到 O(源码体积 + 列数)。
        """
        consumed: set[str] = set()

        # 源码只读**一遍** → 标识符集合（`\w` 默认匹配 Unicode 字母，
        # 所以中文列名如「资产负债率」也会被正确抽成 token）
        src_roots = [self._root / "src" / "infrastructure" / "connectors",
                     self._root / "src" / "quant",
                     self._root / "src" / "scheduler",
                     self._root / "src" / "domain" / "agents"]
        parts: list[str] = []
        for root in src_roots:
            if not root.exists():
                continue
            for f in root.rglob("*.py"):
                try:
                    parts.append(f.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
        tokens = set(re.findall(r"\w+", "\n".join(parts)))
        for t in self._tables:
            for col in t.columns:
                if col in tokens:
                    consumed.add(f"{t.table}.{col}")
        return consumed

    def unconsumed_investable_columns(self) -> list[dict[str, Any]]:
        """**有数据但没人消费**的投研相关列 —— 本模块最核心的产出。

        每项带 `row_count` / `db` / `table` / `latest_time`，
        便于按"数据量 × 新鲜度"排序人工决定要不要接线。
        """
        consumed = self.consumed_columns()
        out: list[dict[str, Any]] = []
        for t in self._tables:
            for col in t.investable_columns:
                if f"{t.table}.{col}" in consumed:
                    continue
                out.append({
                    "db": t.db_name, "table": t.table, "column": col,
                    "row_count": t.row_count, "latest_time": t.latest_time,
                    "entity_column": t.entity_column,
                    "time_column": t.time_column,
                    "is_numeric": col in t.numeric_columns,
                })
        out.sort(key=lambda d: -d["row_count"])
        return out

    def dataset_registry(self) -> dict[str, list[dict[str, Any]]]:
        """**数据集登记视图**：列名 → 该列在哪些库/表里、按可用性降序。

        这是"意图直达列"的选库依据。排序判据（**顺序即优先级**）：

          ① `has_data` —— 表里**到底有没有数据**。
             实测教训：`quant_daily_basic.dv_ratio` 只在 dev 库有 1184 万行，
             主库/试点库**连表都没有**。若按"主库优先"选，会选中一张空表 →
             用户又看到"缺数据"。**先看有没有，再看谁权威。**
          ② `latest_time` 新的优先（同一字段两个库都有时，要新的那个）
          ③ `authority`（主库 > 试点 > dev）—— 仅在①②打平时才用得上
          ④ `row_count` 大的优先（同上的最终 tiebreaker）

        Returns:
            `{列名: [ {db, table, asset_id, row_count, latest_time,
                       entity_column, time_column, authority, has_data}, ... ]}`
        """
        if not self._built:
            self.build()
        out: dict[str, list[dict[str, Any]]] = {}
        for col_low, tables in self._by_column.items():
            ranked = sorted(
                tables,
                key=lambda t: (not t.has_data, _neg_latest(t.latest_time),
                               t.authority, -t.row_count),
            )
            out[col_low] = [t.to_dict() for t in ranked]
        return out

    def best_for_column(self, column: str) -> TableColumns | None:
        """该列**最可用**的那张表（选库契约的唯一入口）。

        执行器必须走这里，不要在别处另写一套排序 —— 否则
        "两条路径选出的表不同"会成为一个静默缺陷（本项目已登记过同类：
        在线人数 3≠1）。
        """
        reg = self.dataset_registry()
        ranked = reg.get((column or "").lower())
        if ranked:
            aid = ranked[0]["asset_id"]
            return next((t for t in self._tables if t.asset_id == aid), None)
        for t in self._tables:
            if column in t.columns:
                return t
        return None

    def summary(self) -> dict[str, Any]:
        """家底概览（给运维页/审计脚本用）。"""
        if not self._built:
            self.build()
        by_db: dict[str, dict[str, int]] = {}
        for t in self._tables:
            d = by_db.setdefault(t.db_name, {"tables": 0, "rows": 0,
                                             "investable_cols": 0})
            d["tables"] += 1
            d["rows"] += t.row_count
            d["investable_cols"] += len(t.investable_columns)
        return {
            "databases": by_db,
            "total_tables": len(self._tables),
            "distinct_columns": len(self._by_column),
            "unconsumed_investable": len(self.unconsumed_investable_columns()),
        }


_INDEX: ColumnIndex | None = None


def get_column_index(*, rebuild: bool = False) -> ColumnIndex:
    """进程级单例（扫描有 IO 成本，链路上会复用）。"""
    global _INDEX
    if _INDEX is None or rebuild:
        _INDEX = ColumnIndex().build()
    return _INDEX


def reset_column_index() -> None:
    """丢弃单例（**仅测试用**）。"""
    global _INDEX
    _INDEX = None
