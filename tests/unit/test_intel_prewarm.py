"""情报流**后台预热**的回归测试（2026-09-25 用户口径）。

## 用户口径

> "服务启动就加载已有数据。用户打开本页就直接加载已有数据，然后后台检查
>   是否是工作日且相比上次抓取间隔2小时，若满足则记录此刻时间并执行一次
>   后台抓取调用…这样就不存在首屏冷启动。"

## 被钉住的四个不变量

1. **判据是"工作日 + 间隔 ≥2 小时"** —— 而且工作日按**本机日历**判，
   不用行情时钟（情报流在非交易时段同样有内容：券商研报盘后与夜里都在发，
   用行情时钟判"休市就不抓"会让周末/节假日的研报整段拿不到 = 功能缺失）。
2. **时间记录必须落盘** —— 只放内存的话进程一重启就归零，于是每次重启都
   立刻重抓，"2 小时间隔"形同虚设（本仓的内存态节流已因此踩过坑）。
3. **只在成功抓取后记时间** —— 失败也记的话，节流会把"失败的这次"当成
   "抓过了"，后面 2 小时都不再尝试 = 静默失效。
4. **预热用的缓存键必须与请求读的键逐字一致** —— 差一点就永远命中不了，
   用户照样吃冷启动，而日志里一个字都不报。
"""

from __future__ import annotations

import asyncio
import datetime as _dt

import pytest

from src.domain.intel import prewarm


@pytest.fixture(autouse=True)
def _isolate_intel_cache_files(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """把预热的三份落盘位置都指到临时目录（autouse）。

    三份都要隔离，因为它们都在**生产实例正在读的同一个目录**
    （`data/cache/intel/`）：

      · `prewarm_state.json`  —— 上次抓取时刻。被测试改掉会让真实预热
        推迟 2 小时或立刻重抓。
      · `feed_payload.json`   —— 上次的 payload 快照。被测试改掉，
        真实实例下次启动会热加载**测试的假数据**。
      · `slow_*.json`         —— 日历/热度的慢聚合缓存。同型风险：
        真实实例启动会把它当真实日程读出来。
    """
    monkeypatch.setattr(prewarm, "DEFAULT_STATE_PATH",
                        tmp_path / "prewarm_state.json")
    monkeypatch.setattr(prewarm, "DEFAULT_PAYLOAD_PATH",
                        tmp_path / "feed_payload.json")
    monkeypatch.setattr(prewarm, "DEFAULT_SLOW_DIR", tmp_path / "slow")
    yield


@pytest.fixture
def state(tmp_path):
    """把"上次抓取时刻"的落盘位置指到临时目录。

    不隔离的话会写到 `data/cache/intel/prewarm_state.json`，而那是**生产实例
    正在读的同一个文件** —— 测试一跑就把生产的节流时间改掉，真实预热会被
    推迟 2 小时（或立刻重抓）。与 `_isolate_intel_source_cache` 同一理由。
    """
    return tmp_path / "prewarm_state.json"


#: 测试用的"现在"：周五 10:00（工作日，确保走"该抓"分支）。
FROZEN = _dt.datetime(2026, 9, 25, 10, 0, 0)


@pytest.fixture(autouse=True)
def _freeze_weekday(monkeypatch: pytest.MonkeyPatch):
    """把 `prewarm` 里的 `datetime.now()` 钉到工作日。

    ⚠️ 为什么必须钉：判据里有"工作日"这一条，而**用例跑在哪一天是不确定的**。
    实测踩到：开发/验证时真实日期是 2026-09-26（周六，中秋假期内），
    于是所有"该抓"的用例都被判成"非工作日"而跳过，表现为 5 个用例
    断言失败 —— 看起来像预热逻辑坏了，实际是用例依赖了真实日历。

    这类"随日历变红"的测试是最讨厌的（周一绿、周末红，且原因指向业务代码）。
    钉住之后本文件与真实日期无关；需要验证周末/间隔判据的用例
    自己传 `now=`（见 `test_weekend_skips_fetch`）。
    """
    class _Frozen(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 与 datetime.now 签名一致
            return cls(2026, 9, 25, 10, 0, 0)

    monkeypatch.setattr(prewarm, "datetime", _Frozen)
    yield


# ==================== 判据 1：工作日 + 间隔 ====================


def test_weekday_is_calendar_based_not_market_clock() -> None:
    """★ "工作日"按**本机日历**判（周一~周五），不看行情时钟。

    为什么不用 `is_trading_day()`：它依赖行情源时钟，而情报流在非交易时段
    同样有内容（券商研报盘后/夜里都在发）。用它判会在周末与节假日
    整段不抓研报 —— 那是功能缺失，不是省成本。
    """
    assert prewarm.is_weekday(_dt.datetime(2026, 9, 25)) is True    # 周五
    assert prewarm.is_weekday(_dt.datetime(2026, 9, 26)) is False   # 周六
    assert prewarm.is_weekday(_dt.datetime(2026, 9, 27)) is False   # 周日


def test_weekend_skips_fetch(state) -> None:
    """周末不抓（但这是**节流**判断，不是"周末没有内容"）。"""
    need, why = prewarm.should_fetch(
        now=_dt.datetime(2026, 9, 26, 10, 0), min_interval_hours=2, path=state)
    assert need is False
    assert "非工作日" in why


def test_no_record_fetches_to_prime(state) -> None:
    """没有上次抓取记录 → 抓一次打底（fail-open）。"""
    assert prewarm.load_last_fetch(path=state) is None
    need, why = prewarm.should_fetch(
        now=_dt.datetime(2026, 9, 25, 10, 0), min_interval_hours=2, path=state)
    assert need is True
    assert "打底" in why


def test_within_interval_skips(state) -> None:
    """距上次不足 2 小时 → 跳过（这是"别自己打自己上游"的核心）。"""
    now = _dt.datetime(2026, 9, 25, 10, 0)
    prewarm.save_last_fetch(now - _dt.timedelta(minutes=30), path=state)
    need, why = prewarm.should_fetch(now=now, min_interval_hours=2, path=state)
    assert need is False
    assert "跳过" in why


def test_after_interval_fetches(state) -> None:
    """距上次已达 2 小时 → 抓。"""
    now = _dt.datetime(2026, 9, 25, 10, 0)
    prewarm.save_last_fetch(now - _dt.timedelta(hours=2, minutes=1), path=state)
    need, why = prewarm.should_fetch(now=now, min_interval_hours=2, path=state)
    assert need is True
    assert "≥" in why


def test_interval_zero_always_fetches(state) -> None:
    """`min_interval_hours=0` = 关闭节流（排查用）。"""
    now = _dt.datetime(2026, 9, 25, 10, 0)
    prewarm.save_last_fetch(now, path=state)
    need, why = prewarm.should_fetch(now=now, min_interval_hours=0, path=state)
    assert need is True
    assert "关闭" in why


# ==================== 判据 2：时间必须落盘（跨重启有效）====================


def test_last_fetch_survives_reload(state) -> None:
    """★ 写进去的时间必须能被重新读出来（跨进程重启有效）。

    只放内存的话进程一重启就归零 → 每次重启都立刻重抓，2 小时间隔形同虚设。
    """
    moment = _dt.datetime(2026, 9, 25, 8, 30, 15)
    assert prewarm.save_last_fetch(moment, path=state) is True
    loaded = prewarm.load_last_fetch(path=state)
    assert loaded is not None
    assert loaded.strftime("%Y-%m-%dT%H:%M:%S") == moment.strftime("%Y-%m-%dT%H:%M:%S")


def test_corrupt_state_fails_open(state) -> None:
    """状态文件损坏 → 当成"从没抓过"（于是会抓一次）。

    反过来 fail-closed（读不到就当刚抓过）会让预热**永远不跑** ——
    界面照常显示，只是数据永远是旧的，属于最坏的静默失效。
    """
    state.write_text("{ not json", encoding="utf-8")
    assert prewarm.load_last_fetch(path=state) is None
    need, _why = prewarm.should_fetch(
        now=_dt.datetime(2026, 9, 25, 10, 0), min_interval_hours=2, path=state)
    assert need is True


# ==================== 判据 3：预热执行与"只在成功时记时间" ====================


def _patch_build(monkeypatch: pytest.MonkeyPatch, *, calls: list[str],
                 boom: bool = False) -> None:
    """把接口层的后台重建换成一个记录器，避免测试真打上游。"""
    from src.api.routes import intel as intel_route

    async def _fake_bg(cache_key, *, watch, limit, sort, filter, hub=None):
        calls.append(cache_key)
        if boom:
            raise RuntimeError("上游挂了")

    monkeypatch.setattr(intel_route, "_build_feed_bg", _fake_bg)


def test_prewarm_fetches_and_records_time(monkeypatch, state) -> None:
    """该抓时：调一次重建 + 记录时间。"""
    calls: list[str] = []
    _patch_build(monkeypatch, calls=calls)
    from src.core.config import Settings

    settings = Settings(intel_prewarm_min_interval_hours=2.0)
    out = asyncio.run(prewarm.prewarm_once(settings=settings, state_path=state))
    assert out["fetched"] is True, out
    assert len(calls) == 1
    assert prewarm.load_last_fetch(path=state) is not None, "抓完必须记时间"


def test_prewarm_uses_same_cache_key_as_request(monkeypatch, state) -> None:
    """★ 预热写进的键必须与**请求读的键**逐字一致。

    差一点（比如 `watch` 传 `[]` 而端点传实际清单）就永远命中不了 ——
    用户照样吃冷启动，而日志里一个字都不报，是最难查的一类。
    """
    from src.api.routes.intel import feed_cache_key

    calls: list[str] = []
    _patch_build(monkeypatch, calls=calls)
    from src.domain.intel.service import DEFAULT_LIMIT

    asyncio.run(prewarm.prewarm_once(state_path=state))
    expected = feed_cache_key(limit=DEFAULT_LIMIT, watch=[], sort="credibility",
                              filter="all")
    assert calls == [expected], f"预热键={calls} 请求键={expected}"


def test_failed_fetch_does_not_record_time(monkeypatch, state) -> None:
    """★ 抓取失败**不得**记时间 —— 否则后面 2 小时都不再尝试（静默失效）。"""
    calls: list[str] = []
    _patch_build(monkeypatch, calls=calls, boom=True)
    out = asyncio.run(prewarm.prewarm_once(state_path=state))
    assert out["fetched"] is False
    assert "抓取失败" in out["reason"]
    assert prewarm.load_last_fetch(path=state) is None, \
        "失败不能记时间，否则 2 小时内不会再试"


def test_prewarm_skips_within_interval(monkeypatch, state) -> None:
    """距上次不足间隔时**根本不调**重建（不白打上游）。"""
    calls: list[str] = []
    _patch_build(monkeypatch, calls=calls)
    # 用冻结的"现在"减去 10 分钟，保证与判据看到的是同一个时间基准
    prewarm.save_last_fetch(FROZEN - _dt.timedelta(minutes=10), path=state)
    out = asyncio.run(prewarm.prewarm_once(state_path=state))
    assert out["fetched"] is False
    assert calls == [], "跳过时不该有任何上游调用"


def test_force_bypasses_the_interval(monkeypatch, state) -> None:
    """`force=True` 绕过节流（给"手动刷新预热"留路）。"""
    calls: list[str] = []
    _patch_build(monkeypatch, calls=calls)
    prewarm.save_last_fetch(FROZEN, path=state)
    out = asyncio.run(prewarm.prewarm_once(state_path=state, force=True))
    assert out["fetched"] is True
    assert len(calls) == 1


# ==================== 判据 4：循环本身 ====================


def test_loop_ticks_and_fetches_once_then_skips(monkeypatch, state) -> None:
    """循环：首轮抓一次并记时间，之后几轮因间隔未到而跳过。"""
    calls: list[str] = []
    _patch_build(monkeypatch, calls=calls)
    from src.core.config import Settings

    settings = Settings(intel_prewarm_min_interval_hours=2.0)
    rounds = asyncio.run(prewarm.run_prewarm_loop(
        settings=settings, state_path=state, tick_seconds=1,
        startup_delay=0, max_rounds=3))
    assert rounds == 1, f"三轮里只该抓一次，实际 {rounds}"
    assert len(calls) == 1


def test_loop_survives_a_failure(monkeypatch, state) -> None:
    """单轮失败不能让循环死掉（下一轮继续）—— 预热是长期后台任务。"""
    calls: list[str] = []
    _patch_build(monkeypatch, calls=calls, boom=True)
    from src.core.config import Settings

    settings = Settings(intel_prewarm_min_interval_hours=0.0)
    rounds = asyncio.run(prewarm.run_prewarm_loop(
        settings=settings, state_path=state, tick_seconds=1,
        startup_delay=0, max_rounds=3))
    assert rounds == 0
    assert len(calls) == 3, "每轮都应尝试（循环没被一次失败打断）"


# ==================== payload 落盘热加载（消除首屏冷启动）====================


def test_payload_roundtrip(tmp_path) -> None:
    """★ payload 落盘后能原样载回，并带回它对应的缓存键。

    "服务启动就加载已有数据"靠的就是这一对函数：载回后写进**同一个键**，
    否则请求读的键与载入的键不一致 —— 用户照样吃冷启动，日志里一个字不报。
    """
    path = tmp_path / "feed_payload.json"
    payload = {"items": [{"title": "x"}], "fetched_at": "2026-09-25T10:00:00",
               "filter": "all", "sort": "credibility"}
    assert prewarm.save_payload(payload, path=path,
                                cache_key="60||credibility|all") is True
    got, key = prewarm.load_payload(path=path)
    assert got == payload
    assert key == "60||credibility|all"


def test_payload_without_items_is_not_loaded(tmp_path) -> None:
    """空 `items` 的 payload 不载回。

    它没有热加载价值（前端还是要等采集完），而且会占住"已有数据"的判断分支，
    让用户看到**一片空白**而不是"正在采集" —— 那比慢更糟。
    """
    path = tmp_path / "p.json"
    prewarm.save_payload({"items": [], "counts": {}}, path=path)
    assert prewarm.load_payload(path=path) == (None, "")


def test_payload_corrupt_fails_open(tmp_path) -> None:
    """文件损坏 → 当成没有缓存（端点照常走空壳 + 后台重建），不能让服务起不来。"""
    path = tmp_path / "p.json"
    path.write_text("{ broken", encoding="utf-8")
    assert prewarm.load_payload(path=path) == (None, "")


def test_prime_cache_makes_first_request_hit(tmp_path) -> None:
    """★ 载回后，接口缓存里就有了**真实数据**（首个请求命中、0 IO）。

    这条是"不存在首屏冷启动"的直接证据：载入前缓存为空（请求会下发空壳），
    载入后同一键有了真实 payload。

    ⚠️ payload 里**必须有 `limit`**（真实的 payload 由 `_feed_public_payload`
    写入）。载入时缓存键是**重算**的而不是信文件里那个（见 `prime_cache`
    的注释：信它的话，键格式一变预热就静默失效），而重算需要
    limit/sort/filter/direction 四个值。缺 `limit` 会退化成"数 items 的条数"——
    这里 items 只有 1 条，于是键变成 `1||credibility|all|all`，
    而请求读的是 `60|...`。
    """
    from src.api.routes import intel as intel_route

    path = tmp_path / "p.json"
    payload = {"items": [{"title": "已有数据"}], "limit": 60,
               "fetched_at": "2026-09-25T09:00:00",
               "filter": "all", "sort": "credibility"}
    prewarm.save_payload(payload, path=path, cache_key="60||credibility|all")

    with intel_route._FEED_LOCK:      # noqa: SLF001 先确认真空
        intel_route._FEED_CACHE.clear()      # noqa: SLF001
    assert prewarm.prime_cache(path=path) is True
    # 键与"端点不传 direction 时会用的那个"必须逐字一致
    from src.api.routes.intel import feed_cache_key

    key = feed_cache_key(limit=60, watch=[], sort="credibility", filter="all")
    entry, age = intel_route._feed_cached(key)   # noqa: SLF001
    assert entry is not None, "载回后必须命中缓存"
    assert entry.payload["items"][0]["title"] == "已有数据"
    # 年龄从**载入**算起（不是文件保存时刻）—— 否则一启动就"已过期"，
    # 首个请求又去触发重建，热加载的意义全没了。
    assert age is not None and age < 5.0


def test_prime_cache_without_file_returns_false(tmp_path) -> None:
    """没有落盘文件时如实返回 False（第一次部署的正常路径）。"""
    assert prewarm.prime_cache(path=tmp_path / "nope.json") is False


# ======================================================================
# 慢聚合落盘缓存（投资日历 / 舆情热度）
#
# 2026-09-26 用户报障："热点&研报小作文 打开界面要等 5-10 秒"。
# 实测根因不在情报流（36 ms），而在同一界面并发拉的另外两条：
#
#     /intel/calendar?horizon_days=45   **11,133 ms**（宏观源按天请求，30 天）
#     /intel/heat                       **3,472 ms**（外部热榜多源重试）
#
# 而这两类内容一天之内几乎不变 —— 却在每次打开页面时重付一遍。
# ======================================================================

def test_slow_cache_roundtrip(tmp_path) -> None:
    """落盘 → 读回：数据一致、年龄接近 0、不算过期。"""
    wrote = prewarm.save_slow("calendar_45", {"events": [{"date": "2026-09-28"}]},
                              directory=tmp_path)
    assert wrote is True

    cache = prewarm.load_slow("calendar_45", ttl_hours=6.0, directory=tmp_path)
    assert cache.hit is True
    assert cache.payload == {"events": [{"date": "2026-09-28"}]}
    assert cache.stale is False
    assert cache.age_seconds < 5.0


def test_slow_cache_missing_file_is_a_miss(tmp_path) -> None:
    """没有文件 = 未命中（路由据此现拉一次）。**fail-open**，不抛异常。"""
    cache = prewarm.load_slow("calendar_45", ttl_hours=6.0, directory=tmp_path)
    assert cache.hit is False
    assert cache.payload is None


def test_slow_cache_corrupt_file_is_a_miss_not_a_crash(tmp_path) -> None:
    """文件损坏 → 当没有缓存。宁可慢一次，也不能让页面永远打不开。"""
    p = prewarm.slow_cache_path("heat", directory=tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ 这不是 JSON", encoding="utf-8")
    cache = prewarm.load_slow("heat", ttl_hours=6.0, directory=tmp_path)
    assert cache.hit is False


def test_slow_cache_version_mismatch_is_a_miss(tmp_path) -> None:
    """结构版本不符必须当未命中。

    否则改了 payload 结构后会热加载一份旧结构，前端在**渲染期**才炸 ——
    那比"没有缓存（慢一次）"难查得多。
    """
    import json

    p = prewarm.slow_cache_path("heat", directory=tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "version": prewarm.SLOW_CACHE_VERSION + 99,
        "saved_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "payload": {"hot_rank": [{"x": 1}]},
    }, ensure_ascii=False), encoding="utf-8")
    assert prewarm.load_slow("heat", ttl_hours=6.0,
                             directory=tmp_path).hit is False


def test_slow_cache_expired_still_returns_payload(tmp_path, monkeypatch) -> None:
    """★ 过期**仍然返回旧数据**，只是同时标 stale。

    这是这一层的核心取舍：宁可给一份几小时前的日程，也不要让用户等 11 秒。
    日程类数据晚几小时没有任何实际影响。

    ⚠️ 时间戳必须相对 `FROZEN` 算，**不能**用 `datetime.now()`：
    本文件的 `_freeze_weekday` 夹具把 `prewarm.datetime` 换成了冻结时钟，
    于是"真实的 9 小时前"在冻结时钟看来可能是在**未来** ——
    `max(0.0, 负数)` 会得到 0.0，表现为"这份 9 小时前的数据是新鲜的"，
    而失败信息只说 `stale is not True`，极难归因到"时钟被冻结了"。
    """
    import json

    p = prewarm.slow_cache_path("heat", directory=tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    old = (FROZEN - _dt.timedelta(hours=9)).isoformat(timespec="seconds")
    p.write_text(json.dumps({"version": prewarm.SLOW_CACHE_VERSION,
                             "saved_at": old,
                             "payload": {"hot_rank": [{"name": "旧热榜"}]}},
                            ensure_ascii=False), encoding="utf-8")

    cache = prewarm.load_slow("heat", ttl_hours=6.0, directory=tmp_path)
    assert cache.hit is True, "过期也必须给数据（否则请求要等上游）"
    assert cache.stale is True
    assert cache.payload == {"hot_rank": [{"name": "旧热榜"}]}
    assert cache.age_seconds > 8 * 3600


def test_slow_cache_ttl_zero_disables_cache(tmp_path) -> None:
    """`ttl_hours=0` = **关闭缓存**（永远 stale），不是"永不过期"。

    这两种语义完全相反；写反的表现是"想临时关缓存却变成永久缓存" ——
    看起来一切正常，只是数据永不更新，最难查的一类问题。
    """
    prewarm.save_slow("heat", {"hot_rank": []}, directory=tmp_path)
    cache = prewarm.load_slow("heat", ttl_hours=0, directory=tmp_path)
    assert cache.stale is True, "0 必须意味着关闭，而不是永久有效"


def test_slow_cache_empty_payload_is_not_written(tmp_path) -> None:
    """空 payload 不落盘：空壳没有缓存价值，还会占着"命中了"这个分支。"""
    assert prewarm.save_slow("heat", {}, directory=tmp_path) is False
    assert prewarm.load_slow("heat", ttl_hours=6.0,
                             directory=tmp_path).hit is False


def test_slow_cache_names_do_not_collide(tmp_path) -> None:
    """日历与热度各占一个文件，互不覆盖。"""
    prewarm.save_slow("calendar_45", {"events": [1]}, directory=tmp_path)
    prewarm.save_slow("heat", {"hot_rank": [2]}, directory=tmp_path)
    assert prewarm.load_slow("calendar_45", ttl_hours=6,
                             directory=tmp_path).payload == {"events": [1]}
    assert prewarm.load_slow("heat", ttl_hours=6,
                             directory=tmp_path).payload == {"hot_rank": [2]}
