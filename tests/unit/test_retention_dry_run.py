"""`run_retention(dry_run=True)` 必须是**全档只读**（判据）。

## 这条判据是怎么来的（真实自伤，值得留档）

我第一版只给**第 3 档**（增量流水表 / `retention_passes`）做了 dry-run，
第 1、2 档（数据点 / 新闻缓存）直接透传了原来的真删调用。结果：

```
$ python manage.py retention --dry-run
数据库：data/moss_finagent.db          ← 真库（2.57 GB）
模式：dry-run（只统计，不修改任何数据）
{ "points_deleted": 12, ... }          ← 它真的删了 12 行
   主库 mtime = 命令执行的那一刻       ← 取证
```

**危害的形状**：命令的名字与提示都在说"不会改数据"，而它在删数据。
比"没有 dry-run"危险得多 —— 用户会据此做保留期决策。

**为什么必须"全档"而不是"做了哪档算哪档"**：dry-run 是一个**承诺**
（"这一步不动数据"），部分实现等于承诺不成立，且失败是静默的
（返回值看起来完全正常）。

## 判据强度

- `test_dry_run_does_not_delete_any_tier` —— **行为判据**：三档都造数据，
  跑 dry-run，逐个断言**行还在**。把任一层改回真删 ⇒ 立刻红。
- `test_dry_run_reports_the_same_counts_as_the_real_run` —— 计数必须与真跑一致
  （否则 dry-run 是安慰剂）。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from src.core.config import Settings
from src.infrastructure import retention_passes as rp
from src.infrastructure.retention_service import run_retention

OLD = "2000-01-01"          # 一定早于任何保留截止线
NEW = "2999-01-01"          # 一定晚于任何保留截止线


@pytest.fixture(autouse=True)
def _allow_destructive_in_tests(monkeypatch: pytest.MonkeyPatch):
    """放行清理，让本文件能验证"计数 == 真跑行数"。

    ⚠️ 没有它，**真跑会被 pytest 防护挡住**（`destructive_allowed` 检测到
    pytest 进程就拒绝执行破坏性清理）⇒ `real["passes_deleted"] == 0`，
    而 dry-run 不受该防护限制 ⇒ 判据会误报成"计数漂移"。
    **第一版判据就是这么误报的** —— 所以这条夹具本身就是判据可信的前提。

    本文件确有资格放行：所有库都是 `tmp_path` 下的隔离库。
    """
    monkeypatch.setenv(rp.ALLOW_IN_TEST_ENV, "1")
    yield


def _settings(db, **overrides) -> Settings:
    base = dict(
        sqlite_path=str(db),
        data_retention_years=10,
        news_cache_enabled=True,
        news_retention_days=30,
        retention_batch_size=100,
        retention_max_batches_per_table=50,
        # 三档里用到的那几个保留期（0 = 不清理）
        retention_notify_log_days=180,
        retention_session_days=0,
        retention_remember_token_days=0,
        retention_verification_code_days=0,
        retention_password_reset_days=0,
        retention_alert_days=0,
        retention_event_days=0,
        retention_crowding_daily_years=0,
        retention_crowding_metric_weeks=0,
        retention_auction_days=0,
        retention_quant_selection_days=0,
    )
    base.update(overrides)
    return Settings(**base)


def _exec(db, sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _count(db, table: str) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


@pytest.fixture
def make_db(tmp_path):
    """造库工厂：**每次调用一份全新的等价库**。

    为什么必须是工厂而不是单个 `db`：判据要比较"dry-run 报的数"与"真跑删的数"，
    而真跑会把行删掉 —— 在同一个库上先 dry 再真跑，真跑看到的必然是 0
    （第一版判据就是这么写错的），于是"计数一致"永远不成立。
    """
    counter = {"n": 0}

    def _make():
        counter["n"] += 1
        path = tmp_path / f"retention_dryrun_{counter['n']}.db"
        # 第 1 档：数据点（period_date 早于 10 年前）
        _exec(path, """CREATE TABLE fact_data_points (
            indicator TEXT, period_date TEXT, source_name TEXT, raw_content_hash TEXT)""")
        _exec(path, "INSERT INTO fact_data_points (indicator, period_date) "
                    "VALUES ('dv_ratio', '1999-01-01')")
        _exec(path, "INSERT INTO fact_data_points (indicator, period_date) "
                    "VALUES ('dv_ratio', '2999-01-01')")
        # 第 2 档：新闻缓存
        _exec(path, """CREATE TABLE news_cache (
            cache_key TEXT PRIMARY KEY, scope TEXT, payload_json TEXT,
            fetch_time TEXT, latest_publish_time TEXT)""")
        _exec(path, "INSERT INTO news_cache (cache_key, scope, payload_json, fetch_time) "
                    "VALUES ('k1', '', '[]', '2000-01-01T00:00:00')")
        # 第 3 档：增量流水表
        _exec(path, "CREATE TABLE fact_notify_log (id INTEGER PRIMARY KEY, at TEXT)")
        _exec(path, "INSERT INTO fact_notify_log (at) VALUES ('2000-01-01')")
        return path

    return _make


@pytest.fixture
def db(make_db):
    """三档各造一张表 + 一条"该被清理"的旧行。"""
    return make_db()


def test_dry_run_does_not_delete_any_tier(db) -> None:
    """★ 核心判据：dry-run 之后，**三档的旧行都还在**。

    这是"自伤那一次"的判据化。任何一档被改回真删 ⇒ 立刻红。
    """
    settings = _settings(db)
    before = (_count(db, "fact_data_points"), _count(db, "news_cache"),
              _count(db, "fact_notify_log"))

    outcome = asyncio.run(run_retention(settings, dry_run=True))

    after = (_count(db, "fact_data_points"), _count(db, "news_cache"),
             _count(db, "fact_notify_log"))
    assert after == before, (
        f"★ dry-run 删了数据！{before} -> {after}（第 1/2 档曾漏做只读）"
    )
    assert outcome["dry_run"] is True
    # 反证：它确实**看见**了该删的行（否则"没删"是因为什么都没找到，判据是假绿）
    assert outcome["points_deleted"] == 1, f"第 1 档没数出待删行：{outcome}"
    assert outcome["passes_deleted"] == 1, f"第 3 档没数出待删行：{outcome}"


def test_dry_run_reports_the_same_counts_as_the_real_run(make_db) -> None:
    """dry-run 报的行数必须与真跑一致 —— 否则"先看后做"是安慰剂。

    ⚠️ 两次运行**各用一份全新的等价库**：在同一个库上先 dry 再真跑，
    真跑看到的必然是 0（行已经被……不，dry 没删；但真跑会删），
    于是比较失去意义。这里两份库的构造完全一致（同一个工厂）。
    """
    dry = asyncio.run(run_retention(_settings(make_db()), dry_run=True))
    real = asyncio.run(run_retention(_settings(make_db()), dry_run=False))

    assert dry["points_deleted"] == real["points_deleted"], (
        f"第 1 档计数漂移：dry={dry['points_deleted']} real={real['points_deleted']}"
    )
    assert dry["passes_deleted"] == real["passes_deleted"], (
        f"第 3 档计数漂移：dry={dry['passes_deleted']} real={real['passes_deleted']}"
    )
    assert dry["news_deleted"] == real["news_deleted"], (
        f"第 2 档计数漂移：dry={dry['news_deleted']} real={real['news_deleted']}"
    )
    # 反证：三档都真的有东西可删（否则"一致"是 0 == 0 的假绿）
    assert real["points_deleted"] == 1 and real["passes_deleted"] == 1


def test_dry_run_keeps_new_rows_and_they_survive_a_real_run(db) -> None:
    """保留期内的行**两个模式都不许动**（防止"为了 dry-run 一致"把范围放宽）。"""
    settings = _settings(db)
    asyncio.run(run_retention(settings, dry_run=True))
    assert _count(db, "fact_data_points") == 2, "dry-run 动了保留期内的行"
    asyncio.run(run_retention(settings, dry_run=False))
    conn = sqlite3.connect(str(db))
    try:
        kept = conn.execute(
            "SELECT COUNT(*) FROM fact_data_points WHERE period_date = ?",
            (NEW,)).fetchone()[0]
    finally:
        conn.close()
    assert int(kept) == 1, "真跑把保留期内的行也删了（截止线算错）"


def test_dry_run_does_not_invalidate_repository_cache(db) -> None:
    """dry-run 不该引发缓存失效（一行没删，失效它只是让下一次查询白跑库）。

    这条钉的是 `CachedRepository.prune_before` 里的 `and not dry_run`。
    不接 Redis 也能测：缓存未启用时装饰器不存在，所以这里直接测装饰器本体。
    """
    from src.infrastructure.repositories.cached_repo import CachedRepository
    from src.infrastructure.repositories.macro_repo import MacroRepository

    inner = MacroRepository(str(db))
    cached = CachedRepository(inner, "redis://127.0.0.1:1/0")   # 不可达 -> fail-open

    calls: list[int] = []

    async def _spy() -> None:
        calls.append(1)

    cached._invalidate_all = _spy        # type: ignore[assignment]
    n = asyncio.run(cached.prune_before("2010-01-01", dry_run=True))
    assert n >= 1, f"计数应 ≥1，实际 {n}"
    assert calls == [], "dry-run 触发了缓存失效（数据一行没改，白失效）"
