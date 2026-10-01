"""★ 「补采→索引→审计」这条闭环的两个断点（2026-09-30 实测抓到的）。

## 断点一：索引重建只 UPDATE，不给新指标建行

`_refresh_stats_from_facts_sync` 原先**只有 UPDATE**。于是 `fred:UNRATE`
（新登记 + 已采到 943 行）在 `indicator_catalog` 里**依然零行**（实测）⇒
  * 维护审计永远把它报成 `missing`，理由写着「从未采到」——与事实**相反**；
  * "补采 → 回填索引"这条闭环（`catalog_collection` 与 `gap_drain` 都调它）
    对**任何新指标**静默无效（= "落库了但索引没更 = 白落库"）。

## 断点二：「停产」写在 YAML 里，审计读的却是索引里的副本

把 5 条源已停更的美国宏观改成 `enabled: false` 后，登记表解析正确
（`meta.enabled is False`），但 `indicator_catalog` 里那 5 行的 `enabled` 列
**仍是 1**，而 `repo.all(only_enabled=True)` 过滤的是**索引那一列** ⇒
停产的 5 条照旧每月进报告、每月白取一次。

两个断点都是"同一件事写两处，必有一处漏改"的同一类缺陷，所以护栏也成对写。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, timedelta

# ============================================================================
# 断点一：新指标必须能进索引
# ============================================================================


def _make_repo(tmp_path):
    from src.infrastructure.catalog.catalog_repo import CatalogRepository

    repo = CatalogRepository(db_path=str(tmp_path / "catalog.db"))
    repo._ensure_schema_sync()
    with repo._connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fact_data_points ("
            "indicator TEXT, period_date TEXT, value REAL)")
    return repo


def _seed_facts(repo, rows):
    with repo._connect() as conn:
        conn.executemany(
            "INSERT INTO fact_data_points (indicator, period_date, value) "
            "VALUES (?, ?, ?)", rows)


def _catalog_row(repo, indicator):
    with repo._connect() as conn:
        return conn.execute(
            "SELECT row_count, last_period_date, frequency FROM indicator_catalog "
            "WHERE indicator = ?", (indicator,)).fetchone()


def test_refresh_creates_index_row_for_new_indicator(tmp_path) -> None:
    """★ 事实表有数据、索引里没有行 ⇒ 重建**必须补建**（否则永远报 missing）。"""
    repo = _make_repo(tmp_path)
    _seed_facts(repo, [("fred:UNRATE", "2026-07-01", 4.2),
                       ("fred:UNRATE", "2026-08-01", 4.1)])
    assert _catalog_row(repo, "fred:UNRATE") is None, "前置：索引里本来没有行"

    n = repo._refresh_stats_from_facts_sync(["fred:UNRATE"])

    row = _catalog_row(repo, "fred:UNRATE")
    assert row is not None, "重建后索引必须有行（旧实现只 UPDATE ⇒ 永远没有）"
    assert row["row_count"] == 2
    assert row["last_period_date"] == "2026-08-01"
    assert n >= 1


def test_refresh_still_updates_existing_row_without_duplicating(tmp_path) -> None:
    """反向判据：已有行时是 UPDATE（不新增行、不丢原有运行时态）。"""
    repo = _make_repo(tmp_path)
    with repo._connect() as conn:
        conn.execute(
            "INSERT INTO indicator_catalog (indicator, category, primary_source, "
            "notes, row_count, last_period_date) "
            "VALUES ('CPI', 'macro', 'AkShare', '人工登记的备注', 0, NULL)")
    _seed_facts(repo, [("CPI", "2026-09-01", 0.5)])

    repo._refresh_stats_from_facts_sync(["CPI"])

    with repo._connect() as conn:
        rows = conn.execute(
            "SELECT row_count, last_period_date, notes FROM indicator_catalog "
            "WHERE indicator = 'CPI'").fetchall()
    assert len(rows) == 1, "不许出现重复行"
    assert rows[0]["row_count"] == 1
    assert rows[0]["last_period_date"] == "2026-09-01"
    assert rows[0]["notes"] == "人工登记的备注", "补建逻辑不许覆盖人工登记的元数据"


def test_refresh_keeps_reporting_zero_for_absent_indicator(tmp_path) -> None:
    """反向判据：事实表里**真的没有**的指标不许被补建成"有数据"。"""
    repo = _make_repo(tmp_path)
    repo._refresh_stats_from_facts_sync(["不存在的指标"])
    assert _catalog_row(repo, "不存在的指标") is None, \
        "没有事实就不该建行（否则把'没量到'伪装成'量到了'）"


# ============================================================================
# 断点二：停产（enabled: false）必须以登记表为准
# ============================================================================


class _Meta:
    def __init__(self, indicator, enabled=True):
        self.indicator = indicator
        self.enabled = enabled
        self.frequency = "monthly"

    def is_template(self) -> bool:
        return "{" in self.indicator


class _Registry:
    def __init__(self, metas):
        self._metas = metas

    def all(self):
        return list(self._metas)

    def get(self, indicator):
        for m in self._metas:
            if m.indicator == indicator:
                return m
        return None


class _Entry:
    def __init__(self, indicator, rows, last, freq="monthly"):
        self.indicator, self.row_count = indicator, rows
        self.last_period_date, self.frequency = last, freq


def _run_audit(tmp_path, monkeypatch, *, enabled: bool):
    """跑一次审计：登记表说 enabled 是什么，就喂什么（其余全替身）。"""
    from src.infrastructure.catalog import registry as registry_mod
    from src.scheduler import maintenance

    stale_day = (date.today() - timedelta(days=500)).isoformat()
    entries = [_Entry("us_unemployment", 107, stale_day),
               _Entry("CPI", 229, date.today().isoformat())]

    class _Repo:
        def __init__(self, *a, **k) -> None:
            pass

        async def all(self, only_enabled: bool = True):
            # ★ 索引里的 enabled **永远是 1**（实测就是这样：两处各存一份）
            return entries

        async def refresh_stats_from_facts(self, indicators):
            return 0

    monkeypatch.setattr(
        "src.infrastructure.catalog.catalog_repo.CatalogRepository", _Repo)
    monkeypatch.setattr(registry_mod, "get_registry", lambda: _Registry([
        _Meta("us_unemployment", enabled=enabled),
        _Meta("CPI", enabled=True),
    ]))
    return asyncio.run(maintenance.audit(
        enqueue=False, write_report=False, root=tmp_path))


def test_retired_indicator_is_not_reported(tmp_path, monkeypatch) -> None:
    """★ `enabled: false` 的指标不许进维护报告（哪怕索引里 enabled=1）。"""
    out = _run_audit(tmp_path, monkeypatch, enabled=False)
    flagged = [i["indicator"] for i in out["issues"]]
    assert "us_unemployment" not in flagged, \
        "停产的指标仍在报告里 ⇒ 每月白取一次、噪音淹掉真缺口"
    assert "CPI" not in flagged, "健康的也不许报"


def test_active_indicator_is_still_reported(tmp_path, monkeypatch) -> None:
    """★ 反向判据：没停产时**必须照旧报**（否则这个判据可以靠"全都不报"通过）。"""
    out = _run_audit(tmp_path, monkeypatch, enabled=True)
    flagged = [i["indicator"] for i in out["issues"]]
    assert "us_unemployment" in flagged, "停更 500 天必须报出来"


def test_disabled_is_read_from_registry_not_index() -> None:
    """判据实现位置：审计的 `disabled` 集合必须由**登记表**现读。"""
    import inspect

    from src.scheduler import maintenance

    src = inspect.getsource(maintenance.audit)
    assert "registry.all() if not m.enabled" in src, \
        "停产判据必须从 registry 现读（索引里的 enabled 是副本，会漂移）"


def test_fact_table_is_reachable_after_insert(tmp_path) -> None:
    """自证：本文件的替身库与真库同形（`sqlite3` 直接读得到刚补建的行）。"""
    repo = _make_repo(tmp_path)
    _seed_facts(repo, [("fred:DGS10", "2026-09-25", 5.17)])
    repo._refresh_stats_from_facts_sync(["fred:DGS10"])
    con = sqlite3.connect(str(tmp_path / "catalog.db"))
    try:
        got = con.execute(
            "SELECT COUNT(*) FROM indicator_catalog WHERE indicator='fred:DGS10'"
        ).fetchone()[0]
    finally:
        con.close()
    assert got == 1


# ============================================================================
# 断点三：`X:{code}` 查不到**基名**口径 ⇒ 11 条 freq_mismatch（2026-09-30）
# ============================================================================


def test_get_by_base_inherits_the_registered_base():
    """★ `商誉占净资产比:600036` 必须继承登记在册的基名口径（quarterly）。

    根因：YAML 只登记基名（`notes` 写着"实际指标带 6 位代码后缀"），
    而 `get()` 的精确/通配都要求**段数相同** ⇒ 查不到 ⇒ 兜底 daily
    ⇒ 审计拿 3 天宽限判季频数据（92 天）⇒ 报 `freq_mismatch`（实测 11 条）。
    """
    from src.infrastructure.catalog.registry import get_registry

    reg = get_registry()
    for ind in ("商誉占净资产比:000001", "商誉占净资产比:600036",
                "有息负债占总资产比:600036", "货币资金占总资产比:600519"):
        meta = reg.get_by_base(ind)
        assert meta is not None, f"{ind} 应能查出基名口径"
        assert meta.indicator == ind.split(":", 1)[0]
        assert meta.frequency == "quarterly", (
            f"{ind} 继承了 {meta.frequency} —— 基名登记的是 quarterly")


def test_get_by_base_does_not_fuzzy_match():
    """★ 不许模糊匹配：认不出就返回 None（错配比缺数据更危险）。

    反例：`PE(TTM)` 与 `PE(TTM):同比` 这类必须由**更长者**胜出，
    而不是"像谁算谁"。
    """
    from src.infrastructure.catalog.registry import get_registry

    reg = get_registry()
    assert reg.get_by_base("从没登记过的指标:600036") is None
    assert reg.get_by_base("PE(TTM)") is None, "没有冒号 ⇒ 不是基名+后缀形态"
    assert reg.get_by_base("") is None


def test_unregistered_indicator_defaults_to_monthly_not_daily(tmp_path) -> None:
    """★ 兜底从 `daily/24h` 改成 `monthly/720h`（用户 2026-09-30 口径：

    「**没登记更新周期的数据都触发月频更新一次**」）。

    为什么不能继续 daily：daily 是"每天该更新"的断言 ⇒
    ① 审计拿 3 天宽限判它 ⇒ 一片 `stale` 假告警；
    ② 补采按日重试 ⇒ 对一个没人登记周期的指标每天花一次取数。
    """
    repo = _make_repo(tmp_path)
    _seed_facts(repo, [("从没登记过的指标:600036", "2026-06-30", 1.0)])
    with repo._connect() as conn:
        conn.execute("INSERT OR REPLACE INTO indicator_catalog (indicator) "
                     "VALUES ('从没登记过的指标:600036')")
    import asyncio

    stats = asyncio.run(repo.rebuild_all())
    row = _catalog_row(repo, "从没登记过的指标:600036")
    assert row is not None
    assert row["frequency"] == "monthly", (
        f"未登记周期的指标应是 monthly（月频兜底），实际 {row['frequency']}；"
        f"stats={stats}")
    with repo._connect() as conn:
        fresh = conn.execute("SELECT freshness_hours FROM indicator_catalog "
                             "WHERE indicator='从没登记过的指标:600036'"
                             ).fetchone()[0]
    assert float(fresh) == 720.0
