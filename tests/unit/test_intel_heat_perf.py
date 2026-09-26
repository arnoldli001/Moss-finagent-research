"""情报流「平台热议」的两处性能回归。

## 这两条都不是"慢一点"，是**每打开一次页面就白烧几秒**

1. **热榜曾经是接口每次请求实时抓的**（实测 3.0~3.5 秒／次），而
   `intel.py` 的注释一直写着"热榜走定时任务落库，这里只读结果"。
   注释与代码相反，所以谁读代码都会以为这条链路已经优化过了。
   `test_prefers_stored_rank` / `test_refreshes_when_stale`。

2. **`_build_heat` 曾经必然触发第二次六源聚合**：它拿 `feed.items` 当扫描池，
   而那是展示口径（`limit` 截断 + 收容组并成一行），实测只剩 2 条 ——
   于是 `len(pool) < HOT_SCAN_POOL` 恒成立，每个请求 +1.4 秒。
   `test_reuses_scan_pool`。

3. 顺带钉住"**抓失败不能把上一份好榜覆盖成空**" —— 主源是 T-1、
   备源偶发断连，抓失败是常态；覆盖等于我们自己把可用数据删了。
   `test_failed_refresh_keeps_previous_rows`。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from src.api.routes import intel as intel_route
from src.domain.intel import hot_job, hot_rank, hot_scan
from src.domain.intel.service import IntelFeed

#: 池子大小取**真实量级**（实测 126~148 条）。
#:
#: ⚠️ 这里曾经写成 300 —— 恰好等于 `HOT_SCAN_POOL`，于是"池子够大"这个条件
#: 在测试里成立、在生产里**永远不成立**：测试绿了，而每个请求仍在多跑一遍
#: 六源聚合（+1.4 秒）。测试用的样本一旦比阈值"刚好大一点"，
#: 就会把"阈值本身取错了"这件事掩盖掉。
POOL = [{"title": f"快讯 {i}", "summary": "某公司发布公告", "kind": "newswire",
         "published_at": "2026-09-25T10:00:00"} for i in range(140)]


def _stamp(minutes_ago: float = 0) -> str:
    return (datetime.now(timezone.utc).astimezone()
            - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def _install(monkeypatch, *, stored: dict, refreshed: dict | None = None
             ) -> dict[str, int]:
    """把 `_build_heat` 依赖的四个东西全部换成替身，并统计调用次数。"""
    calls = {"build_feed": 0, "refresh": 0, "scan": 0}

    async def fake_build_feed(**_kwargs):
        calls["build_feed"] += 1
        return IntelFeed(items=[])

    async def fake_refresh(**_kwargs):
        calls["refresh"] += 1
        return refreshed if refreshed is not None else stored

    def fake_scan(pool):
        calls["scan"] += 1
        return []

    monkeypatch.setattr(intel_route, "build_feed", fake_build_feed)
    monkeypatch.setattr(hot_rank, "refresh", fake_refresh)
    monkeypatch.setattr(hot_rank, "load", lambda **_kw: stored)
    monkeypatch.setattr(hot_scan, "scan_items", fake_scan)
    monkeypatch.setattr(hot_job, "load", lambda **_kw: {"topics": [], "at": ""})
    return calls


def test_reuses_scan_pool(monkeypatch) -> None:
    """**回归测试**：有 `scan_pool` 就不能再跑一遍六源聚合。

    这是每次请求 +1.4 秒的那一条：`feed.items` 只有 2 条（展示口径），
    拿它当扫描池必然触发兜底聚合。池子用真实量级（见 `POOL` 的说明）——
    写成 300 会让这条测试在"阈值取错"时照样通过。
    """
    calls = _install(monkeypatch, stored={
        "rows": [{"name": "贵州茅台", "code": "600519", "rank": 1}],
        "at": _stamp(), "tried_at": _stamp()})
    feed = IntelFeed(items=[{"title": "只留一条"}], scan_pool=POOL)
    out = asyncio.run(intel_route._build_heat(feed, limit=60))

    assert calls["build_feed"] == 0, "有 scan_pool 就不该再聚合一次"
    assert calls["scan"] == 1
    assert out["stocks_scanned"] == len(POOL)


def test_falls_back_only_when_nothing_to_scan(monkeypatch) -> None:
    """兜底聚合只在**真的没东西可扫**时才跑。

    三级取池：`scan_pool` → `feed.items` → 兜底再聚合一次。
    判据是**看有没有**，不是"看够不够大" —— 拿期望大小当阈值，
    就会出现在"上游只给 148 条、阈值写 300"时条件恒成立、
    每个请求都多跑一遍聚合（这正是这次的性能事故）。
    """
    stored = {"rows": [{"name": "贵州茅台", "code": "600519", "rank": 1}],
              "at": _stamp(), "tried_at": _stamp()}

    # ① 没有 scan_pool，但 items 有内容 → 用 items，不再聚合
    calls = _install(monkeypatch, stored=stored)
    out = asyncio.run(intel_route._build_heat(
        IntelFeed(items=[{"title": "一条"}]), limit=60))
    assert calls["build_feed"] == 0
    assert out["stocks_scanned"] == 1

    # ② 两者都空 → 才允许兜底聚合
    calls = _install(monkeypatch, stored=stored)
    asyncio.run(intel_route._build_heat(IntelFeed(items=[]), limit=60))
    assert calls["build_feed"] == 1


def test_prefers_stored_rank(monkeypatch) -> None:
    """**回归测试**：落盘结果新鲜时**不许**再发网络请求。"""
    calls = _install(monkeypatch, stored={
        "rows": [{"name": "贵州茅台", "code": "600519", "rank": 1}],
        "at": _stamp(minutes_ago=1), "tried_at": _stamp(minutes_ago=1)})
    out = asyncio.run(intel_route._build_heat(
        IntelFeed(items=[], scan_pool=POOL), limit=60))
    assert calls["refresh"] == 0
    assert out["rank"] and out["rank"][0]["name"] == "贵州茅台"
    assert not [g for g in out["gaps"] if g["kind"].startswith("hot_rank")]


def test_refreshes_when_stale(monkeypatch) -> None:
    """过期（超过 `HOT_RANK_MAX_AGE`）才允许现抓一次。"""
    fresh = {"rows": [{"name": "新榜票", "code": "000001", "rank": 2}],
             "at": _stamp(), "tried_at": _stamp()}
    calls = _install(monkeypatch, stored={
        "rows": [{"name": "旧榜票", "code": "600519", "rank": 1}],
        "at": _stamp(minutes_ago=99), "tried_at": _stamp(minutes_ago=99)},
        refreshed=fresh)
    out = asyncio.run(intel_route._build_heat(
        IntelFeed(items=[], scan_pool=POOL), limit=60))
    assert calls["refresh"] == 1
    assert out["rank"][0]["name"] == "新榜票"


def test_retry_rhythm_uses_tried_at(monkeypatch) -> None:
    """**回归测试**：重试节奏看 `tried_at`，不是看 `at`。

    只看 `at` 的话，主源宕机期间每一个请求都会再抓一次 —— 优化原地失效。
    这里：数据是 1 小时前抓到的（很旧），但 1 分钟前刚试过 → **不该重试**。
    """
    calls = _install(monkeypatch, stored={
        "rows": [{"name": "旧但可用", "code": "600519", "rank": 1}],
        "at": _stamp(minutes_ago=60), "tried_at": _stamp(minutes_ago=1)})
    out = asyncio.run(intel_route._build_heat(
        IntelFeed(items=[], scan_pool=POOL), limit=60))
    assert calls["refresh"] == 0, "刚试过就别再试"
    assert out["rank"][0]["name"] == "旧但可用"
    # 但要把"多旧"如实说出来，不能让前端以为是刚抓的
    assert any(g["kind"] == "hot_rank_age" for g in out["gaps"])


def test_failed_refresh_keeps_previous_rows(tmp_path) -> None:
    """**回归测试**：抓失败**不能**把上一份好榜覆盖成空。

    主源是 T-1、备源偶发 `RemoteDisconnected` —— 抓失败是常态。
    失败也覆盖的话，下一个请求拿到空榜，页面显示"人气榜不可用"，
    而上一份完全可用的数据被我们自己删了。
    """
    async def failing():
        res = hot_rank.HotRankResult()
        res.failures = {"hot_focus_em": "RemoteDisconnected"}
        return res

    original = hot_rank.fetch_hot_rank
    hot_rank.fetch_hot_rank = failing            # type: ignore[assignment]
    hot_rank._CACHE.clear()
    try:
        root = tmp_path
        before = _stamp(minutes_ago=30)
        hot_rank.save({"at": before, "tried_at": before,
                       "rows": [{"name": "好榜"}], "sources": ["s"]}, root=root)
        data = asyncio.run(hot_rank.refresh(root=root))
        assert data["rows"] == [{"name": "好榜"}], "失败时不许清掉已有行"
        assert data["at"] == before, "失败时不许改写「最近一次成功」的时间"
        assert data["failures"], "失败原因要记下来（管理员排障）"
        assert data["tried_at"] != before, "尝试时间必须更新，否则每个请求都会重试"
        # 落盘的也是同一份（读回来再确认一次，防止只改了内存里的 dict）
        on_disk = hot_rank.load(root=root, force=True)
        assert on_disk["rows"] == [{"name": "好榜"}]
    finally:
        hot_rank.fetch_hot_rank = original       # type: ignore[assignment]
        hot_rank._CACHE.clear()


def test_age_seconds_handles_both_keys_and_bad_input() -> None:
    data = {"at": _stamp(minutes_ago=10), "tried_at": _stamp(minutes_ago=1)}
    assert 500 < hot_rank.age_seconds(data) < 700
    # `_stamp` 截到秒，所以 1 分钟前可能是 60.x 秒 —— 给足容差
    assert 0 <= hot_rank.age_seconds(data, key="tried_at") < 120
    assert hot_rank.age_seconds({}) is None
    assert hot_rank.age_seconds({"at": "不是时间"}) is None
