"""★ 「同一逻辑时刻只许采样一次」的判据（2026-10-01）。

## 为什么需要这个文件（现场）

`tests/unit/test_auth_service.py::test_session_slides_but_absolute_cap_never_extends`
在**满载时偶发红灯**，单独跑永远通过。追下去不是"测试写得松"，而是产品代码的
一个**缺陷类别**：

    `iso_in(seconds)` 每次调用都自己取一次当前时间，
    而 `create_session()` 里一行数据有 4 个由当前时间派生的列
    （`created_at` / `last_seen_at` / `idle_expires_at` / `absolute_expires_at`）。

于是 INSERT 用的 `idle_expires_at` 与**返回给调用方**的
`SessionRecord.idle_expires_at` 是**两次独立采样**：跨过秒边界时，函数返回的
对象与它刚刚写进库的那一行**差 1 秒**（违反"读己之写"）。产品行为是对的，
判据却红了 —— 这种红灯最坏，因为它会被当成"抖动"忽略掉，
而它掩盖的是"**同一行内部可能来自不同时刻**"。

## 判据怎么做到确定（不 sleep、不依赖负载）

把时钟换成**每调用一次就前进一秒**的充气时钟（`_ticking_clock`）：

* 只要还有"同一逻辑时刻采样两次"的代码，两次采样必然相差 1 秒 ⇒ **必红**；
* 修好之后（一次采样、处处派生）⇒ **必绿**，且与机器快慢无关。

这就是本项目那条纪律的用法：**把不确定性换成确定性，而不是把阈值调松**。

## 自证

`test_the_ticking_clock_proves_the_defect_is_real` 直接把**旧写法**就地重现
（`iso_in(x)` 连调两次），断言它**确实**给出不同的值 ——
否则上面的判据可能是"时钟根本没走"造成的假绿。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.infrastructure.repositories import auth_sqlite_repo as repo_mod
from src.infrastructure.repositories.auth_sqlite_repo import (
    AuthSqliteRepository,
    iso_in,
)


def _ticking_clock(start: str = "2026-10-01T00:00:00+00:00"):
    """每被调用一次就前进一秒的时钟（确定性"慢机器"）。"""
    base = datetime.fromisoformat(start)
    state = {"n": 0}

    def _now() -> datetime:
        state["n"] += 1
        return base + timedelta(seconds=state["n"] - 1)

    _now.state = state          # type: ignore[attr-defined]
    return _now


@pytest.fixture()
def ticking(monkeypatch):
    clock = _ticking_clock()
    monkeypatch.setattr(repo_mod, "utc_now", clock)
    return clock


@pytest.fixture()
def repo(tmp_path) -> AuthSqliteRepository:
    return AuthSqliteRepository(str(tmp_path / "auth_single_clock.db"))


# ======================================================================
# 一、自证：这个充气时钟真的能暴露"多次采样"
# ======================================================================

def test_the_ticking_clock_proves_the_defect_is_real(ticking) -> None:
    """★ 自证：**旧写法**（两次独立采样）在这个时钟下必须给出不同的值。

    没有这一条，下面几条判据在"时钟其实没走"的情况下会**假绿** ——
    本项目纪律：一条从没红过的判据，要先怀疑它坏了。
    """
    a = iso_in(60)          # 不传 now：自己取时钟 ⇒ 每次都不同（旧写法）
    b = iso_in(60)
    assert a != b, "充气时钟没起作用（旧写法居然给出相同的值）⇒ 下面的判据不可信"

    # 而"一次采样、处处派生"的写法必须给出**完全相同**的值
    base = ticking()
    assert iso_in(60, now=base) == iso_in(60, now=base)


# ======================================================================
# 二、会话：写进去的那一行 == 返回给调用方的那一对象
# ======================================================================

def test_create_session_returns_exactly_what_it_persisted(
    repo: AuthSqliteRepository, ticking,
) -> None:
    """★ 读己之写：`create_session` 的返回值必须与库里的行**逐字段一致**。

    旧实现在这个判据下必红：返回的对象是**第二次采样**算出来的。
    """
    created = repo.create_session(
        session_id="s1", user_id="u1", tenant_id="vip", access_jti="j1",
        refresh_hash="rh", idle_seconds=1800, absolute_seconds=43200)
    row = repo.get_session("s1")
    assert row is not None

    assert created.created_at == row.created_at
    assert created.last_seen_at == row.last_seen_at
    assert created.idle_expires_at == row.idle_expires_at, (
        "返回对象与库里那行的 idle_expires_at 不一致 ⇒ 同一逻辑时刻被采样了多次")
    assert created.absolute_expires_at == row.absolute_expires_at, (
        "返回对象与库里那行的 absolute_expires_at 不一致 ⇒ 同一逻辑时刻被采样了多次")


def test_session_columns_are_derived_from_one_instant(
    repo: AuthSqliteRepository, ticking,
) -> None:
    """★ 同一行的派生列必须来自**同一个时刻**：差值要精确等于给定秒数。"""
    created = repo.create_session(
        session_id="s2", user_id="u1", tenant_id="vip", access_jti="j2",
        refresh_hash="rh", idle_seconds=1800, absolute_seconds=43200)
    t0 = datetime.fromisoformat(created.created_at)
    assert datetime.fromisoformat(created.last_seen_at) == t0
    assert datetime.fromisoformat(created.idle_expires_at) == t0 + timedelta(seconds=1800)
    assert (datetime.fromisoformat(created.absolute_expires_at)
            == t0 + timedelta(seconds=43200))


def test_touch_session_never_puts_idle_before_last_seen(
    repo: AuthSqliteRepository, ticking,
) -> None:
    """★ 滑动续期后 `idle_expires_at` 必须**正好**是 `last_seen_at + idle_seconds`。

    旧实现两次采样 ⇒ 跨秒时两者差 1 秒，极端情况下 idle 会落在 last_seen 之前
    （一行自相矛盾的会话）。
    """
    repo.create_session(
        session_id="s3", user_id="u1", tenant_id="vip", access_jti="j3",
        refresh_hash="rh", idle_seconds=60, absolute_seconds=43200)
    touched = repo.touch_session("s3", idle_seconds=60)
    assert touched is not None
    last_seen = datetime.fromisoformat(touched.last_seen_at)
    idle = datetime.fromisoformat(touched.idle_expires_at)
    assert idle == last_seen + timedelta(seconds=60), (
        "idle_expires_at 必须正好是 last_seen_at + idle_seconds"
        "（两次采样会让它差 1 秒甚至倒退）")


def test_absolute_cap_is_not_extended_by_sliding(
    repo: AuthSqliteRepository, ticking,
) -> None:
    """滑动只推 idle；绝对上限**一字不动** —— 且与库里那行一致。"""
    created = repo.create_session(
        session_id="s4", user_id="u1", tenant_id="vip", access_jti="j4",
        refresh_hash="rh", idle_seconds=1800, absolute_seconds=43200)
    touched = repo.touch_session("s4", idle_seconds=60)
    assert touched is not None
    assert touched.absolute_expires_at == created.absolute_expires_at
    row = repo.get_session("s4")
    assert row is not None and row.absolute_expires_at == created.absolute_expires_at


# ======================================================================
# 三、同类：其它"一条记录里多个时间列"的写入点
# ======================================================================

def test_remember_token_issued_at_and_expiry_share_one_instant(
    repo: AuthSqliteRepository, ticking,
) -> None:
    """同类写入点：`issued_at` 与 `expires_at` 必须同源（直接读库，不猜读取接口）。"""
    import sqlite3

    from src.infrastructure.repositories.auth_sqlite_repo import TABLE_REMEMBER

    repo.issue_remember(user_id="u1", tenant_id="vip", days=7)
    with sqlite3.connect(repo._db_path) as conn:  # noqa: SLF001
        row = conn.execute(
            f"SELECT issued_at, expires_at FROM {TABLE_REMEMBER} "
            f"ORDER BY rowid DESC LIMIT 1").fetchone()
    assert row is not None, "remember 行没落库"
    issued = datetime.fromisoformat(str(row[0]))
    expires = datetime.fromisoformat(str(row[1]))
    assert expires == issued + timedelta(days=7), (
        "issued_at 与 expires_at 不是同一采样的派生 ⇒ 有效期会漂")


def test_login_failure_records_one_instant(
    repo: AuthSqliteRepository, ticking,
) -> None:
    """锁定与"最后一次失败"必须同一时刻：`locked_until - last_failed_at == lock_seconds`。"""
    repo.set_password("u9", "Acct#2026xyz")     # 建出凭据行
    for _ in range(4):
        repo.record_login_failure("u9", threshold=5, lock_seconds=900)
    assert repo.record_login_failure("u9", threshold=5, lock_seconds=900) == 5
    row = repo.get_credential("u9")
    assert row is not None
    last_failed = datetime.fromisoformat(str(row["last_failed_at"]))
    locked_until = datetime.fromisoformat(str(row["locked_until"]))
    assert locked_until == last_failed + timedelta(seconds=900), (
        "锁定时刻与最后一次失败时刻不是同一采样的派生 ⇒ 跨秒即漂移")
