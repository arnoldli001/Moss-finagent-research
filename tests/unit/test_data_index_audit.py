"""数据索引体系 · 设计要求审计器自身的测试（自我验证）。

## 为什么审计器也要被测（AGENTS.md 硬约束）

> 「**自己的检查脚本必须先自证**：喂一个"已知答案"的输入，确认它报 0。
>   本轮实测：自写正则报 6 个假告警，差点去修没坏的东西。」

审计器如果本身有 bug，会在两个方向害人：
  · **假绿**：明明没实现却报 PASS → 用户以为做了（这正是用户担心的"改了≠生效了"）
  · **假红**：明明实现对了却报 FAIL → 去修没坏的东西

所以本文件守三件事：
  1. 审计器能跑完 7 条（不崩）
  2. 每条判据**认得出成功**（当前仓库状态应 7/7 PASS）
  3. 每条判据**认得出失败**（构造坏输入 → 必须 FAIL）
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

# 导入审计器（scripts 目录已加入 sys.path）
import audit_data_index as audit  # noqa: E402


# ============================================================
# 1. 审计器能跑完
# ============================================================


async def test_auditor_runs_all_seven_requirements():
    """审计器必须跑完 7 条，且每条都给出证据字符串。"""
    auditor = audit.DataIndexAuditor()
    results = await auditor.run_all()
    assert len(results) == 7, f"应审计 7 条要求，实际 {len(results)}"
    rids = [r.rid for r in results]
    assert rids == ["R1", "R2", "R3", "R4", "R5", "R6", "R7"]
    for r in results:
        assert r.requirement, f"{r.rid} 缺要求描述"
        assert r.evidence, f"{r.rid} 缺证据字符串（空证据 = 无法 review）"


async def test_current_repo_passes_all_requirements():
    """★ 当前仓库状态应 7/7 通过。

    这条红了 = 有人把已实现的能力改回去了（配置漂移哨兵）。
    """
    auditor = audit.DataIndexAuditor()
    results = await auditor.run_all()
    failed = [f"{r.rid}({r.evidence})" for r in results if not r.passed]
    assert not failed, (
        "数据索引设计要求未达标：\n  " + "\n  ".join(failed)
        + "\n→ 跑 `uv run python scripts/audit_data_index.py --fix`"
    )


# ============================================================
# 2. 判据认得出失败（反向测试 —— 最重要的一组）
# ============================================================


async def test_r1_fails_when_asset_table_missing(tmp_path, monkeypatch):
    """R1 必须认得出"缺 data_asset_catalog"。"""
    db = str(tmp_path / "empty.db")
    # 造一个空库（没有两张索引表）
    sqlite3.connect(db).close()

    auditor = audit.DataIndexAuditor()
    monkeypatch.setattr(auditor, "db_path", db)
    r = await auditor.check_r1_index_exists()
    assert r.passed is False, "空库应该报 R1 失败"
    assert "缺" in r.evidence or "覆盖不全" in r.evidence
    assert r.fixable, "R1 失败应可自动修复"


async def test_r1_fails_when_asset_coverage_incomplete(tmp_path, monkeypatch):
    """R1 必须认得出「资产层缺 role 列」（无法区分采集目标与内部计算表）。

    ★ 2026-09-28 语义变更（用户指出）：
    > 「sector_crowding_daily 这个表应该不需要加索引」

    索引要回答的是"采集时取本地还是联网"，前提是"这表的数据来自外部采集"。
    `sector_crowding_daily`（218 万行拥挤度明细）是**内部计算产物**，
    没有联网源 —— 给它判 stale → 联网是纯浪费。

    所以 R1 的判据从"覆盖全部表"改为"覆盖全部 **role='source'** 的表"：
      · source   —— 采集目标，必须登记
      · derived  —— 内部计算，不要求（登记只为运维可见性）
      · dim/ops  —— 不参与路由

    本用例守：**缺 role 列 = 无法区分 = FAIL**（否则会给 derived 表白跑联网）。
    """
    db = str(tmp_path / "norole.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE indicator_catalog (indicator TEXT PRIMARY KEY);
        -- 故意建一个**没有 role 列**的资产表（模拟旧库未迁移）
        CREATE TABLE data_asset_catalog (
            asset_id TEXT PRIMARY KEY, kind TEXT, location TEXT,
            category TEXT, description TEXT, row_count INTEGER,
            size_bytes INTEGER, file_count INTEGER, time_column TEXT,
            latest_time TEXT, frequency TEXT, freshness_hours REAL,
            last_scanned_at INTEGER, extras_json TEXT
        );
        INSERT INTO data_asset_catalog (asset_id, kind, location)
            VALUES ('sqlite:x:fact_data_points', 'sqlite_table', 'fact_data_points');
    """)
    conn.commit()
    conn.close()

    auditor = audit.DataIndexAuditor()
    monkeypatch.setattr(auditor, "db_path", db)
    r = await auditor.check_r1_index_exists()
    assert r.passed is False, "缺 role 列应报失败"
    assert "role" in r.evidence, f"证据里应点名 role 列，实际：{r.evidence}"
    assert r.fixable, "应可自动修复（重扫数据资产）"


async def test_r1_fails_when_no_source_role(tmp_path, monkeypatch):
    """R1 必须认得出「没有任何 role='source' 资产」—— 说明角色分类失效。"""
    db = str(tmp_path / "nosource.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE indicator_catalog (indicator TEXT PRIMARY KEY);
        CREATE TABLE data_asset_catalog (
            asset_id TEXT PRIMARY KEY, kind TEXT, location TEXT,
            category TEXT, role TEXT DEFAULT 'derived',
            description TEXT, row_count INTEGER, size_bytes INTEGER,
            file_count INTEGER, time_column TEXT, latest_time TEXT,
            frequency TEXT, freshness_hours REAL,
            last_scanned_at INTEGER, extras_json TEXT
        );
        -- 全部标成 derived（没有采集目标）→ 说明分类逻辑坏了
        INSERT INTO data_asset_catalog (asset_id, kind, location, role)
            VALUES ('sqlite:x:t1', 'sqlite_table', 't1', 'derived');
    """)
    conn.commit()
    conn.close()

    auditor = audit.DataIndexAuditor()
    monkeypatch.setattr(auditor, "db_path", db)
    r = await auditor.check_r1_index_exists()
    assert r.passed is False, "没有 source 资产应报失败"
    assert "source" in r.evidence


async def test_r1_fails_when_fact_table_not_source(tmp_path, monkeypatch):
    """★ 核心：`fact_data_points` 必须被认成 source。

    它是**唯一的事实表**（199 万行）。若被误标成 derived/dim，
    取数路由就会跳过它 → 每次都联网 → 索引等于白建。
    """
    db = str(tmp_path / "wrongrole.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE indicator_catalog (indicator TEXT PRIMARY KEY);
        CREATE TABLE data_asset_catalog (
            asset_id TEXT PRIMARY KEY, kind TEXT, location TEXT,
            category TEXT, role TEXT, description TEXT, row_count INTEGER,
            size_bytes INTEGER, file_count INTEGER, time_column TEXT,
            latest_time TEXT, frequency TEXT, freshness_hours REAL,
            last_scanned_at INTEGER, extras_json TEXT
        );
        INSERT INTO data_asset_catalog
            (asset_id, kind, location, role)
            VALUES ('sqlite:x:fact_data_points', 'sqlite_table',
                    'fact_data_points', 'derived');
        INSERT INTO data_asset_catalog
            (asset_id, kind, location, role)
            VALUES ('sqlite:x:other', 'sqlite_table', 'other', 'source');
    """)
    conn.commit()
    conn.close()

    auditor = audit.DataIndexAuditor()
    monkeypatch.setattr(auditor, "db_path", db)
    r = await auditor.check_r1_index_exists()
    assert r.passed is False, "fact_data_points 被误标应报失败"
    assert "fact_data_points" in r.evidence


async def test_r2_fails_when_nothing_is_fresh(tmp_path, monkeypatch):
    """R2 必须认得出"没有任何 fresh 指标"（= 索引没起作用）。"""
    import time

    db = str(tmp_path / "stale.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE indicator_catalog (
            indicator TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1,
            row_count INTEGER DEFAULT 0, freshness_hours REAL DEFAULT 24,
            last_fetch_time_ms INTEGER DEFAULT 0
        );
        -- 3 个指标全部 row_count=0（永远 stale）
        INSERT INTO indicator_catalog VALUES ('a', 1, 0, 24, 0);
        INSERT INTO indicator_catalog VALUES ('b', 1, 0, 24, 0);
        INSERT INTO indicator_catalog VALUES ('c', 1, 0, 24, 0);
    """)
    conn.commit()
    conn.close()

    auditor = audit.DataIndexAuditor()
    monkeypatch.setattr(auditor, "db_path", db)
    r = await auditor.check_r2_routing()
    assert r.passed is False, f"全 stale 应报失败，实际 evidence={r.evidence}"
    assert "fresh" in r.evidence.lower() or "快路径" in r.evidence
    del time


async def test_r3_detects_non_batched_implementation(monkeypatch):
    """R3 必须认得出"批量查询退化成了 N 次单查"。"""
    from src.infrastructure.repositories.macro_repo import MacroRepository

    # 造一个"假批量"：内部是 for 循环单查（没有 IN）
    def fake_query_points_batch(self, *a, **k):
        return {}

    def fake_impl(self, indicators, s, e, lim):
        result = {}
        for ind in indicators:              # ← 退化实现
            result[ind] = self._query_sync(ind, s, e)
        return result

    monkeypatch.setattr(MacroRepository, "_query_batch_sync", fake_impl)
    del fake_query_points_batch

    auditor = audit.DataIndexAuditor()
    r = await auditor.check_r3_batch_query()
    assert r.passed is False, "for 循环单查应被 R3 判为失败"
    assert "IN" in r.evidence


async def test_r6_fails_when_no_frequency_mapping(monkeypatch):
    """R6 必须认得出"频率缺 cron 映射"（= 不会定期更新）。"""
    import src.scheduler.catalog_jobs as cj

    # 抹掉一个频率的映射
    broken = dict(cj.FREQUENCY_TO_CRON)
    broken.pop("monthly", None)
    monkeypatch.setattr(cj, "FREQUENCY_TO_CRON", broken)

    auditor = audit.DataIndexAuditor()
    r = await auditor.check_r6_schedule()
    assert r.passed is False, "缺 monthly 映射应报失败"
    assert "cron" in r.evidence.lower() or "映射" in r.evidence


async def test_r7_threshold_is_meaningful():
    """R7 的耗时阈值必须与生产同构（生产传 limit_per_indicator=60）。

    若审计器不传 limit，会拉全量 → 把"审计器写法不对"误报成"生产慢"。
    本测守住"审计器用了与生产相同的参数"。
    """
    import inspect

    src = inspect.getsource(audit.DataIndexAuditor.check_r7_cache_hit)
    assert "limit_per_indicator" in src, (
        "R7 没传 limit_per_indicator —— 会拉全量导致判据失真"
    )


# ============================================================
# 3. 数据资产分类
# ============================================================


def test_classify_covers_real_tables():
    """分类器要能认出真实的四类数据（宏观/行业/股市/运维）。"""
    from src.infrastructure.catalog.assets import classify

    assert classify("fact_data_points") == "market"
    assert classify("sector_crowding_daily") == "market"
    assert classify("sector_member") == "industry"
    assert classify("mainline_score") == "market"
    assert classify("dim_concept") == "meta"
    assert classify("fact_alert_read") == "ops"
    assert classify("something_unknown_xyz") == "other"


def test_asset_scan_finds_big_tables(tmp_path):
    """资产扫描必须能发现"比 fact_data_points 还大"的表。

    这是本轮修复的核心：原索引只覆盖 fact_data_points（199 万行），
    漏了 sector_crowding_daily（218 万行）。
    """
    from src.infrastructure.catalog.assets import DataAssetCatalog

    db = str(tmp_path / "scan.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE fact_data_points (indicator TEXT, period_date TEXT);
        CREATE TABLE sector_crowding_daily (trade_date TEXT, v REAL);
        INSERT INTO sector_crowding_daily VALUES ('2026-09-28', 1.0);
        INSERT INTO sector_crowding_daily VALUES ('2026-09-25', 2.0);
        INSERT INTO fact_data_points VALUES ('CPI', '2026-08');
    """)
    conn.commit()
    conn.close()

    cat = DataAssetCatalog(db_path=db, project_root=tmp_path)
    assets = cat._scan_sqlite_sync()          # noqa: SLF001 直接测扫描
    names = {a.location: a for a in assets}
    assert "fact_data_points" in names
    assert "sector_crowding_daily" in names, "扫描漏了非主事实表"
    assert names["sector_crowding_daily"].row_count == 2
    assert names["sector_crowding_daily"].time_column == "trade_date"
    assert names["sector_crowding_daily"].latest_time == "2026-09-28"


# ============================================================
# 角色分类（用户 2026-09-28 反馈的核心）
# ============================================================


def test_sector_crowding_daily_is_derived_not_source():
    """★ `sector_crowding_daily` 必须是 `derived`（内部计算），不是 `source`。

    用户原话：
    > 「sector_crowding_daily 这个表应该不需要加索引」

    他是对的：这张表（218 万行拥挤度明细）是**本系统算出来的**，
    没有外部对应源。给它做"取本地还是联网"的路由判断毫无意义 ——
    判 stale 之后**根本没有联网源可取**，只会白跑一次必然失败的网络调用。

    这条测试红了 = 有人把 derived 表误标成 source（会给它白跑联网）。
    """
    from src.infrastructure.catalog.assets import classify_role

    assert classify_role("sector_crowding_daily") == "derived"
    assert classify_role("mainline_score") == "derived"
    assert classify_role("auction_pick") == "derived"


def test_fact_data_points_is_source():
    """★ `fact_data_points` 必须是 `source`（采集目标）—— 它是唯一事实表。

    若被误标成 derived，取数路由会跳过它 → 每次都联网 → 索引等于白建。
    """
    from src.infrastructure.catalog.assets import classify_role

    assert classify_role("fact_data_points") == "source"


def test_ops_and_dim_tables_classified():
    """运维/维度表要正确归类（不参与取数路由）。"""
    from src.infrastructure.catalog.assets import classify_role

    assert classify_role("dim_user") == "ops"
    assert classify_role("fact_session") == "ops"
    assert classify_role("dim_concept") == "dim"
    assert classify_role("map_stock_concept") == "dim"


def test_unknown_table_defaults_to_source():
    """未知表默认 `source` —— 宁可多登记，不可漏掉真正的采集目标。

    方向性理由：漏登记 = 该走本地却联网（每次白等网络，本机实测 13.1s）；
    多登记 = 多一行资产记录（零成本）。所以默认值必须偏"source"。
    """
    from src.infrastructure.catalog.assets import classify_role

    assert classify_role("some_brand_new_table") == "source"


async def test_routing_targets_excludes_derived(tmp_path):
    """`routing_targets()` 必须排除 derived —— 取数路由只看采集目标。"""
    from src.infrastructure.catalog.assets import DataAssetCatalog

    db = str(tmp_path / "roles.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE fact_data_points (indicator TEXT, period_date TEXT);
        CREATE TABLE sector_crowding_daily (trade_date TEXT);
        CREATE TABLE dim_user (id TEXT);
    """)
    conn.commit()
    conn.close()

    cat = DataAssetCatalog(db_path=db, project_root=tmp_path)
    await cat.scan_and_store()
    targets = {t["location"] for t in cat.routing_targets()}
    assert "fact_data_points" in targets
    assert "sector_crowding_daily" not in targets, (
        "derived 表不该出现在取数路由目标里"
    )
    assert "dim_user" not in targets
