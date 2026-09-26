"""两个 P0 修复的回归测试（2026-09-25 数据库审计后新增）。

## 一、回填不再写"注定被删"的数据点

审计发现 `fact_data_points` 的**回填范围与保留窗口完全重叠**：
回填写 10 年历史，保留策略删"早于今天 - 10 年"的行 —— 于是刚写进去的
最老那几天立刻被下一次清理删掉，单次回填 60 万行、主库 3.96GB 空闲页
就是这么产生的（`auto_vacuum=0`，文件只涨不缩）。

修复：回填**之前**用同一个截止日过滤（口径同源，见
`retention_passes.points_cutoff_for_years`）。

## 二、"账号不存在的登录"审计不再按请求数放大

`auth/service.py` 里那条 `a_log_notify(scene="login", status="failed")`
原来**无条件写**，且**不受验证码月配额约束** —— 任何人用任意不存在的
账号 `POST /auth/login` 每次写一行。唯一护栏是进程内 IP 限流
（重启即清空），实测单 IP 可达约 1.7 万行/天。

修复：按 (账号, IP) 在窗口内只写一行。审计信号保留，放大被切断。
"""

from __future__ import annotations

import datetime as _dt

from src.core.config import get_settings
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.router import ConnectorRouter
from src.infrastructure.retention_passes import points_cutoff_for_years


def _point(period_date: str) -> DataPoint:
    now = _dt.datetime(2026, 9, 25, 10, 0)
    return DataPoint(
        data_id=f"id-{period_date}", indicator="stock_close:600000", value=1.0,
        unit="CNY", period_date=period_date, extra={},
        source_name="test", source_url="", source_type=DataSourceType.API,
        publish_time=None, fetch_time=now, fetch_method=FetchMethod.API_CALL,
        raw_content_hash="h" * 8, processed_by="test", process_time=now,
        confidence=0.9, verified=False,
    )


def _router() -> ConnectorRouter:
    """只要 `_filter_expired` 这个纯方法，不构造真实路由（那要联网）。"""
    return ConnectorRouter.__new__(ConnectorRouter)


# ==================== 一、回填过滤 ====================


def test_expired_points_are_not_written() -> None:
    """★ 早于保留截止日的点必须被过滤掉 —— 写了也会立刻被下一次清理删掉。"""
    cutoff = points_cutoff_for_years(
        int(getattr(get_settings(), "data_retention_years", 10) or 10))
    old = (_dt.date.fromisoformat(cutoff) - _dt.timedelta(days=30)).isoformat()
    fresh = (_dt.date.fromisoformat(cutoff) + _dt.timedelta(days=30)).isoformat()
    kept = _router()._filter_expired([_point(old), _point(fresh)])  # noqa: SLF001
    assert [p.period_date for p in kept] == [fresh]


def test_points_inside_window_are_kept() -> None:
    """保留期内的点必须原样保留（别把过滤做成"什么都不写"）。"""
    today = _dt.date.today().isoformat()
    points = [_point(today), _point((_dt.date.today() - _dt.timedelta(days=1)).isoformat())]
    kept = _router()._filter_expired(points)      # noqa: SLF001
    assert len(kept) == 2


def test_points_without_period_date_are_kept() -> None:
    """`period_date` 缺失的点要保留 —— 保留策略明确不按日期删这类行。"""
    kept = _router()._filter_expired([_point(""), _point("not-a-date")])  # noqa: SLF001
    assert len(kept) == 2, "缺日期的行不在保留策略的删除范围内，不该被过滤"


def test_filter_fails_open_when_cutoff_unavailable(monkeypatch) -> None:
    """截止日读不到时**放行**（fail-open）：停止回填是功能失效，
    比多写几行被删严重得多。"""
    def _boom(*a, **k):
        raise RuntimeError("config broken")

    monkeypatch.setattr(
        "src.infrastructure.retention_passes.points_cutoff_for_years", _boom)
    points = [_point("2000-01-01")]
    assert _router()._filter_expired(points) == points   # noqa: SLF001


# ==================== 二、登录失败审计去重 ====================


def _service():
    """构造一个最小 AuthService（不连真库），只测去重判定。"""
    from src.domain.auth.service import AuthService

    class _Repo:
        def __getattr__(self, name):
            async def _noop(*a, **k):
                return None
            return _noop

    return AuthService(repo=_Repo(), notifier=None)      # type: ignore[arg-type]


def test_same_account_ip_logged_once() -> None:
    """★ 同一 (账号, IP) 在窗口内只写一行 —— 这是切断放大倍数的关键。"""
    svc = _service()
    first = svc._should_log_login_miss("nobody@example.com", "1.2.3.4")    # noqa: SLF001
    assert first is True, "首次必须立即记账（安全信号不能延后）"
    for _ in range(50):
        assert svc._should_log_login_miss(
            "nobody@example.com", "1.2.3.4") is False                       # noqa: SLF001
    # 50 次扫描只产生 1 行，而不是 50 行


def test_different_ip_same_account_is_logged() -> None:
    """换 IP 扫同一账号要单独记账（分布式扫描的特征不能丢）。"""
    svc = _service()
    assert svc._should_log_login_miss("nobody@example.com", "1.2.3.4") is True   # noqa: SLF001
    assert svc._should_log_login_miss("nobody@example.com", "5.6.7.8") is True   # noqa: SLF001


def test_different_account_same_ip_is_logged() -> None:
    """同 IP 扫不同账号要单独记账（账号维度是审计要看的配对）。"""
    svc = _service()
    assert svc._should_log_login_miss("a@example.com", "1.2.3.4") is True   # noqa: SLF001
    assert svc._should_log_login_miss("b@example.com", "1.2.3.4") is True   # noqa: SLF001


def test_window_expiry_allows_new_log() -> None:
    """窗口过后允许再记一行（否则长期扫描只在第一天留痕）。"""
    import time

    svc = _service()
    assert svc._should_log_login_miss("x@example.com", "1.2.3.4") is True    # noqa: SLF001
    assert svc._should_log_login_miss("x@example.com", "1.2.3.4") is False   # noqa: SLF001
    # 把首记时间往前推过窗口
    key = next(iter(svc._login_miss_seen))                                  # noqa: SLF001
    svc._login_miss_seen[key][0] = time.monotonic() - svc._login_miss_window - 1  # noqa: SLF001
    assert svc._should_log_login_miss("x@example.com", "1.2.3.4") is True    # noqa: SLF001


def test_dedupe_cache_is_bounded() -> None:
    """★ 去重表必须**有界** —— 否则攻击者用随机账号名就能撑爆内存
    （那就把"磁盘填充"换成了"内存填充"）。"""
    svc = _service()
    limit = svc._login_miss_max_keys                                             # noqa: SLF001
    for i in range(limit * 3):
        svc._should_log_login_miss(f"user{i}@example.com", f"10.0.0.{i % 250}")  # noqa: SLF001
    assert len(svc._login_miss_seen) <= limit, \
        f"去重表涨到 {len(svc._login_miss_seen)}，超过上限 {limit}"
