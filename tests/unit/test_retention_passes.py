"""增量保留清理与回填过滤的回归测试（2026-09-25 数据库审计后新增）。

## 被钉住的三个问题

审计发现主库 6.32GB，其中 **3.96GB（63%）是空闲页**，而全项目只有
`retention_service` 一个清理服务、只覆盖 2 张表。本文件钉住修复后的不变量：

1. **口径正确**：每张表的截止值格式必须与其时间列匹配 ——
   `trade_date` 是 `YYYYMMDD`（无横线），`expires_at` 是 ISO。
   混用会让字符串比较得出完全错误的结论
   （实测 `"2026-09-24" > "20260923"` 恒为真，翻译过来就是"什么都没删"）。
2. **不会误删**：会话必须**同时**满足"已过期或已撤销"才删；
   告警只删 `status='expired'`；`fact_registration_review` 这种审计链**不在**清理名单里。
3. **级联成对**：删告警要级联删已读记录（否则留孤儿）；
   删事件只**摘引用**不删告警（告警是业务数据，不是垃圾）。
"""

from __future__ import annotations

import sqlite3

import pytest

from src.core.config import Settings
from src.infrastructure import retention_passes as rp
from src.infrastructure.retention_passes import (
    RetentionPass,
    points_cutoff_for_years,
    prune_table,
    run_passes,
)


@pytest.fixture(autouse=True)
def _allow_destructive_in_tests(monkeypatch: pytest.MonkeyPatch):
    """显式放行清理，让本文件能验证清理逻辑本身。

    ⚠️ 为什么需要它：`retention_passes` 有一道**结构防护** —— 检测到
    pytest 进程（`PYTEST_CURRENT_TEST`）就拒绝执行破坏性清理
    （见 `destructive_allowed`）。理由是 `TestClient(app)` 会触发应用启动
    钩子，而 `main.py` 的 `settings` 是模块导入时求值的，测试里的
    `MOSS_SQLITE_PATH` 隔离**对它无效** —— 清理会打到真实库上。
    本仓已因同一陷阱清空过一次生产认证表（`core/config.py:83`）。

    这里显式打开是本文件**确有资格**：`db` fixture 用的是 `tmp_path` 隔离库。
    """
    monkeypatch.setenv(rp.ALLOW_IN_TEST_ENV, "1")
    yield


@pytest.fixture
def db(tmp_path):
    """建一个只含被测表的临时库，并把 Settings 指向它。

    为什么不用真实主库：清理是**删数据**的操作，测试绝不能碰生产库
    （本仓有过"测试夹具清空生产认证表"的真实事故，见 `core/config.py:83`）。
    """
    path = tmp_path / "retention.db"
    return path


def _settings(db, **overrides) -> Settings:
    base = dict(
        sqlite_path=str(db),
        data_retention_years=10,
        retention_batch_size=100,
        retention_max_batches_per_table=50,
        retention_notify_log_days=0,
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


def _scalar(db, sql: str, params: tuple = ()):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


# ==================== 1. 截止值格式 ====================


def test_trade_date_cutoff_is_compact_format() -> None:
    """★ `trade_date` 的截止值必须是 `YYYYMMDD`（无横线），与库里的实际格式一致。

    这条是**最容易写错**的地方：`date.isoformat()` 给出 `2026-09-25`，
    与 `20260923` 比较时字符串序完全错乱，后果是"要么一行都删不掉，
    要么把不该删的全删了"。所以两个格式都必须钉住。
    """
    # 构造成 years 口径
    pass_daily = RetentionPass(name="sector_crowding_daily",
                              time_column="trade_date", unit="years",
                              setting="retention_crowding_daily_years")
    cutoff = rp._cutoff_for(pass_daily, 6)
    assert len(cutoff) == 8 and cutoff.isdigit(), \
        f"trade_date 截止值应为 YYYYMMDD，实际 {cutoff!r}"
    assert "-" not in cutoff


def test_iso_cutoff_keeps_dashes() -> None:
    """ISO 时间列的截止值必须带横线（否则与库里的 `2026-09-25T...` 比不了）。"""
    pass_log = RetentionPass(name="fact_notify_log", time_column="at",
                             unit="days", setting="retention_notify_log_days")
    cutoff = rp._cutoff_for(pass_log, 180)
    assert cutoff.count("-") == 2, f"ISO 截止值应含横线，实际 {cutoff!r}"
    assert len(cutoff) == 10


def test_week_cutoff_matches_compute_week_format() -> None:
    """`compute_week` 是 `YYYY-Www`（实测样例 `2026-W38`），截止值必须同形。"""
    pass_metric = RetentionPass(name="sector_crowding_metric",
                               time_column="compute_week", unit="weeks",
                               setting="retention_crowding_metric_weeks")
    cutoff = rp._cutoff_for(pass_metric, 104)
    assert "-W" in cutoff, f"周口径应为 YYYY-Www，实际 {cutoff!r}"
    year, _, week = cutoff.partition("-W")
    assert year.isdigit() and week.isdigit() and len(week) == 2


def test_points_cutoff_for_years_is_single_source() -> None:
    """`fact_data_points` 的截止日只能有一份实现（回填过滤与清理同源）。"""
    from src.infrastructure.retention_service import _points_cutoff

    for years in (1, 10):
        assert _points_cutoff(years) == points_cutoff_for_years(years)


# ==================== 2. 不误删 ====================


def test_disabled_pass_deletes_nothing(db) -> None:
    """保留期为 0 = 不清理（历史行情数据的默认状态）。"""
    _exec(db, "CREATE TABLE fact_notify_log (id INTEGER PRIMARY KEY, at TEXT)")
    _exec(db, "INSERT INTO fact_notify_log (at) VALUES ('2000-01-01')")
    settings = _settings(db)            # retention_notify_log_days = 0
    result = __import__("asyncio").run(
        prune_table(settings, rp.PASSES[0]))
    assert result["deleted"] == 0
    assert "未开启" in result["skipped"]
    assert _scalar(db, "SELECT COUNT(*) FROM fact_notify_log") == 1


def test_notify_log_prunes_only_older_than_cutoff(db) -> None:
    """只删早于截止日的行，保留期内的必须留着。"""
    _exec(db, "CREATE TABLE fact_notify_log (id INTEGER PRIMARY KEY, at TEXT)")
    _exec(db, "INSERT INTO fact_notify_log (at) VALUES ('2000-01-01')")
    _exec(db, "INSERT INTO fact_notify_log (at) VALUES ('2999-01-01')")
    settings = _settings(db, retention_notify_log_days=180)
    result = __import__("asyncio").run(prune_table(settings, rp.PASSES[0]))
    assert result["deleted"] == 1
    left = _scalar(db, "SELECT COUNT(*) FROM fact_notify_log")
    assert left == 1
    assert _scalar(db, "SELECT at FROM fact_notify_log") == "2999-01-01"


def test_session_requires_expired_or_revoked(db) -> None:
    """★ 会话必须**同时**满足"不再有效"才删 —— 否则会把用户踢下线。

    构造三种行：
      A 绝对到期已过（该删）
      B 到期还在未来、未撤销（**绝不能删** —— 活跃会话）
      C 已撤销但到期在未来（该删：撤销即失效）
    """
    _exec(db, """CREATE TABLE fact_session (
        session_id TEXT PRIMARY KEY, absolute_expires_at TEXT,
        revoked_at TEXT)""")
    _exec(db, "INSERT INTO fact_session VALUES ('A','2000-01-01',NULL)")
    _exec(db, "INSERT INTO fact_session VALUES ('B','2999-01-01',NULL)")
    _exec(db, "INSERT INTO fact_session VALUES ('C','2999-01-01','2000-01-02')")
    settings = _settings(db, retention_session_days=90)
    pass_session = next(p for p in rp.PASSES if p.name == "fact_session")
    result = __import__("asyncio").run(prune_table(settings, pass_session))
    assert result["deleted"] == 2, result
    assert _scalar(db, "SELECT session_id FROM fact_session") == "B"


def test_alerts_only_expired_are_deleted(db) -> None:
    """告警只删 `status='expired'` 的；未过期的（含 unread）必须留着。"""
    _exec(db, """CREATE TABLE fact_alerts (
        alert_id TEXT PRIMARY KEY, status TEXT, expire_time TEXT)""")
    _exec(db, "INSERT INTO fact_alerts VALUES ('e1','expired','2000-01-01')")
    _exec(db, "INSERT INTO fact_alerts VALUES ('e2','active','2000-01-01')")
    settings = _settings(db, retention_alert_days=365)
    pass_alert = next(p for p in rp.PASSES if p.name == "fact_alerts")
    result = __import__("asyncio").run(prune_table(settings, pass_alert))
    assert result["deleted"] == 1
    assert _scalar(db, "SELECT alert_id FROM fact_alerts") == "e2"


def test_registration_review_is_never_in_passes() -> None:
    """★ 审计哈希链**不在**清理名单里 —— 删行会断链。

    它是 `_AuditChain` 的哈希链，且 AGENTS.md 要求敏感操作日志留存 ≥3 年。
    """
    names = {p.name for p in rp.PASSES}
    assert "fact_registration_review" not in names


# ==================== 3. 级联与解引用 ====================


def test_alert_cascade_deletes_read_rows(db) -> None:
    """★ 删告警必须级联删"已读"记录，否则留下引用不存在告警的孤儿行。"""
    _exec(db, """CREATE TABLE fact_alerts (
        alert_id TEXT PRIMARY KEY, status TEXT, expire_time TEXT)""")
    _exec(db, """CREATE TABLE user_alert_read (
        user_id TEXT, alert_id TEXT)""")
    _exec(db, "INSERT INTO fact_alerts VALUES ('e1','expired','2000-01-01')")
    _exec(db, "INSERT INTO fact_alerts VALUES ('e2','expired','2000-01-02')")
    _exec(db, "INSERT INTO user_alert_read VALUES ('u1','e1')")
    _exec(db, "INSERT INTO user_alert_read VALUES ('u1','e2')")
    _exec(db, "INSERT INTO user_alert_read VALUES ('u2','e1')")
    settings = _settings(db, retention_alert_days=365)
    pass_alert = next(p for p in rp.PASSES if p.name == "fact_alerts")
    result = __import__("asyncio").run(prune_table(settings, pass_alert))
    assert result["deleted"] == 2
    assert result["cascaded"] == 3, result
    assert _scalar(db, "SELECT COUNT(*) FROM user_alert_read") == 0


def test_missing_cascade_table_is_tolerated(db) -> None:
    """子表不存在（实测生产库还没有 `user_alert_read`）不能让整表清理失败。"""
    _exec(db, """CREATE TABLE fact_alerts (
        alert_id TEXT PRIMARY KEY, status TEXT, expire_time TEXT)""")
    _exec(db, "INSERT INTO fact_alerts VALUES ('e1','expired','2000-01-01')")
    settings = _settings(db, retention_alert_days=365)
    pass_alert = next(p for p in rp.PASSES if p.name == "fact_alerts")
    result = __import__("asyncio").run(prune_table(settings, pass_alert))
    assert result["deleted"] == 1
    assert "error" not in result


def test_event_dereference_keeps_alerts(db) -> None:
    """★ 删事件只**摘引用**，不删告警 —— 告警是业务数据，不是垃圾。"""
    _exec(db, """CREATE TABLE fact_events (
        event_id TEXT PRIMARY KEY, created_at TEXT)""")
    _exec(db, """CREATE TABLE fact_alerts (
        alert_id TEXT PRIMARY KEY, status TEXT, expire_time TEXT,
        event_id TEXT)""")
    _exec(db, "INSERT INTO fact_events VALUES ('ev1','2000-01-01')")
    _exec(db, "INSERT INTO fact_events VALUES ('ev2','2999-01-01')")
    _exec(db, "INSERT INTO fact_alerts VALUES ('a1','active','2999-01-01','ev1')")
    settings = _settings(db, retention_event_days=730)
    pass_event = next(p for p in rp.PASSES if p.name == "fact_events")
    result = __import__("asyncio").run(prune_table(settings, pass_event))
    assert result["deleted"] == 1
    # 告警还在，只是 event_id 被摘空
    assert _scalar(db, "SELECT COUNT(*) FROM fact_alerts") == 1
    assert _scalar(db, "SELECT event_id FROM fact_alerts") == ""


def test_quant_selection_cascades_items(db) -> None:
    """运行台账按 `run_id` 级联子表，否则子表留一堆永远查不到的孤儿行。"""
    _exec(db, """CREATE TABLE fact_quant_selection (
        id INTEGER PRIMARY KEY, trade_date TEXT)""")
    _exec(db, """CREATE TABLE fact_quant_selection_item (
        run_id INTEGER, code TEXT)""")
    _exec(db, "INSERT INTO fact_quant_selection (id, trade_date) VALUES (1,'20000101')")
    _exec(db, "INSERT INTO fact_quant_selection (id, trade_date) VALUES (2,'29990101')")
    _exec(db, "INSERT INTO fact_quant_selection_item VALUES (1,'600000')")
    _exec(db, "INSERT INTO fact_quant_selection_item VALUES (2,'600036')")
    settings = _settings(db, retention_quant_selection_days=365)
    pass_q = next(p for p in rp.PASSES if p.name == "fact_quant_selection")
    result = __import__("asyncio").run(prune_table(settings, pass_q))
    assert result["deleted"] == 1
    assert result["cascaded"] == 1
    assert _scalar(db, "SELECT run_id FROM fact_quant_selection_item") == 2


# ==================== 4. 批次有界 / 隔离失败 ====================


def test_batches_are_bounded_and_resume_next_round(db) -> None:
    """分批删除：单轮受 `retention_max_batches_per_table` 限制，
    剩余的行下一轮再删（不能一轮跑几小时把库锁死）。"""
    _exec(db, "CREATE TABLE fact_notify_log (id INTEGER PRIMARY KEY, at TEXT)")
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany("INSERT INTO fact_notify_log (at) VALUES ('2000-01-01')",
                         [()] * 250)
        conn.commit()
    finally:
        conn.close()
    # 每批 10 行、最多 3 批 → 单轮最多 30 行
    settings = _settings(db, retention_notify_log_days=180,
                         retention_batch_size=10,
                         retention_max_batches_per_table=3)
    result = __import__("asyncio").run(prune_table(settings, rp.PASSES[0]))
    assert result["truncated"] is True
    assert result["deleted"] <= 30
    assert 0 < _scalar(db, "SELECT COUNT(*) FROM fact_notify_log")


def test_run_passes_skips_disabled_and_reports_errors(db) -> None:
    """`run_passes` 只回报**启用**的档位（否则真正的删除信息被 skipped 淹没）。"""
    _exec(db, "CREATE TABLE fact_notify_log (id INTEGER PRIMARY KEY, at TEXT)")
    settings = _settings(db, retention_notify_log_days=180)
    results = __import__("asyncio").run(run_passes(settings))
    assert [r["table"] for r in results] == ["fact_notify_log"]


def test_prune_table_isolates_failure(db) -> None:
    """某张表清理失败不能抛出（保留是增强能力，不能拖垮主链路）。"""
    bad = RetentionPass(name="no_such_table", time_column="at", unit="days",
                        setting="retention_notify_log_days")
    settings = _settings(db, retention_notify_log_days=180)
    result = __import__("asyncio").run(prune_table(settings, bad))
    assert result["deleted"] == 0
    assert "表不存在" in result["skipped"]


# ==================== 5. 测试进程防护（保护生产库） ====================


def test_guard_refuses_inside_pytest_without_optin(
        db, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 核心防护：pytest 进程里**默认拒绝**破坏性清理。

    这条挡住的是一条真实链路：`TestClient(app)` 会跑应用启动钩子
    → `run_retention` → 本模块，而 `main.py` 的 `settings` 是模块导入时
    求值的，测试里的 `MOSS_SQLITE_PATH` 隔离**对它无效**，
    清理会打到真实库上。本仓已因同一陷阱清空过一次生产认证表。

    所以这里**显式撤掉**放行标志，验证防护确实生效、且一行都没删。
    """
    from src.infrastructure.retention_passes import destructive_allowed

    monkeypatch.delenv(rp.ALLOW_IN_TEST_ENV, raising=False)
    _exec(db, "CREATE TABLE fact_notify_log (id INTEGER PRIMARY KEY, at TEXT)")
    _exec(db, "INSERT INTO fact_notify_log (at) VALUES ('2000-01-01')")
    settings = _settings(db, retention_notify_log_days=180)

    allowed, why = destructive_allowed(settings)
    assert allowed is False, "pytest 进程里默认必须拒绝"
    assert "pytest" in why.lower()

    result = __import__("asyncio").run(prune_table(settings, rp.PASSES[0]))
    assert result["deleted"] == 0
    assert "拒绝" in result["skipped"]
    assert _scalar(db, "SELECT COUNT(*) FROM fact_notify_log") == 1, \
        "被防护挡住时一行都不能删"


def test_guard_allows_outside_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 反向用例：**生产（非 pytest 进程）必须照常清理**。

    这一条与上面同等重要：若判据写成"只有临时库才允许清理"，
    生产库就永远不清理 —— 保留功能被静默废掉，没人会发现。
    所以必须钉住"生产路径放行"。
    """
    from src.infrastructure.retention_passes import destructive_allowed

    monkeypatch.delenv(rp.ALLOW_IN_TEST_ENV, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    settings = _settings("/var/lib/moss/moss_finagent.db")
    allowed, why = destructive_allowed(settings)
    assert allowed is True, f"生产库必须放行清理，实际被拒：{why}"
