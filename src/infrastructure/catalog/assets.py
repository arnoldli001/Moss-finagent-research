"""数据资产登记表（data_asset_catalog）—— 回答"本地到底有哪些数据"。

## 为什么需要它（用户实测打脸后的修正）

原实现只给**一张表**（`fact_data_points`）建了指标索引，而本机实际有：
    · `moss_finagent.db`  **50 张表 / 2.4 GB**
    · `data/quant/`       **35,855 文件 / 17.3 GB**
其中 `sector_crowding_daily`（218 万行）**比 `fact_data_points`（199 万行）还大**，
却完全没进索引 —— 用户说的"行业数据、产业数据、股市数据"全漏了。

## 分层设计（不要混为一谈）

| 层 | 表 | 粒度 | 回答什么 |
|---|---|---|---|
| **资产层** | `data_asset_catalog`（本模块） | 表 / 目录 | "本地有哪些数据、多新、多大、什么周期更新" |
| **指标层** | `indicator_catalog` | 单个指标 | "这个指标在哪、新不新、能不能跳过联网" |

指标层回答"取数决策"，资产层回答"本地家底"。前者是后者的一个视图。

## 数据从哪来（自动扫描，不手写）

    · SQLite：遍历 `sqlite_master`，逐表 COUNT(*) + MAX(时间列)
    · 文件目录：递归统计文件数 / 体积 / 扩展名分布

**不手写清单** —— 手写的清单必然与实际漂移（AGENTS.md：
「同一个 key 写在 N 处，必有一处被漏改」）。扫描是唯一事实源。
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.infrastructure.repositories.event_sqlite_base import connect_sqlite

logger = logging.getLogger(__name__)

#: 表的**角色**——决定它该不该参与"取数路由"（用户 2026-09-28 指出）
#:
#: ## 为什么要分角色
#:
#: 用户原话：
#: > 「sector_crowding_daily 这个表应该不需要加索引」
#:
#: 他是对的。索引的目的是回答**"采集时该取本地还是联网"**，
#: 而这个问题的前提是"这张表里的数据**是从外部采集来的**"。
#:
#: | 角色 | 含义 | 要不要取数路由 | 例 |
#: |---|---|---|---|
#: | `source` | **采集目标**：数据来自外部源，可被联网取到 | ✅ 要 | `fact_data_points`、`data/quant/prices` |
#: | `derived` | **内部计算产物**：由本系统算出来，不存在"联网取"这个选项 | ❌ 不要 | `sector_crowding_daily`（218 万行拥挤度明细）、`mainline_score` |
#: | `dim` | **维度/映射表**：低频变更的枚举数据 | ⚠️ 仅登记不路由 | `dim_concept`、`map_stock_concept` |
#: | `ops` | **运维/用户表**：与投研数据无关 | ❌ 不要 | `fact_session`、`dim_user` |
#:
#: **误把 `derived` 当 `source` 的代价**：给一张 218 万行的内部表
#: 判"stale → 联网"，而它**根本没有联网源** → 每次请求都白跑一次
#: 必然失败的网络调用（本项目已因同类问题损失过 23 秒/次）。
#:
#: 判定规则（按表名前缀，人工维护 —— 扫描拿不到"这表从哪来"）：
TABLE_ROLE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 内部计算产物：由本系统的计算作业写出，无外部对应源
    ("derived", (
        "sector_crowding_daily", "sector_crowding_metric",
        "sector_crowding_max_ma5", "mainline_score", "mainline_alert",
        "mainline_backtest", "mainline_etf_flow_backtest",
        "mainline_mapping_calibration", "auction_feature", "auction_pick",
        "auction_run", "auction_watch_score", "fact_quant_selection",
        "quant_selection_item", "sector_crowding_alert",
    )),
    # 运维/用户
    ("ops", (
        "dim_user", "fact_session", "fact_remember_token",
        "fact_password_reset", "fact_verification_code",
        "fact_registration_review", "user_alert_read", "fact_notify",
        "fact_alert", "run_log",
    )),
    # 维度/映射
    ("dim", ("dim_", "map_", "sector_meta", "sector_member",
             "sector_crowding_list", "sector_crowding_watch",
             "sector_crowding_metric_meta", "schema", "catalog")),
    # 其余默认按采集目标（含 fact_data_points / news_cache / fact_events）
    ("source", ()),
)


def classify_role(table_name: str) -> str:
    """判定表的角色（source / derived / dim / ops）。

    默认 `source` —— 宁可多登记也不要漏掉真正的采集目标
    （漏登记 = 该走本地却联网，代价是每次白等网络）。
    """
    low = table_name.lower()
    for role, keys in TABLE_ROLE_RULES:
        if any(k in low for k in keys):
            return role
    return "source"


#: 数据分类（用于"宏观/行业/产业/股市"这类人话查询）
#: 判定顺序即优先级，先命中先得
#:
#: ⚠️ 顺序有讲究（测试 `test_classify_covers_real_tables` 守着）：
#: `sector_member`（板块成分股）属于**行业结构**，不能因为名字里有
#: "sector" 就被 market 抢走 —— 所以 market 规则里**不列它**，
#: 让它落到 industry 的 `sector_` 前缀上。
CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 元数据 / 运维（最先判，避免被业务词误命中）
    ("meta", ("dim_", "map_", "schema", "catalog", "run_log", "sqlite_")),
    ("ops", ("user_", "auth", "session", "notify", "alert_read",
             "password", "remember", "registration", "verification")),
    # 时间序列事实（行情/拥挤度/主线 —— 高频、大表）
    ("market", ("fact_data_points", "sector_crowding_daily",
                "sector_crowding_metric", "sector_crowding_max_ma5",
                "mainline_score", "mainline_alert", "auction_",
                "quant_selection")),
    # 行业结构（板块构成、概念映射）
    ("industry", ("sector_", "map_stock_concept", "dim_concept")),
    # 信息/事件
    ("news", ("news_cache", "fact_events", "fact_alerts")),
)

#: 分类的中文说明（给运维页/审计报告用）
CATEGORY_LABELS: dict[str, str] = {
    "macro": "宏观",
    "industry": "行业",
    "market": "股市",
    "quant": "量化",
    "news": "信息",
    "meta": "元数据/维度",
    "ops": "运维/用户",
    "other": "其它",
}

#: 各表的更新周期与新鲜度阈值（**人工语义**，扫描拿不到）
#:
#: 扫描只能拿到"最新数据时间"，拿不到"应该多久更新一次"。
#: 这张表是**唯一权威**，新增表必须在这里登记（否则审计会报"未声明周期"）。
TABLE_FREQUENCY: dict[str, tuple[str, float, str]] = {
    # 表名 → (frequency, freshness_hours, 说明)
    "fact_data_points": ("realtime", 0.5, "统一数据点事实表（按指标各自周期）"),
    "sector_crowding_daily": ("daily", 26, "板块拥挤度日频明细"),
    "sector_crowding_metric": ("daily", 26, "板块拥挤度指标（周频计算，日频检查）"),
    "sector_crowding_max_ma5": ("daily", 26, "板块 MA5 截面（日频刷新）"),
    "sector_crowding_list": ("weekly", 168, "拥挤度板块清单"),
    "sector_member": ("weekly", 168, "板块成分股"),
    "sector_meta": ("weekly", 168, "板块元数据"),
    "mainline_score": ("daily", 26, "主线打分"),
    "mainline_alert": ("daily", 26, "主线预警"),
    "map_stock_concept": ("weekly", 168, "个股-概念映射"),
    "dim_concept": ("weekly", 168, "概念维度表"),
    "fact_events": ("intraday", 4, "事件表"),
    "fact_alerts": ("intraday", 4, "告警表"),
    "news_cache": ("intraday", 4, "新闻缓存"),
    "auction_snapshot": ("daily", 26, "竞价快照"),
    "auction_pick": ("daily", 26, "竞价选股结果"),
    "auction_feature": ("daily", 26, "竞价特征"),
    "fact_quant_selection": ("daily", 26, "量化选股记录"),
    "mainline_etf_flow_signal": ("daily", 26, "ETF 资金流信号"),
}

#: 文件目录的更新周期
DIR_FREQUENCY: dict[str, tuple[str, float, str]] = {
    "data/quant/prices": ("daily", 26, "全市场前复权日线（PriceStore 落盘）"),
    "data/quant": ("daily", 26, "量化数据集（parquet/因子/中间产物）"),
    "data/llm_cache": ("realtime", 0.5, "LLM 响应缓存"),
    "data/dynamic_connectors": ("monthly", 720, "自修复生成的动态连接器"),
}


@dataclass
class DataAsset:
    """一条数据资产登记。"""

    asset_id: str            # "sqlite:moss_finagent.db:fact_data_points"
    kind: str                # sqlite_table | file_dir
    location: str            # 物理位置（表名 / 相对路径）
    category: str
    description: str
    row_count: int = 0
    size_bytes: int = 0
    file_count: int = 0
    time_column: str = ""
    latest_time: str = ""
    frequency: str = "unknown"
    freshness_hours: float = 24.0
    last_scanned_at: int = 0
    #: ★ 角色：source（采集目标）/ derived（内部计算）/ dim（维度）/ ops（运维）
    #: 决定它**该不该参与"取本地还是联网"的路由**。见 TABLE_ROLE_RULES。
    role: str = "source"
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id, "kind": self.kind,
            "location": self.location, "category": self.category,
            "role": self.role,
            "description": self.description,
            "row_count": self.row_count, "size_bytes": self.size_bytes,
            "file_count": self.file_count,
            "time_column": self.time_column, "latest_time": self.latest_time,
            "frequency": self.frequency,
            "freshness_hours": self.freshness_hours,
            "last_scanned_at": self.last_scanned_at,
            "extras": self.extras,
        }


_ASSET_SCHEMA = """
CREATE TABLE IF NOT EXISTS data_asset_catalog (
    asset_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    location TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other',
    -- ★ 角色：source（采集目标）/ derived（内部计算）/ dim / ops
    --   决定该资产是否参与"取本地还是联网"的路由
    role TEXT NOT NULL DEFAULT 'source',
    description TEXT NOT NULL DEFAULT '',
    row_count INTEGER NOT NULL DEFAULT 0,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    time_column TEXT NOT NULL DEFAULT '',
    latest_time TEXT NOT NULL DEFAULT '',
    frequency TEXT NOT NULL DEFAULT 'unknown',
    freshness_hours REAL NOT NULL DEFAULT 24.0,
    last_scanned_at INTEGER NOT NULL DEFAULT 0,
    extras_json TEXT NOT NULL DEFAULT '{}'
);
"""

#: ★ 索引 DDL 必须**在迁移补列之后**执行。
#: 反过来的话，"为 role 建索引"会在旧库上失败（那一列还不存在）——
#: 本项目在 `catalog_repo.py` 已踩过一次同样的坑
#: （`no such column: storage_backend`）。
_ASSET_SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_asset_category
    ON data_asset_catalog(category, kind);
CREATE INDEX IF NOT EXISTS idx_asset_latest
    ON data_asset_catalog(latest_time);
-- 按角色索引：路由只关心 role='source'
CREATE INDEX IF NOT EXISTS idx_asset_role
    ON data_asset_catalog(role, category);
"""

#: 旧库补 `role` 列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）
_ASSET_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("role",
     "ALTER TABLE data_asset_catalog ADD COLUMN "
     "role TEXT NOT NULL DEFAULT 'source'"),
)

#: 时间列候选（按优先级：越靠前越能代表"数据本身的时间"）
_TIME_COLUMN_CANDIDATES = (
    "period_date", "trade_date", "publish_time", "fetch_time",
    "trigger_time", "latest_publish_time", "updated_at", "created_at",
)


def classify(table_or_path: str) -> str:
    """按规则给人话分类（macro/industry/market/quant/news/meta/ops/other）。"""
    low = table_or_path.lower()
    for cat, keys in CATEGORY_RULES:
        if any(k in low for k in keys):
            return cat
    return "other"


class DataAssetCatalog:
    """数据资产登记表的读写 + 扫描。"""

    def __init__(self, db_path: str | None = None,
                 project_root: str | Path | None = None) -> None:
        from src.core.config import get_settings

        settings = get_settings()
        self._db_path = db_path or settings.sqlite_path
        self._root = Path(project_root or Path(__file__).resolve().parents[3])

    # ---------- schema ----------

    def _ensure_schema_sync(self) -> None:
        """建表 → 迁移补列 → 建索引（**顺序不能反**）。"""
        with connect_sqlite(self._db_path) as conn:
            conn.executescript(_ASSET_SCHEMA)
            existing = {r["name"] for r in conn.execute(
                "PRAGMA table_info(data_asset_catalog)").fetchall()}
            for col, ddl in _ASSET_MIGRATIONS:
                if col not in existing:
                    conn.execute(ddl)
            conn.executescript(_ASSET_SCHEMA_INDEXES)

    async def ensure_schema(self) -> None:
        await asyncio.to_thread(self._ensure_schema_sync)

    # ---------- 扫描：SQLite ----------

    def _scan_sqlite_sync(self) -> list[DataAsset]:
        """扫描主库的**全部**表（排除 sqlite_ 内部表）。"""
        assets: list[DataAsset] = []
        db_name = Path(self._db_path).name
        now_ms = int(time.time() * 1000)
        with connect_sqlite(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            for t in tables:
                name = t["name"]
                cols = [r["name"] for r in conn.execute(
                    f'PRAGMA table_info("{name}")').fetchall()]
                # 选时间列
                time_col = next(
                    (c for c in _TIME_COLUMN_CANDIDATES if c in cols), "")
                try:
                    if time_col:
                        row = conn.execute(
                            f'SELECT COUNT(*) AS n, MAX("{time_col}") AS mx '
                            f'FROM "{name}"').fetchone()
                        n, mx = row["n"], str(row["mx"] or "")
                    else:
                        n = conn.execute(
                            f'SELECT COUNT(*) AS n FROM "{name}"').fetchone()["n"]
                        mx = ""
                except sqlite3.Error as exc:
                    logger.debug("扫描表 %s 失败: %s", name, exc)
                    n, mx = 0, ""
                freq, fresh_h, desc = TABLE_FREQUENCY.get(
                    name, ("unknown", 24.0, ""))
                assets.append(DataAsset(
                    asset_id=f"sqlite:{db_name}:{name}",
                    kind="sqlite_table", location=name,
                    category=classify(name),
                    role=classify_role(name),
                    description=desc or f"表 {name}",
                    row_count=int(n or 0), time_column=time_col,
                    latest_time=mx, frequency=freq,
                    freshness_hours=fresh_h,
                    last_scanned_at=now_ms,
                    extras={"column_count": len(cols),
                            "columns": cols[:40]},
                ))
        return assets

    # ---------- 扫描：文件目录 ----------

    def _scan_dir_sync(self, rel: str, *, max_files: int = 60_000) -> DataAsset | None:
        """扫描一个目录（文件数 / 体积 / 扩展名分布 / 最新 mtime）。"""
        path = self._root / rel
        if not path.exists():
            return None
        n, total, latest = 0, 0, 0.0
        exts: dict[str, int] = {}
        for f in path.rglob("*"):
            if not f.is_file():
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            n += 1
            total += st.st_size
            latest = max(latest, st.st_mtime)
            exts[f.suffix] = exts.get(f.suffix, 0) + 1
            if n >= max_files:
                break
        freq, fresh_h, desc = DIR_FREQUENCY.get(rel, ("unknown", 24.0, ""))
        latest_iso = ""
        if latest:
            from datetime import datetime
            latest_iso = datetime.fromtimestamp(latest).strftime(
                "%Y-%m-%dT%H:%M:%S")
        return DataAsset(
            asset_id=f"file:{rel}",
            kind="file_dir", location=rel,
            category="quant" if "quant" in rel else classify(rel),
            # 目录类默认 source（落盘行情是采集目标）；LLM 缓存属运维
            role=("ops" if "cache" in rel or "connector" in rel else "source"),
            description=desc or f"目录 {rel}",
            size_bytes=total, file_count=n,
            time_column="mtime", latest_time=latest_iso,
            frequency=freq, freshness_hours=fresh_h,
            last_scanned_at=int(time.time() * 1000),
            extras={"extensions": dict(sorted(
                exts.items(), key=lambda kv: -kv[1])[:10])},
        )

    # ---------- 全量扫描 + 落库 ----------

    def _scan_all_sync(self) -> list[DataAsset]:
        assets = self._scan_sqlite_sync()
        for rel in DIR_FREQUENCY:
            a = self._scan_dir_sync(rel)
            if a is not None:
                assets.append(a)
        return assets

    def _upsert_assets_sync(self, assets: list[DataAsset]) -> int:
        import json

        with connect_sqlite(self._db_path) as conn:
            for a in assets:
                conn.execute(
                    """
                    INSERT INTO data_asset_catalog (
                        asset_id, kind, location, category, role, description,
                        row_count, size_bytes, file_count, time_column,
                        latest_time, frequency, freshness_hours,
                        last_scanned_at, extras_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(asset_id) DO UPDATE SET
                        kind=excluded.kind, location=excluded.location,
                        category=excluded.category,
                        role=excluded.role,
                        description=excluded.description,
                        row_count=excluded.row_count,
                        size_bytes=excluded.size_bytes,
                        file_count=excluded.file_count,
                        time_column=excluded.time_column,
                        latest_time=excluded.latest_time,
                        frequency=excluded.frequency,
                        freshness_hours=excluded.freshness_hours,
                        last_scanned_at=excluded.last_scanned_at,
                        extras_json=excluded.extras_json
                    """,
                    (a.asset_id, a.kind, a.location, a.category, a.role,
                     a.description,
                     a.row_count, a.size_bytes, a.file_count, a.time_column,
                     a.latest_time, a.frequency, a.freshness_hours,
                     a.last_scanned_at,
                     json.dumps(a.extras, ensure_ascii=False)),
                )
        return len(assets)

    async def scan_and_store(self) -> dict[str, Any]:
        """全量扫描并落库。返回统计（供审计脚本断言）。"""
        await self.ensure_schema()
        assets = await asyncio.to_thread(self._scan_all_sync)
        n = await asyncio.to_thread(self._upsert_assets_sync, assets)
        by_cat: dict[str, int] = {}
        by_role: dict[str, int] = {}
        for a in assets:
            by_cat[a.category] = by_cat.get(a.category, 0) + 1
            by_role[a.role] = by_role.get(a.role, 0) + 1
        total_rows = sum(a.row_count for a in assets)
        total_bytes = sum(a.size_bytes for a in assets)
        logger.info(
            "数据资产扫描完成：%d 个资产（%d 张表 + %d 个目录），"
            "合计 %s 行 / %.1f MB；角色分布 %s",
            n, sum(1 for a in assets if a.kind == "sqlite_table"),
            sum(1 for a in assets if a.kind == "file_dir"),
            f"{total_rows:,}", total_bytes / 1024 / 1024, by_role)
        return {
            "assets": n, "by_category": by_cat, "by_role": by_role,
            "routing_targets": sum(1 for a in assets if a.role == "source"),
            "total_rows": total_rows, "total_bytes": total_bytes,
        }

    # ---------- 查询 ----------

    def _list_sync(self, *, category: str = "",
                   kind: str = "", role: str = "") -> list[dict[str, Any]]:
        import json

        sql = "SELECT * FROM data_asset_catalog WHERE 1=1"
        params: list[Any] = []
        if category:
            sql += " AND category = ?"
            params.append(category)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if role:
            sql += " AND role = ?"
            params.append(role)
        sql += " ORDER BY row_count DESC, size_bytes DESC"
        with connect_sqlite(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["extras"] = json.loads(d.pop("extras_json", "{}"))
            except (ValueError, TypeError):
                d["extras"] = {}
            out.append(d)
        return out

    async def list_assets(self, *, category: str = "", kind: str = "",
                          role: str = "") -> list[dict[str, Any]]:
        await self.ensure_schema()
        return await asyncio.to_thread(
            self._list_sync, category=category, kind=kind, role=role)

    def routing_targets(self) -> list[dict[str, Any]]:
        """**只返回采集目标**（role='source'）—— 取数路由只该关心这些。

        用户 2026-09-28 指出：`sector_crowding_daily`（218 万行拥挤度明细）
        是**内部计算产物**，没有联网源，给它判 stale → 联网是纯浪费。
        本方法把 `derived` / `dim` / `ops` 全部排除。
        """
        import json

        with connect_sqlite(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM data_asset_catalog WHERE role = 'source' "
                "ORDER BY row_count DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["extras"] = json.loads(d.pop("extras_json", "{}"))
            except (ValueError, TypeError):
                d["extras"] = {}
            out.append(d)
        return out

    def stats(self) -> dict[str, Any]:
        """汇总统计（供 /health 与审计）。**按角色分组**，因为角色决定用途。"""
        with connect_sqlite(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT category, kind, role, COUNT(*) AS n, "
                "SUM(row_count) AS rows_, SUM(size_bytes) AS bytes_ "
                "FROM data_asset_catalog GROUP BY category, kind, role"
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM data_asset_catalog").fetchone()["n"]
        by_cat: dict[str, dict[str, int]] = {}
        by_role: dict[str, int] = {}
        for r in rows:
            d = by_cat.setdefault(r["category"], {"assets": 0, "rows": 0,
                                                  "bytes": 0})
            d["assets"] += r["n"]
            d["rows"] += int(r["rows_"] or 0)
            d["bytes"] += int(r["bytes_"] or 0)
            by_role[r["role"]] = by_role.get(r["role"], 0) + r["n"]
        return {"total_assets": total, "by_category": by_cat,
                "by_role": by_role}


# ============ 单例 ============

_ASSET_CATALOG: DataAssetCatalog | None = None


def get_asset_catalog() -> DataAssetCatalog:
    global _ASSET_CATALOG
    if _ASSET_CATALOG is None:
        _ASSET_CATALOG = DataAssetCatalog()
    return _ASSET_CATALOG


def reset_asset_catalog_for_test() -> None:
    global _ASSET_CATALOG
    _ASSET_CATALOG = None
