"""运行时指标索引表（indicator_catalog）的 DB 仓储。

## 设计目标

把 `configs/indicators.yaml` 的元数据 + **运行时新鲜度** 落库一份：

| 字段 | 含义 | 来源 |
|---|---|---|
| indicator | 主键，指标 id | YAML + 动态 |
| category | 分类（cn_macro/us_macro/...） | YAML |
| frequency | 频率（realtime/daily/monthly/...） | YAML |
| freshness_hours | freshness 阈值 | YAML |
| primary_source | 主数据源 | YAML |
| enabled | 是否启用 | YAML |
| last_period_date | 最新数据点 period_date | runtime |
| last_fetch_time_ms | 最近一次入库的 fetch_time（epoch ms） | runtime |
| row_count | 当前 DB 内的数据点行数 | runtime |
| first_seen_at | 该 indicator 首次出现在 DB 的时间 | runtime |
| updated_at | 该索引行最后更新时间 | runtime |

## 为什么要单独建表（不直接复用 `fact_data_points`）

`fact_data_points` 已经有数据；但"该指标最新数据是什么时候入库的"这类
**元数据查询**需要聚合（`MAX(period_date)` / `MAX(fetch_time)` / `COUNT(*)`），
对每条 fetch 都跑聚合很贵。`indicator_catalog` 是缓存这种聚合的"运行索引"，
让 SmartFetcher 一次 SELECT 就能决定哪些 fresh / 哪些 stale。

## 与 registry 的关系

  - IndicatorRegistry（registry.py）：静态配置（YAML），每次启动加载；
    改 YAML 不重启 → 不会自动 reload（生产 30s 缓存）
  - CatalogRepository（本模块）：运行时索引（DB），每次 fetch 自动更新；
    是 SmartFetcher 的"工作记忆"，YAML 改了它会重新对齐

★ 修改 configs/indicators.yaml 后必须：
  1) 跑 `python scripts/rebuild_indicator_catalog.py` 让 DB 索引表同步（见后续脚本）
  2) 或重启后端（registry 30s 内自动 reload；首次 SmartFetcher 调用会重建 DB 索引）
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.core.schemas import Confidence, DataPoint
from src.infrastructure.catalog.registry import (
    FreshnessState,
    IndicatorMeta,
    get_registry,
)
from src.infrastructure.repositories.event_sqlite_base import connect_sqlite

logger = logging.getLogger(__name__)


#: 建表 DDL（**不含**依赖新列的索引 —— 索引在迁移之后建，见 _CATALOG_SCHEMA_INDEXES）
_CATALOG_SCHEMA_BASE = """
CREATE TABLE IF NOT EXISTS indicator_catalog (
    indicator TEXT PRIMARY KEY,
    category TEXT NOT NULL DEFAULT 'other',
    frequency TEXT NOT NULL DEFAULT 'daily',
    freshness_hours REAL NOT NULL DEFAULT 24.0,
    primary_source TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    ttl_days INTEGER NOT NULL DEFAULT 365,
    units TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    -- ★ 存储位置（2026-09-28）：回答"数据在哪张表/哪个文件"
    storage_backend TEXT NOT NULL DEFAULT 'sqlite:fact_data_points',
    storage_location TEXT NOT NULL DEFAULT '',
    storage_note TEXT NOT NULL DEFAULT '',
    last_period_date TEXT,
    last_fetch_time_ms INTEGER NOT NULL DEFAULT 0,
    row_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);
"""

#: 索引 DDL（必须在 `_migrate_sync` 补列**之后**执行）
_CATALOG_SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_catalog_category
    ON indicator_catalog(category, enabled);
CREATE INDEX IF NOT EXISTS idx_catalog_last_fetch
    ON indicator_catalog(last_fetch_time_ms);
-- 按频率索引：Scheduler 自动生成 job 时按 frequency 分组捞指标
CREATE INDEX IF NOT EXISTS idx_catalog_frequency
    ON indicator_catalog(frequency, enabled);
-- 按后端索引：运维查"哪些数据落在文件里 / 数仓里"
CREATE INDEX IF NOT EXISTS idx_catalog_backend
    ON indicator_catalog(storage_backend);
"""

#: 向后兼容别名（旧测试/旧代码可能引用 `_CATALOG_SCHEMA`）
_CATALOG_SCHEMA = _CATALOG_SCHEMA_BASE


@dataclass
class CatalogEntry:
    """单条索引（运行时态 + 元数据）。"""

    indicator: str
    category: str
    frequency: str
    freshness_hours: float
    primary_source: str
    source_url: str
    enabled: bool
    ttl_days: int
    units: str
    notes: str
    last_period_date: str | None
    last_fetch_time_ms: int
    row_count: int
    first_seen_at: int
    updated_at: int
    # ★ 存储位置
    storage_backend: str = "sqlite:fact_data_points"
    storage_location: str = ""
    storage_note: str = ""

    def to_meta(self) -> IndicatorMeta:
        """CatalogEntry → IndicatorMeta（去掉运行时字段）。"""
        return IndicatorMeta(
            indicator=self.indicator,
            category=self.category,
            frequency=self.frequency,
            freshness_hours=self.freshness_hours,
            primary_source=self.primary_source,
            source_url=self.source_url,
            enabled=self.enabled,
            ttl_days=self.ttl_days,
            units=self.units,
            notes=self.notes,
            storage_backend=self.storage_backend,
            storage_location=self.storage_location,
            storage_note=self.storage_note,
        )

    def to_freshness(self) -> FreshnessState:
        """CatalogEntry → FreshnessState（meta + 运行时态）。"""
        return FreshnessState(
            meta=self.to_meta(),
            last_period_date=self.last_period_date,
            last_fetch_time_ms=self.last_fetch_time_ms,
            row_count=self.row_count,
        )


class CatalogRepository:
    """indicator_catalog 表 CRUD（SQLite，线程池化）。"""

    def __init__(self, db_path: str | None = None) -> None:
        # 默认与 DataPointRepository 共享 db_path（同一个 SQLite 文件）
        from src.core.config import get_settings
        settings = get_settings()
        self._db_path = db_path or settings.sqlite_path

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._db_path)

    # ---------- schema ----------

    def _ensure_schema_sync(self) -> None:
        """建表 + 迁移 + 建索引（**顺序有讲究**）。

        ⚠️ 顺序必须是：建表 → 补列（迁移）→ 建索引。
        反过来的话，"为 storage_backend 建索引"会在旧库上失败 ——
        旧库的表还没有这一列（实测：`no such column: storage_backend`）。
        """
        with self._connect() as conn:
            # 1) 建表（新库）
            conn.executescript(_CATALOG_SCHEMA_BASE)
            # 2) 迁移（旧库补列）
            self._migrate_sync(conn)
            # 3) 建索引（此时列一定存在）
            conn.executescript(_CATALOG_SCHEMA_INDEXES)

    @staticmethod
    def _migrate_sync(conn: sqlite3.Connection) -> None:
        """幂等迁移：给旧库补 `storage_*` 列（2026-09-28 新增）。

        为什么不用 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`：SQLite 不支持
        `IF NOT EXISTS` 子句修饰 ADD COLUMN（只有 PostgreSQL 支持）。
        所以先 PRAGMA 读现有列，缺什么补什么。
        """
        existing = {r["name"] for r in conn.execute(
            "PRAGMA table_info(indicator_catalog)").fetchall()}
        for col, ddl in (
            ("storage_backend",
             "ALTER TABLE indicator_catalog ADD COLUMN "
             "storage_backend TEXT NOT NULL DEFAULT 'sqlite:fact_data_points'"),
            ("storage_location",
             "ALTER TABLE indicator_catalog ADD COLUMN "
             "storage_location TEXT NOT NULL DEFAULT ''"),
            ("storage_note",
             "ALTER TABLE indicator_catalog ADD COLUMN "
             "storage_note TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in existing:
                conn.execute(ddl)

    async def ensure_schema(self) -> None:
        await asyncio.to_thread(self._ensure_schema_sync)

    # ---------- 运维查询：数据在哪 ----------

    def _locate_sync(self, indicator: str) -> dict[str, str] | None:
        entry = self._get_sync(indicator)
        if entry is None:
            return None
        return {
            "indicator": entry.indicator,
            "storage_backend": entry.storage_backend,
            "storage_location": entry.storage_location,
            "storage_note": entry.storage_note,
            "frequency": entry.frequency,
            "freshness_hours": str(entry.freshness_hours),
            "last_period_date": entry.last_period_date or "",
            "last_fetch_time_ms": str(entry.last_fetch_time_ms),
            "row_count": str(entry.row_count),
        }

    async def locate(self, indicator: str) -> dict[str, str] | None:
        """回答"这个指标的数据在哪张表/哪个文件"（运维/审计用）。"""
        await asyncio.to_thread(self._ensure_schema_sync)
        return await asyncio.to_thread(self._locate_sync, indicator)

    def _by_backend_sync(self, kind: str) -> list[CatalogEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM indicator_catalog WHERE storage_backend LIKE ? "
                "ORDER BY indicator",
                (f"{kind}:%",),
            ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    async def by_backend(self, kind: str) -> list[CatalogEntry]:
        """按后端类型列指标（'file' / 'sqlite' / 'mysql' / 'postgres'）。"""
        await asyncio.to_thread(self._ensure_schema_sync)
        return await asyncio.to_thread(self._by_backend_sync, kind)

    def _stale_sync(self, *, now_ms: int | None = None) -> list[CatalogEntry]:
        """列出所有 stale 指标（last_fetch 超过 freshness_hours）。"""
        now_ms = now_ms or int(time.time() * 1000)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM indicator_catalog
                WHERE enabled = 1
                  AND (row_count = 0
                       OR (? - last_fetch_time_ms) > freshness_hours * 3600000)
                ORDER BY last_fetch_time_ms
                """,
                (now_ms,),
            ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    async def stale(self, *, now_ms: int | None = None) -> list[CatalogEntry]:
        """列出 stale 指标（给"该更新哪些"的运维视图用）。"""
        await asyncio.to_thread(self._ensure_schema_sync)
        return await asyncio.to_thread(self._stale_sync, now_ms=now_ms)

    # ---------- 写入 ----------

    def _upsert_meta_sync(
        self,
        meta: IndicatorMeta,
        *,
        only_if_missing: bool = False,
    ) -> None:
        """把 YAML 元数据落库（不覆盖运行时字段 last_* / row_count）。"""
        now_ms = int(time.time() * 1000)
        with self._connect() as conn:
            if only_if_missing:
                # 已存在行则不修改元数据（保留运行时的微调）
                existing = conn.execute(
                    "SELECT 1 FROM indicator_catalog WHERE indicator = ?",
                    (meta.indicator,)).fetchone()
                if existing:
                    return
            conn.execute(
                """
                INSERT INTO indicator_catalog (
                    indicator, category, frequency, freshness_hours,
                    primary_source, source_url, enabled, ttl_days,
                    units, notes,
                    storage_backend, storage_location, storage_note,
                    first_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(indicator) DO UPDATE SET
                    category = excluded.category,
                    frequency = excluded.frequency,
                    freshness_hours = excluded.freshness_hours,
                    primary_source = excluded.primary_source,
                    source_url = excluded.source_url,
                    enabled = excluded.enabled,
                    ttl_days = excluded.ttl_days,
                    units = excluded.units,
                    notes = excluded.notes,
                    storage_backend = excluded.storage_backend,
                    storage_location = excluded.storage_location,
                    storage_note = excluded.storage_note,
                    updated_at = excluded.updated_at
                """,
                (
                    meta.indicator, meta.category, meta.frequency,
                    meta.freshness_hours, meta.primary_source, meta.source_url,
                    int(meta.enabled), meta.ttl_days, meta.units, meta.notes,
                    meta.storage_backend, meta.storage_location, meta.storage_note,
                    now_ms, now_ms,
                ),
            )

    async def upsert_meta(self, meta: IndicatorMeta, *, only_if_missing: bool = False) -> None:
        await asyncio.to_thread(self._upsert_meta_sync, meta,
                                only_if_missing=only_if_missing)

    def _refresh_from_registry_sync(self) -> int:
        """把 YAML 全量元数据落库（启动时调用 1 次）。返回落库条数。"""
        registry = get_registry()
        count = 0
        for meta in registry.all():
            self._upsert_meta_sync(meta)
            count += 1
        return count

    async def refresh_from_registry(self) -> int:
        return await asyncio.to_thread(self._refresh_from_registry_sync)

    # ---------- ★ 全量重建（修"索引从未同步"的缺陷）----------

    def _rebuild_all_sync(self, *, data_db_path: str | None = None) -> dict[str, int]:
        """从 YAML + 事实表**全量重建**索引。返回统计。

        ## 为什么需要它（2026-09-28 实测暴露）

        `indicator_catalog` 表有两条**独立**的数据来源，而原实现两条都没接：

          1. **元数据**来自 `configs/indicators.yaml` —— 靠 `refresh_from_registry()`
             写入。但**从没有地方在启动时调用它** → 表里只有零星几条
             （实测：YAML 43 条，表里 16 条，`mkt:cybkcb:spot_summary` 根本不在）。
          2. **运行时状态**（row_count / last_period_date / last_fetch_time）
             靠 `refresh_stats_from_facts()` 回填。但它只在 `supervisor._node`
             的 A04 分支里对**本次任务涉及的指标**调用 → **索引表建立之前就
             已入库的历史数据从未被登记**（实测：事实表 `mkt:turnover:total`
             有 241 条，索引表记 `row_count=0`）。

        后果：SmartFetcher 查索引 → 查不到 / row_count=0 → 判 stale →
        **明明库里有最新数据还是去联网**（用户实测：今天的数据已入库，
        仍白等 13.1s）。

        修法：启动时调一次本函数 —— 先同步全部元数据，再对**事实表里
        出现过的所有指标**做一次聚合回填。
        """
        registry = get_registry()
        metas = registry.all()
        stats = {"metas_synced": 0, "indicators_backfilled": 0}

        # 1) 元数据全量同步（含模板）
        for meta in metas:
            self._upsert_meta_sync(meta)
            stats["metas_synced"] += 1

        # 2) 事实表里出现过的所有指标 → 聚合回填
        db_path = data_db_path or self._db_path
        try:
            conn = connect_sqlite(db_path)
            rows = conn.execute(
                "SELECT DISTINCT indicator FROM fact_data_points"
            ).fetchall()
            conn.close()
        except sqlite3.Error as exc:
            logger.warning("catalog 重建：读取事实表失败（%s），跳过回填", exc)
            return stats
        existing = [r["indicator"] for r in rows]
        # 事实表里的指标若未在 YAML 登记 → 也补一条（category=other）
        known = {m.indicator for m in metas}
        for ind in existing:
            if ind in known:
                continue
            # ★★ 2026-09-30：先试**基名继承**，再谈兜底。
            #
            # 实测缺陷：`商誉占净资产比:000001` 这类具体 id 在 YAML 里**只登记了
            # 基名**（`商誉占净资产比`，notes 写明"模板：实际指标带 6 位代码后缀"），
            # 而 `get()` 的精确/通配都要求段数相同 ⇒ 查不到 ⇒ 兜底 `daily/24h`
            # ⇒ 维护审计拿**日频**宽限（3 天）去判**季频**数据（92 天）
            # ⇒ 报 `freq_mismatch`（实测 3+5+3 = 11 条），而且它们只有 1 期数据，
            # 频率推断按纪律"证据不足不猜"（n<2）⇒ **永远修不好**。
            base = registry.get_by_base(ind)
            if base is not None:
                self._upsert_meta_sync(IndicatorMeta(
                    indicator=ind, category=base.category,
                    frequency=base.frequency,
                    freshness_hours=base.freshness_hours,
                    primary_source=base.primary_source,
                    source_url=base.source_url, enabled=base.enabled,
                    ttl_days=base.ttl_days, units=base.units, notes=base.notes,
                ))
            else:
                # ★ 兜底从 `daily/24h` 改成 `monthly/720h`（用户 2026-09-30 口径：
                #   「没登记更新周期的数据都触发**月频**更新一次」）。
                #   为什么不能继续用 daily：daily 意味着"每天该更新"，于是
                #   ① 审计用 3 天宽限判它 ⇒ 一片 `stale` 假告警；
                #   ② 补采按日重试 ⇒ 对一个其实没人登记周期的指标每天花一次取数。
                #   monthly 是**诚实的下限**：我不知道它该多久更新，那就至少每月试一次。
                self._upsert_meta_sync(IndicatorMeta(
                    indicator=ind, category="other", frequency="monthly",
                    freshness_hours=720, primary_source="", source_url="",
                    enabled=True, ttl_days=365,
                ))
            stats["metas_synced"] += 1
        if existing:
            stats["indicators_backfilled"] = self._refresh_stats_from_facts_sync(
                existing)
        return stats

    async def rebuild_all(self, *, data_db_path: str | None = None) -> dict[str, int]:
        """全量重建索引（元数据 + 运行时状态）。启动时与运维脚本都调它。"""
        await asyncio.to_thread(self._ensure_schema_sync)
        return await asyncio.to_thread(
            self._rebuild_all_sync, data_db_path=data_db_path)

    def _sync_stats_from_facts_sync(self, indicators: list[str]) -> int:
        """批量回填运行时状态（供 rebuild_all 复用，避免重复 SELECT）。"""
        return self._refresh_stats_from_facts_sync(indicators)

    # ---------- 单指标更新（fetch 完成后）----------

    def _mark_updated_sync(self, indicator: str, *, period_date: str | None,
                           fetch_time_ms: int, row_count_delta: int = 0) -> None:
        """fetch 完成后调用：更新 last_* 与 row_count。"""
        now_ms = int(time.time() * 1000)
        with self._connect() as conn:
            # row_count_delta 是本批新增条数；first_seen_at 保留最小值
            conn.execute(
                """
                UPDATE indicator_catalog SET
                    last_period_date = COALESCE(?, last_period_date),
                    last_fetch_time_ms = CASE
                        WHEN ? > last_fetch_time_ms THEN ?
                        ELSE last_fetch_time_ms
                    END,
                    row_count = MAX(0, row_count + ?),
                    first_seen_at = CASE
                        WHEN first_seen_at = 0 THEN ?
                        ELSE first_seen_at
                    END,
                    updated_at = ?
                WHERE indicator = ?
                """,
                (period_date, fetch_time_ms, fetch_time_ms, row_count_delta,
                 now_ms, now_ms, indicator),
            )

    async def mark_updated(self, indicator: str, *, period_date: str | None,
                           fetch_time_ms: int, row_count_delta: int = 0) -> None:
        await asyncio.to_thread(
            self._mark_updated_sync, indicator,
            period_date=period_date, fetch_time_ms=fetch_time_ms,
            row_count_delta=row_count_delta,
        )

    # ---------- 读取 ----------

    def _get_sync(self, indicator: str) -> CatalogEntry | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM indicator_catalog WHERE indicator = ?",
                (indicator,)).fetchone()
        if row is None:
            return None
        return self._row_to_entry(row)

    async def get(self, indicator: str) -> CatalogEntry | None:
        await asyncio.to_thread(self._ensure_schema_sync)
        return await asyncio.to_thread(self._get_sync, indicator)

    def _all_sync(self, *, only_enabled: bool = True) -> list[CatalogEntry]:
        sql = "SELECT * FROM indicator_catalog"
        params: tuple[Any, ...] = ()
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY indicator"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_entry(r) for r in rows]

    async def all(self, *, only_enabled: bool = True) -> list[CatalogEntry]:
        await asyncio.to_thread(self._ensure_schema_sync)
        return await asyncio.to_thread(self._all_sync, only_enabled=only_enabled)

    def _bulk_get_sync(self, indicators: list[str]) -> dict[str, CatalogEntry]:
        """批量查询（一次 SELECT，给 SmartFetcher 一次拿全 freshness）。"""
        if not indicators:
            return {}
        placeholders = ",".join("?" * len(indicators))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM indicator_catalog WHERE indicator IN ({placeholders})",
                indicators,
            ).fetchall()
        return {r["indicator"]: self._row_to_entry(r) for r in rows}

    async def bulk_get(self, indicators: list[str]) -> dict[str, CatalogEntry]:
        await asyncio.to_thread(self._ensure_schema_sync)
        return await asyncio.to_thread(self._bulk_get_sync, indicators)

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> CatalogEntry:
        # 兼容旧库（未跑 migration 时缺 storage_* 列）
        keys = row.keys()

        def _get(col: str, default: str = "") -> str:
            return row[col] if col in keys else default

        return CatalogEntry(
            indicator=row["indicator"],
            category=row["category"],
            frequency=row["frequency"],
            freshness_hours=float(row["freshness_hours"]),
            primary_source=row["primary_source"],
            source_url=row["source_url"],
            enabled=bool(row["enabled"]),
            ttl_days=int(row["ttl_days"]),
            units=row["units"],
            notes=row["notes"],
            last_period_date=row["last_period_date"],
            last_fetch_time_ms=int(row["last_fetch_time_ms"]),
            row_count=int(row["row_count"]),
            first_seen_at=int(row["first_seen_at"]),
            updated_at=int(row["updated_at"]),
            storage_backend=_get("storage_backend", "sqlite:fact_data_points"),
            storage_location=_get("storage_location", ""),
            storage_note=_get("storage_note", ""),
        )

    # ---------- 工具：基于数据点更新索引 ----------

    async def update_from_points(
        self, points: list[DataPoint], indicator: str
    ) -> None:
        """从一批数据点反推 last_period_date / last_fetch_time / row_count。

        ⚠️ `row_count` 是**累加**语义（delta）。反复对同一批点调用会重复累加。
        需要幂等精确值时请用 `refresh_stats_from_facts(indicator)` ——
        它直接对事实表做 `COUNT(*)` / `MAX(period_date)`，永远准。
        """
        if not points:
            return
        # 取最大 period_date 与 fetch_time
        max_pd = max(
            (str(p.period_date) for p in points if p.period_date),
            default=None,
        )
        max_ft = max(
            (int(p.fetch_time.timestamp() * 1000) for p in points if p.fetch_time),
            default=int(time.time() * 1000),
        )
        await self.mark_updated(
            indicator,
            period_date=max_pd,
            fetch_time_ms=max_ft,
            row_count_delta=len(points),
        )

    # ---------- 幂等重算（推荐：落库后回填索引用这个）----------

    def _refresh_stats_from_facts_sync(
        self, indicators: list[str], *, infer_unknown: bool = True,
    ) -> int:
        """从 `fact_data_points` 重算 row_count / last_period_date（幂等）。

        为什么不用 `update_from_points` 累加：同一次投研任务里 collect 阶段
        可能被调用多次（重试/懒批），累加会让 row_count 越滚越大；
        而索引表的 row_count 只用于"是否有数据"的判断，虚高会导致
        SmartFetcher 误判 fresh。直接对事实表聚合永远准，且
        `idx_points_indicator_period` 索引让它是一次 index range scan。

        ## ★ 2026-09-28 第十三轮：`infer_unknown` —— 从数据本身修正频率

        自动登记的指标（不在 `configs/indicators.yaml` 里的 709 个）原先
        一律用兜底值 `daily / 24h`。对季度财务数据（`资产负债率:300308`）
        这意味着**永远 stale → 每次都联网**，与"优先本地取"完全相反。

        `infer_unknown=True` 时，对**频率仍为兜底值**的指标，用
        `period_date` 序列的中位间隔反推真实频率并回写。
        已人工声明过频率的指标（来自 YAML）**不动** —— 人工声明优先。
        """
        if not indicators:
            return 0
        placeholders = ",".join("?" for _ in indicators)
        now_ms = int(time.time() * 1000)
        updated = 0
        inserted = 0
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT indicator,
                       COUNT(*) AS n,
                       MAX(COALESCE(period_date, '')) AS max_pd
                FROM fact_data_points
                WHERE indicator IN ({placeholders})
                GROUP BY indicator
                """,
                list(indicators),
            ).fetchall()
            # ★★ 2026-09-30：索引里**没有行**的指标要**补建**，不能只 UPDATE。
            #
            # 实测缺陷（本轮 FRED 补采时抓到）：本函数原先**只有 UPDATE** ⇒
            # 一个"新登记 + 新采集 + 事实表已 943 行"的指标（`fred:UNRATE`）
            # 在 `indicator_catalog` 里**依然没有行**（实测该表零行）⇒
            #   ① 维护审计永远把它报成 `missing`（理由写着"从未采到"，
            #      与事实**相反**）；
            #   ② "补采 → 回填索引"这条闭环（`catalog_collection` 与
            #      `gap_drain` 都调本函数）对**任何新指标**静默无效 ——
            #      正是本项目登记过的"落库了但索引没更 = 白落库"。
            # 补建的行 `primary_source` 取默认空串 ⇒ 紧随其后的
            # `_infer_frequencies_sync` 会用**真实期间间隔**推断频率
            # （而不是硬编码 daily）。
            existing = {r["indicator"] for r in conn.execute(
                f"SELECT indicator FROM indicator_catalog "
                f"WHERE indicator IN ({placeholders})",
                list(indicators)).fetchall()}
            for row in rows:
                if row["indicator"] not in existing:
                    conn.execute(
                        """
                        INSERT INTO indicator_catalog (
                            indicator, row_count, last_period_date,
                            last_fetch_time_ms, first_seen_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (row["indicator"], int(row["n"]),
                         (row["max_pd"] or None), now_ms, now_ms, now_ms),
                    )
                    inserted += 1
                    updated += 1
                    continue
                conn.execute(
                    """
                    UPDATE indicator_catalog SET
                        row_count = ?,
                        last_period_date = CASE
                            WHEN ? != '' THEN ?
                            ELSE last_period_date
                        END,
                        last_fetch_time_ms = CASE
                            WHEN ? > last_fetch_time_ms THEN ?
                            ELSE last_fetch_time_ms
                        END,
                        first_seen_at = CASE
                            WHEN first_seen_at = 0 THEN ?
                            ELSE first_seen_at
                        END,
                        updated_at = ?
                    WHERE indicator = ?
                    """,
                    (int(row["n"]), row["max_pd"], row["max_pd"],
                     now_ms, now_ms, now_ms, now_ms, row["indicator"]),
                )
                updated += 1
            # 事实表里完全没有的指标：把 row_count 归零（诚实反映"没数据"）
            found = {r["indicator"] for r in rows}
            for ind in indicators:
                if ind in found:
                    continue
                conn.execute(
                    "UPDATE indicator_catalog SET row_count = 0, updated_at = ? "
                    "WHERE indicator = ?",
                    (now_ms, ind),
                )
                updated += 1

        if infer_unknown:
            updated += self._infer_frequencies_sync(indicators)
        if inserted:
            logger.info("索引补建：%d 个指标原先在 indicator_catalog 里没有行"
                        "（只有 UPDATE 的旧实现会让它们永远被报成 missing）",
                        inserted)
        return updated

    def _infer_frequencies_sync(self, indicators: list[str]) -> int:
        """对**自动登记**的指标，用数据间隔反推真实频率并回写。

        ## 判据：怎么识别"自动登记"

        用 `primary_source = ''` 作为指纹 —— YAML 里声明的指标**必须**填
        `primary_source`（schema 校验会拦），自动登记的则是空串。
        人工声明的指标**一律不动**（人工优先于推断）。

        ## ⚠️ 为什么不能只匹配 `daily/24h`（2026-09-28 踩坑）

        最初的条件是 `frequency='daily' AND freshness_hours=24`。
        但推断本身**会改变这两个字段** —— 一旦某个指标被（错误地）
        推成 `realtime/0.5h`，它就**再也匹配不上**这个条件，
        永远卡在错误频率上（实测：日频数据被判 realtime → 永久 stale →
        fresh 占比从 752/769 崩到 41/769）。

        改用 `primary_source=''` 后，自动登记的指标**每次重建都会重新推断**，
        错了能自愈。
        """
        from src.infrastructure.catalog.frequency_infer import (
            infer_frequency_from_periods,
        )

        if not indicators:
            return 0
        placeholders = ",".join("?" for _ in indicators)
        with self._connect() as conn:
            candidates = [r["indicator"] for r in conn.execute(
                f"SELECT indicator FROM indicator_catalog "
                f"WHERE indicator IN ({placeholders}) "
                f"AND (primary_source IS NULL OR primary_source = '')",
                list(indicators)).fetchall()]
            if not candidates:
                return 0
            changed = 0
            for ind in candidates:
                periods = [r["period_date"] for r in conn.execute(
                    "SELECT period_date FROM fact_data_points "
                    "WHERE indicator = ? ORDER BY period_date", (ind,)).fetchall()]
                freq, fresh, n = infer_frequency_from_periods(periods)
                if freq == "unknown" or n < 2:
                    continue      # 证据不足 → 不猜（AGENTS.md 纪律）
                # 与现状一致就不写（避免无意义的 UPDATE 与日志噪音）
                cur = conn.execute(
                    "SELECT frequency, freshness_hours FROM indicator_catalog "
                    "WHERE indicator = ?", (ind,)).fetchone()
                if cur and cur["frequency"] == freq and \
                        abs(float(cur["freshness_hours"]) - fresh) < 1e-6:
                    continue
                conn.execute(
                    "UPDATE indicator_catalog SET frequency = ?, "
                    "freshness_hours = ?, updated_at = ? WHERE indicator = ?",
                    (freq, fresh, int(time.time() * 1000), ind),
                )
                changed += 1
        if changed:
            logger.info(
                "频率推断：修正 %d 个自动登记指标（原先一律 daily/24h，"
                "对月/季频数据等于「永远联网」）", changed)
        return changed

    async def refresh_stats_from_facts(self, indicators: list[str]) -> int:
        """从事实表重算索引（幂等）。返回更新的指标数。"""
        await asyncio.to_thread(self._ensure_schema_sync)
        return await asyncio.to_thread(
            self._refresh_stats_from_facts_sync, indicators)


# ============ 进程内单例 ============

_CATALOG_REPO: CatalogRepository | None = None


def get_catalog_repository(db_path: str | None = None) -> CatalogRepository:
    """获取 CatalogRepository 单例。"""
    global _CATALOG_REPO
    if _CATALOG_REPO is None:
        _CATALOG_REPO = CatalogRepository(db_path=db_path)
    return _CATALOG_REPO


def reset_catalog_repo_for_test() -> None:
    """清空单例（**仅测试用**）。"""
    global _CATALOG_REPO
    _CATALOG_REPO = None