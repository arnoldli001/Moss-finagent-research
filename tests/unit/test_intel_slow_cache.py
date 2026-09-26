"""慢聚合端点（投资日历 / 舆情热度）缓存行为的回归测试。

## 为什么单独立一个文件

2026-09-26 用户报障："热点&研报小作文 打开界面要等 5-10 秒"。

量出来的真凶**不是情报流**（`/feed` 只要 36 ms），而是同一界面并发拉的
另外两条：

    /intel/calendar?horizon_days=45   **11,133 ms**  675 KB
    /intel/heat                       **3,472 ms**

日历 11 秒的构成（逐源计时）：宏观 **8,731 ms**、财报 2,402 ms、解禁
1,137 ms、交易日 852 ms。宏观占八成的原因是它**按天逐个请求**财经日历
（30 天 ≈ 30 次 HTTP）。

而这两类内容一天之内几乎不变 —— 于是本层给它们加了小时级落盘缓存。
这里钉住的就是那个契约，**四条**都不能破：

1. **命中且新鲜 → 绝不调用上游**（这是"打开就出来"的全部依据）。
2. **过期 → 仍然返回旧数据**，同时**后台**续期（请求不等待）。
3. **未命中 → 现拉一次并落盘**（首次部署的正常路径）。
4. **同键并发只续期一次**（单飞）—— 用户连点刷新不该变成连打上游。
"""

from __future__ import annotations

import asyncio

import pytest

from src.api.routes import intel as intel_route
from src.domain.intel import prewarm


@pytest.fixture()
def slow_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """把慢聚合缓存目录指到临时目录（**必须**，否则会写生产文件）。"""
    d = tmp_path / "slow"
    monkeypatch.setattr(prewarm, "DEFAULT_SLOW_DIR", d)
    with intel_route._FEED_LOCK:                 # noqa: SLF001
        intel_route._SLOW_BUILDING.clear()       # noqa: SLF001
        intel_route._SLOW_TASKS.clear()          # noqa: SLF001
    return d


def _calls_counter(payload: dict):
    """返回一个"被调用就 +1"的协程构造器 + 计数器。"""
    counter = {"n": 0}

    async def _build() -> dict:
        counter["n"] += 1
        return dict(payload)

    return _build, counter


# ======================================================================
# 第 1 条：命中且新鲜 → 零上游
# ======================================================================

def test_fresh_cache_returns_without_calling_upstream(slow_dir) -> None:
    """★ "打开就出来"的全部依据：新鲜的缓存**一次上游都不打**。"""
    prewarm.save_slow("heat", {"hot_rank": [{"name": "缓存里的热榜"}]})
    build, counter = _calls_counter({"hot_rank": [{"name": "现拉的"}]})

    got = asyncio.run(intel_route._slow_payload("heat", build))  # noqa: SLF001

    assert counter["n"] == 0, "命中新鲜缓存时绝不许调上游"
    assert got["hot_rank"] == [{"name": "缓存里的热榜"}]
    assert got["cache_age_seconds"] < 5.0
    # 没有起后台任务（没触发续期）
    assert not intel_route._SLOW_BUILDING        # noqa: SLF001


def test_cache_hit_does_not_mutate_the_stored_payload(slow_dir) -> None:
    """读缓存时**不能改到落盘那份** —— 否则 `cache_age_seconds` 会被写进去，
    下次读出来它就成了数据的一部分（这种"缓存里混进元信息"极难发现）。"""
    prewarm.save_slow("heat", {"hot_rank": []})
    build, _ = _calls_counter({})
    asyncio.run(intel_route._slow_payload("heat", build))        # noqa: SLF001

    again = prewarm.load_slow("heat", ttl_hours=6.0)
    assert "cache_age_seconds" not in (again.payload or {}), (
        "缓存文件被读操作污染了")


# ======================================================================
# 第 2 条：过期 → 先给旧的，后台续期
# ======================================================================

def test_stale_cache_serves_old_data_and_renews_in_background(
    slow_dir, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 本层的核心取舍：宁可给几小时前的日程，也不让用户等 11 秒。

    所以过期时**同步返回旧数据**，续期丢到后台 —— 请求本身不等它。
    """
    # 直接落一份，然后把 TTL 设成 0 → 一律视为过期（= 关闭缓存语义）
    prewarm.save_slow("calendar_45", {"events": [{"date": "旧日程"}]})
    monkeypatch.setattr(intel_route, "_slow_ttl_hours", lambda: 0)
    build, counter = _calls_counter({"events": [{"date": "新日程"}]})

    async def _main() -> dict:
        got = await intel_route._slow_payload("calendar_45", build)  # noqa: SLF001
        # 后台续期任务要跑完，断言才稳定（生产上不等它）
        for _ in range(50):
            if counter["n"] > 0 and not intel_route._SLOW_BUILDING:  # noqa: SLF001
                break
            await asyncio.sleep(0.02)
        return got

    got = asyncio.run(_main())

    assert got["events"] == [{"date": "旧日程"}], "过期也必须先给旧数据"
    assert counter["n"] == 1, "过期应当触发一次后台续期"
    # 续期结果已落盘 → 下一次请求就是新鲜的
    refreshed = prewarm.load_slow("calendar_45", ttl_hours=6.0)
    assert refreshed.payload == {"events": [{"date": "新日程"}]}


def test_concurrent_renew_requests_only_hit_upstream_once(
    slow_dir, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 单飞：同键并发续期只打一次上游。

    用户连点刷新会产生几十个请求；没有单飞就是几十次 11 秒级的上游拉取
    （"自己打自己的上游"在本项目是有过代价的教训）。
    """
    prewarm.save_slow("calendar_45", {"events": [{"date": "旧"}]})
    monkeypatch.setattr(intel_route, "_slow_ttl_hours", lambda: 0)
    build, counter = _calls_counter({"events": [{"date": "新"}]})

    async def _main() -> None:
        await asyncio.gather(*[
            intel_route._slow_payload("calendar_45", build)   # noqa: SLF001
            for _ in range(8)
        ])
        for _ in range(50):
            if counter["n"] > 0 and not intel_route._SLOW_BUILDING:  # noqa: SLF001
                break
            await asyncio.sleep(0.02)

    asyncio.run(_main())
    assert counter["n"] == 1, f"8 个并发请求应只续期一次，实际 {counter['n']} 次"


# ======================================================================
# 第 3 条：未命中 → 现拉并落盘
# ======================================================================

def test_miss_builds_and_persists(slow_dir) -> None:
    """首次部署的正常路径：现拉一次，并落盘给后续请求/下次启动用。"""
    build, counter = _calls_counter({"events": [{"date": "现场拉的"}]})

    got = asyncio.run(intel_route._slow_payload("calendar_45", build))  # noqa: SLF001

    assert counter["n"] == 1
    assert got["events"] == [{"date": "现场拉的"}]
    assert got["cache_age_seconds"] == 0.0
    assert prewarm.load_slow("calendar_45", ttl_hours=6.0).payload is not None, (
        "现拉的结果必须落盘，否则下一个用户又要等 11 秒")


def test_build_failure_propagates_to_caller(slow_dir) -> None:
    """未命中且现拉失败 → 异常必须抛给路由（由它转成 503 + 明确文案）。

    ⚠️ 与"后台续期失败"要分开：后台失败只能记日志（请求早已返回），
    而**未命中时的失败就是这次请求的失败** —— 静默返回 `{}` 会让前端
    显示一个空日历，用户以为是"今天没有日程"。
    """
    async def _boom() -> dict:
        raise RuntimeError("上游挂了")

    with pytest.raises(RuntimeError):
        asyncio.run(intel_route._slow_payload("calendar_45", _boom))  # noqa: SLF001


# ======================================================================
# 预热：启动期只报状态，重建在后台
# ======================================================================

def test_prime_slow_reports_not_ready_without_cache(slow_dir) -> None:
    """没有缓存时 `prime_slow` 如实返回 False（并打日志说明首屏可能慢一次）。"""
    assert prewarm.prime_slow() is False


def test_prime_slow_detects_ready_cache(slow_dir) -> None:
    """有新鲜缓存时返回 True —— 这就是"首屏无需现拉"的判据。"""
    prewarm.save_slow("calendar_45", {"events": []})
    prewarm.save_slow("heat", {"hot_rank": []})
    assert prewarm.prime_slow() is True


def test_prewarm_slow_rebuilds_only_when_stale(
    slow_dir, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预热判据是**缓存文件年龄**，与 feed 的"2 小时间隔"无关。

    新鲜 → 跳过（不打上游）；TTL=0（一律过期）→ 重建。
    """
    from src.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "intel_slow_cache_hours", 6.0, raising=False)
    prewarm.save_slow("calendar_45", {"events": []})
    prewarm.save_slow("heat", {"hot_rank": []})

    out = asyncio.run(prewarm.prewarm_slow(settings=settings))
    assert sorted(out["skipped"]) == ["calendar_45", "heat"]
    assert out["rebuilt"] == []
