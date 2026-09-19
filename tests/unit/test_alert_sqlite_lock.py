"""事件告警读路径的 SQLite 锁竞争回归测试（2026-09-18 线上500复盘）。

故障现象：主库 data/moss_finagent.db 被做T采集/量化仓库/资金流等多条链并发
写入时，前端"扫描完成后"拉取 `GET /api/v1/alerts` 返回 500，后台 traceback：

    alert_sqlite_repo.py:66 _list_alerts_sync
        self._expire_due(conn, now_iso(), tenant_id)
    sqlite3.OperationalError: database is locked

根因与修复纪律（本文件逐条锁死）：
1. 读路径里的懒过期 `UPDATE fact_alerts` 是**写操作**，抢不到写锁就把整个
   列表请求打成 500 —— 过期只是幂等清理，必须可降级；
2. 事件/告警仓储用裸 `sqlite3.connect()`（Python 默认仅 5s 锁等待），
   与项目其它仓储（WAL + busy_timeout）不一致 —— 必须显式给锁等待；
3. 等待时间必须按代价分级：服务是单事件循环 + to_thread 默认线程池，
   让"顺带清理"也排队 20s 会占死线程、打满线程池（实测占锁 12s 时连续
   4 次列表请求全部超时，比 500 更糟）—— 读路径/懒过期必须秒级失败。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest

from src.domain.alerts.models import (
    DEFAULT_TENANT,
    Alert,
    AlertLevel,
    AlertStatus,
    AlertType,
)
from src.infrastructure.repositories import event_sqlite_base
from src.infrastructure.repositories.event_sqlite_base import (
    BUSY_TIMEOUT_MS,
    FAST_BUSY_TIMEOUT_MS,
    FAST_TIMEOUT_S,
    connect_sqlite,
    is_locked_error,
    retry_on_locked,
)
from src.infrastructure.repositories.event_sqlite_repo import (
    EventSqliteRepository,
)

#: 已经过期（expire_time < now）→ 任何一次读都会尝试把它置 expired。
_EXPIRED_AT = "2020-01-01T00:00:00+08:00"


def _alert(alert_id: str = "al_lock1") -> Alert:
    return Alert(
        alert_id=alert_id, alert_key=f"{alert_id}:opportunity",
        event_id=f"evt_{alert_id}", alert_type=AlertType.OPPORTUNITY,
        alert_level=AlertLevel.MEDIUM, title="锁竞争回归用例",
        description="机会信号", risk_score=5, opportunity_score=70,
        confidence=0.7, trigger_time="2020-01-01T00:00:00+08:00",
        expire_time=_EXPIRED_AT,
    )


def _open_raw(db_path: str, wait_ms: int = 0) -> sqlite3.Connection:
    # check_same_thread=False：占锁连接由用例的释放线程回滚（见重试用例）
    conn = sqlite3.connect(db_path, timeout=0.2, check_same_thread=False)
    conn.execute(f"PRAGMA busy_timeout={wait_ms}")
    return conn


def _hold_write_lock(db_path: str) -> sqlite3.Connection:
    """另开连接拿住写锁（BEGIN IMMEDIATE），模拟主库里的并发写入者。"""
    holder = _open_raw(db_path)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(
        "INSERT OR REPLACE INTO fact_alerts "
        "(alert_id, alert_key, event_id, alert_type, alert_level, title, "
        " description, risk_score, opportunity_score, confidence, "
        " trigger_time, expire_time, status, tenant_id, disclaimer) "
        "VALUES ('al_holder','al_holder:risk','evt_h','risk','low','占位锁',"
        " '',0,0,0,'2020-01-01T00:00:00+08:00','2020-01-01T00:00:00+08:00',"
        " 'active', ?, 'x')",
        (DEFAULT_TENANT,),
    )
    return holder


@pytest.fixture
def locked_repo(tmp_dir):
    """建好表、写进一条已过期告警，然后让别的连接占住写锁。"""
    db_path = f"{tmp_dir}/lock.db"
    repo = EventSqliteRepository(db_path)
    asyncio.run(repo.ensure_schema())
    assert asyncio.run(repo.upsert_alerts([_alert()]))["inserted"] == 1

    holder = _hold_write_lock(db_path)
    try:
        yield repo, holder, db_path
    finally:
        holder.rollback()
        holder.close()


@pytest.fixture
def no_lock_wait(monkeypatch):
    """把锁等待压到 0：让"抢不到锁"立刻发生，用例无需真等 20 秒。"""
    monkeypatch.setattr(event_sqlite_base, "CONNECT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(event_sqlite_base, "BUSY_TIMEOUT_MS", 0)
    monkeypatch.setattr(event_sqlite_base, "FAST_TIMEOUT_S", 0.05)
    monkeypatch.setattr(event_sqlite_base, "FAST_BUSY_TIMEOUT_MS", 0)
    monkeypatch.setattr(event_sqlite_base, "EXPIRE_TIMEOUT_S", 0)


def test_reader_connection_waits_for_main_db_lock(tmp_dir):
    """仓储连接必须带锁等待 + WAL（与项目其它仓储同范式）。"""
    conn = connect_sqlite(f"{tmp_dir}/pragma.db")
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_interactive_connection_uses_short_wait(tmp_dir):
    """读/交互路径的连接必须用短等待，绝不能排 20s（否则会打满线程池）。"""
    store_conn = connect_sqlite(
        f"{tmp_dir}/fast.db", timeout_s=FAST_TIMEOUT_S,
        busy_timeout_ms=FAST_BUSY_TIMEOUT_MS)
    try:
        assert FAST_TIMEOUT_S < 5.0
        assert store_conn.execute(
            "PRAGMA busy_timeout").fetchone()[0] == FAST_BUSY_TIMEOUT_MS
    finally:
        store_conn.close()


def test_list_alerts_survives_locked_main_db(locked_repo, no_lock_wait):
    """核心回归：写锁被占用时列表照常返回，不再 500。

    过期清理被跳过的告警仍以原状态返回（active），下轮读/写自会重试，
    绝不会因为一次清理失败让用户看不到告警。
    """
    repo, _, _ = locked_repo

    alerts = asyncio.run(repo.list_alerts(tenant_id=DEFAULT_TENANT))

    assert [a.alert_id for a in alerts] == ["al_lock1"]
    assert alerts[0].status is AlertStatus.ACTIVE  # 过期降级为"下次再说"
    assert asyncio.run(repo.count_unread(DEFAULT_TENANT)) == 1


def test_list_alerts_latency_is_bounded_when_db_is_busy(locked_repo, monkeypatch):
    """读路径延迟必须有界：占锁 1s 时列表也要在 1s 级返回，不许长期阻塞。

    这条守住 2026-09-18 的二次踩坑：把懒过期改成 20s 排队后，占锁 12s
    期间 4 次请求全部超时（线程池被占死）。这里占锁 1s、单次抢锁上限 0.3s，
    只要"清理等待"重新变成长时间排队，用例立刻变红。
    """
    repo, holder, _ = locked_repo
    monkeypatch.setattr(event_sqlite_base, "CONNECT_TIMEOUT_S", 0.3)
    monkeypatch.setattr(event_sqlite_base, "BUSY_TIMEOUT_MS", 300)
    monkeypatch.setattr(event_sqlite_base, "FAST_TIMEOUT_S", 0.3)
    monkeypatch.setattr(event_sqlite_base, "FAST_BUSY_TIMEOUT_MS", 300)
    monkeypatch.setattr(event_sqlite_base, "EXPIRE_TIMEOUT_S", 0.3)

    started = time.monotonic()
    alerts = asyncio.run(repo.list_alerts(tenant_id=DEFAULT_TENANT))
    elapsed = time.monotonic() - started

    assert [a.alert_id for a in alerts] == ["al_lock1"]
    assert elapsed < 1.0, f"占锁期间列表耗时 {elapsed:.2f}s，读路径又变成长阻塞"
    assert holder.in_transaction  # 占锁者全程未释放


def test_expire_due_reports_deferred_cleanup(locked_repo, no_lock_wait):
    """`_expire_due` 抢不到锁时返回 False（供上层判定降级），而不是抛异常。"""
    repo, _, _ = locked_repo

    assert repo._expire_due(  # noqa: SLF001 用例直接验内部清理语义
        DEFAULT_TENANT, "2026-09-18T00:00:00+08:00") is False


def test_upsert_alerts_retries_until_lock_released(locked_repo, monkeypatch):
    """写路径的锁重试：占锁者释放后，同一次 upsert 仍能成功入库。

    真实时序：主库被占 → 连接在 busy_timeout 内排队重试 → 占锁者提交释放 →
    本次采集/告警入库照常成功，而不是白丢一轮结果。
    """
    repo, holder, _ = locked_repo
    # 生产是 20s 排队；用例压到 0.6s，覆盖 0.15s 的持锁窗口即可
    monkeypatch.setattr(event_sqlite_base, "CONNECT_TIMEOUT_S", 0.6)
    released = threading.Event()

    def _release_after(delay: float) -> None:
        time.sleep(delay)
        holder.rollback()  # 释放写锁（连接已 check_same_thread=False）
        released.set()

    releaser = threading.Thread(target=_release_after, args=(0.15,))
    releaser.start()
    try:
        saved = asyncio.run(repo.upsert_alerts([_alert("al_lock2")]))
    finally:
        releaser.join()

    assert released.is_set()
    assert saved["inserted"] == 1


def test_retry_on_locked_retries_lock_but_not_other_errors():
    calls = {"n": 0}

    def _flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert retry_on_locked(_flaky, attempts=3, delay_s=0.01) == "ok"
    assert calls["n"] == 3

    broken = {"n": 0}

    def _broken() -> str:
        broken["n"] += 1
        raise sqlite3.OperationalError("no such table: nope")

    with pytest.raises(sqlite3.OperationalError):
        retry_on_locked(_broken, attempts=3, delay_s=0.01)
    assert broken["n"] == 1  # 非锁错误立即上抛，不掩盖真实故障


def test_is_locked_error_matches_busy_and_locked():
    assert is_locked_error(sqlite3.OperationalError("database is locked"))
    assert is_locked_error(sqlite3.OperationalError("database table is locked"))
    assert not is_locked_error(sqlite3.OperationalError("no such table: t"))
    assert not is_locked_error(ValueError("locked"))
