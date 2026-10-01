"""逐股解禁表（`app_db::unlock_plan`）的护栏。

## 为什么要有这张表（用户 2026-09-29 原话）

> 「按投研分析要查的股票、解禁日期、**解禁数量**落入到数据库**单独的表中**，
>   每月自动调度一次……agent 数据连接**对接这个单独的表**，查询未来一个月
>   这个股的解禁数据。**直接精准哈希就找到了**。」

## 本文件守的六件事

1. **幂等**：同内容重复落库不产生新行、也不动 `fetched_at`（月频作业会重复跑）；
2. **精准点查**：`EXPLAIN QUERY PLAN` 必须是 `SEARCH ... USING INDEX`
   （不是 `SCAN`）—— 这就是用户说的"直接精准哈希就找到了"；
3. **覆盖窗口显式**：`window()` 给出 `start/end/fetched_at`，
   查询超窗时调用方才有依据说"没量到"而不是"无解禁"；
4. **写闸门 fail-closed**：不可写的实例抛错，且错误里带"照抄就能用"的命令；
5. **口径同源**：`ingest()` 调的就是投资日历那份取数（`fetch_unlock_schedule`），
   不另写一套（两份必然漂移）；
6. **字段真落库**：`shares`（解禁数量/股）等源里本来就有的列必须落进去
   （用户点名要"解禁数量"）。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.infrastructure.repositories import unlock_plan_repo as repo


@pytest.fixture()
def temp_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把表指到临时库（**只猴补 `_path` 这一个缝**）。"""
    path = tmp_path / "unlock.db"
    monkeypatch.setattr(repo, "_path", lambda: str(path))
    return path


def _rows() -> list[dict[str, Any]]:
    return [
        {"code": "600036", "name": "招商银行", "unlock_date": "2026-10-15",
         "shares": 12_345_678.4, "actual_shares": 12_345_678.0,
         "market_cap": 4.5e8, "pct_of_float": 0.35,
         "share_type": "定向增发机构配售股份", "certainty": "rule"},
        {"code": "300308", "name": "中际旭创", "unlock_date": "2026-11-02",
         "shares": 2.0e6, "actual_shares": 2.0e6,
         "market_cap": 1.2e9, "pct_of_float": 1.5,
         "share_type": "首发原股东限售股份", "certainty": "rule"},
    ]


def test_schema_and_idempotent_upsert(temp_store: Path) -> None:
    """★ 幂等：同内容重复落库 → 新增 N / 更新 0 / 未变 N；再改一个值 → 只更新它。"""
    first = repo.upsert(_rows(), source="test")
    assert (first.inserted, first.updated, first.unchanged) == (2, 0, 0)
    assert first.window_start == "2026-10-15" and first.window_end == "2026-11-02"

    again = repo.upsert(_rows(), source="test")
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 2), (
        "同内容重复落库必须一行都不动（含 fetched_at）")

    with repo.connect() as conn:
        before = conn.execute(
            f"SELECT fetched_at FROM {repo.TABLE} WHERE code='600036'").fetchone()[0]
    changed = _rows()
    changed[0]["market_cap"] = 9.9e8
    third = repo.upsert(changed, source="test")
    assert (third.inserted, third.updated, third.unchanged) == (0, 1, 1)
    with repo.connect() as conn:
        after = conn.execute(
            f"SELECT fetched_at FROM {repo.TABLE} WHERE code='600036'").fetchone()[0]
    assert after >= before


def test_shares_are_whole_numbers(temp_store: Path) -> None:
    """`解禁数量`是**股**（离散量）：源的 `12345678.4` 这类尾差要收成整数。"""
    repo.upsert(_rows(), source="test")
    rows = repo.query("600036", "2026-10-01", "2026-10-31")
    assert rows and rows[0]["shares"] == 12345678.0
    assert rows[0]["name"] == "招商银行"
    assert rows[0]["share_type"] == "定向增发机构配售股份"


def test_lookup_uses_index_seek_not_scan(temp_store: Path) -> None:
    """★★ 「直接精准哈希就找到了」的机器判据：**索引点查**，不是全表扫。"""
    repo.upsert(_rows(), source="test")
    plan = repo.query_plan(
        f"SELECT code, unlock_date FROM {repo.TABLE} WHERE code=? "
        "AND unlock_date>=? AND unlock_date<=? ORDER BY unlock_date ASC",
        ("600036", "2026-09-29", "2026-10-29"))
    assert plan, "EXPLAIN QUERY PLAN 没返回任何行（判据自己坏了）"
    joined = " ".join(plan)
    assert "SEARCH" in joined and "INDEX" in joined, joined
    assert "SCAN" not in joined, joined


def test_window_reports_coverage(temp_store: Path) -> None:
    """覆盖窗口必须可查（调用方靠它区分"没量到/量到 0"）。"""
    repo.upsert(_rows(), source="test")
    win = repo.window()
    assert win["rows"] == 2
    assert win["start"] == "2026-10-15" and win["end"] == "2026-11-02"
    assert win["fetched_at"], "必须记录落库时间（新鲜度）"


def test_query_respects_window(temp_store: Path) -> None:
    """窗口外的行不许被查出来（否则"未来一个月"会掺进一年后的解禁）。"""
    repo.upsert(_rows(), source="test")
    assert repo.query("600036", "2026-10-01", "2026-10-31")
    assert repo.query("600036", "2026-11-01", "2026-11-30") == []
    assert repo.query("999999", "2026-10-01", "2026-10-31") == []


def test_write_gate_is_fail_closed(
    temp_store: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 不可写的实例：抛错，且错误里带**照抄就能用**的命令（拒绝要给出路）。"""
    from src.infrastructure.catalog.data_stores import WriteDecision

    monkeypatch.setattr(
        repo.data_stores, "writable_here",
        lambda _name: WriteDecision("app_db", False, "登记的写者是别的实例"))
    with pytest.raises(PermissionError) as excinfo:
        repo.upsert(_rows(), source="test")
    message = str(excinfo.value)
    assert "不可写" in message and "unlock_plan.py --ingest" in message


def test_ingest_uses_the_calendar_source(
    temp_store: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ `ingest()` 必须**复用**投资日历那份取数（不另写一套）。

    造两个事件（含明细分股），断言落库行数、`解禁数量`、`certainty` 都进来了。
    """
    class _Scope(dict):
        pass

    class _Event:
        def __init__(self, day: str, stocks: list[dict[str, Any]]) -> None:
            self.date = day
            self.scope = _Scope({"stocks": stocks})
            self.certainty = "rule"

    events = [
        _Event("2026-10-20", [
            {"code": "600519", "name": "贵州茅台", "market_cap": 1.0e9,
             "pct_of_float": 0.8, "share_type": "首发原股东限售股份",
             "shares": 3_000_000.0, "actual_shares": 3_000_000.0,
             "close_before": 1700.0, "chg_before_20d": -3.2,
             "chg_after_20d": 0.0},
        ]),
    ]

    import src.domain.intel.calendar as calendar_mod

    monkeypatch.setattr(calendar_mod, "fetch_unlock_schedule",
                        lambda **_kw: (events, ["unlock_em"]))
    outcome = repo.ingest(horizon_days=400)
    assert outcome.error == "", outcome.error
    assert outcome.inserted == 1
    rows = repo.query("600519", "2026-10-01", "2026-10-31")
    assert rows and rows[0]["shares"] == 3_000_000.0
    assert rows[0]["certainty"] == "rule"
    assert rows[0]["close_before"] == 1700.0
    assert rows[0]["chg_before_20d"] == -3.2


def test_ingest_reports_source_failure_without_raising(
    temp_store: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取数失败 → **如实返回 error**（调度器要能把这次记成 partial 而不是崩）。"""
    import src.domain.intel.calendar as calendar_mod

    def _boom(**_kw: Any) -> Any:
        raise RuntimeError("模拟源不可达")

    monkeypatch.setattr(calendar_mod, "fetch_unlock_schedule", _boom)
    outcome = repo.ingest()
    assert "取数失败" in outcome.error


def test_ingest_empty_source_is_an_error_not_a_silent_zero(
    temp_store: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源返回 0 条 → 必须报出来（"没量到"与"量到 0"分开）。"""
    import src.domain.intel.calendar as calendar_mod

    monkeypatch.setattr(calendar_mod, "fetch_unlock_schedule",
                        lambda **_kw: ([], ["unlock_em"]))
    outcome = repo.ingest()
    assert "0 条" in outcome.error
    assert repo.window()["rows"] == 0


def test_horizon_end_helper(temp_store: Path) -> None:
    """视野右端计算（判断"这次查询有没有超出覆盖"用）。"""
    end = repo.expected_horizon_end(today=date(2026, 9, 29), horizon_days=30)
    assert end == (date(2026, 9, 29) + timedelta(days=30)).isoformat()


def test_repo_never_writes_path_literals() -> None:
    """纪律：路径只能来自 registry（`test_store_registry` 的仓级 ratchet 之外，
    这里再加一条**本模块**的断言，防止后来者顺手写死路径）。"""
    source = Path(repo.__file__).read_text(encoding="utf-8")
    for literal in ("data/moss", "D:/", "D:\\\\", "sqlite3.connect(\"data"):
        assert literal not in source, literal
    assert "resolve_store" in source
    # 表名与存储名都必须是常量（不是散落的字面量）
    assert repo.TABLE == "unlock_plan"
    assert isinstance(repo.connect(), sqlite3.Connection)
