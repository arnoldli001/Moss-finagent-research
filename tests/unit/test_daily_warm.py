"""日K快照预热（`daily_warm`）的护栏。

## 这条作业对应的需求原话（用户 2026-09-29）

> 「点击日K 为什么卡在 正在取日线并跑量价规则？理论上只要服务器开着，
>   到了交易时间，就会自动获取数据，而不是冷加载」

实测背景（判据里的数字都是量出来的，不是估的）：
`/api/v1/intraday/daily` **冷 4.19 s / 热 0.07 s**，缓存 TTL 只有 180 s，
而全仓库**唯一的写入点就是 `IntradayService.daily()` 自己** —— 于是"服务器一直开着"
并不等于"日K是热的"。

## 为什么判据是这几条（每条都对着一个已发生过的缺陷形态）

| 判据 | 防的是哪个已实测缺陷 |
|---|---|
| `test_warm_interval_is_shorter_than_cache_ttl` | 本项目硬约束「预取的续期间隔必须**小于**缓存 TTL」；相等 = 每轮都踩在过期线上 |
| `test_execute_*`（走真执行器） | 「registry 声明了、执行器没有分支」→ 每次静默记 `未知作业类型` failed |
| `test_outside_window_*` | 非盯盘时段还去取数 = 白烧数据源（且会把运行台账刷成噪音） |
| `test_available_false_is_not_counted_as_warmed` | 「没量到」当「量到 0」：`available=False` **不进缓存**，算成成功就是假绿 |
| `test_budget_cap_*` / `test_per_code_timeout_*` | 「上限必须写进代码，不能留在注释里」 |
| `test_warm_job_is_read_only` | 只读作业若声明了 `updates`，会被写权限裁剪从**只读实例**上摘掉 |
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

import pytest

from src.core.trading_session import MORNING_CLOSE, minutes_of
from src.intraday.config import IntradayConfig
from src.intraday.warm import (
    DAILY_WARM_BUDGET_SEC,
    DAILY_WARM_CONCURRENCY,
    DAILY_WARM_PER_CODE_SEC,
    in_warm_window,
    warm_watchlist_daily,
)
from src.scheduler.jobs import execute_job
from src.scheduler.registry import JOB_REGISTRY
from src.scheduler.run_log import RunLog
from src.scheduler.service import cron_due

JOB_NAME = "daily_warm"


# ---------------------------------------------------------------- 替身
class _Watch:
    def __init__(self, code: str) -> None:
        self.code = code
        self.name = f"票{code}"


class _Snapshot:
    def __init__(self, available: bool = True) -> None:
        self.available = available


class _FakeConfig:
    def __init__(self, codes: list[str]) -> None:
        self.watchlist = [_Watch(code) for code in codes]


class _FakeService:
    """够用的假做T服务：只实现 `config.watchlist` / `daily()` / `recent_daily_codes()`。

    `fetch_count` 模拟**真实缓存语义**（TTL 内不再取数）—— 幂等判据靠它，
    而不是靠"我看了一遍代码觉得没问题"。
    """

    def __init__(self, codes: list[str], *, ttl: float = 180.0,
                 hang: set[str] | None = None,
                 unavailable: set[str] | None = None,
                 boom: set[str] | None = None,
                 recent: list[str] | None = None) -> None:
        self.config = _FakeConfig(codes)
        self._ttl = ttl
        self._cache: dict[str, tuple[float, _Snapshot]] = {}
        self._hang = set(hang or ())
        self._unavailable = set(unavailable or ())
        self._boom = set(boom or ())
        self._recent = list(recent or [])
        self.calls: list[str] = []

    def recent_daily_codes(self, limit: int | None = None) -> list[str]:
        """最近点开过（最近在前）—— 真服务里的 LRU 在这里退化成固定列表。"""
        return self._recent if limit is None else self._recent[:limit]

    async def daily(self, code: str, *, refresh: bool = False) -> _Snapshot:
        hit = self._cache.get(code)
        if (not refresh and hit is not None
                and time.monotonic() - hit[0] < self._ttl):
            return hit[1]                     # 命中缓存：不产生"取数"
        self.calls.append(code)               # 真的取了一次
        if code in self._boom:
            raise RuntimeError(f"{code} 源炸了")
        if code in self._hang:
            await asyncio.sleep(30)
        snapshot = _Snapshot(available=code not in self._unavailable)
        if snapshot.available:
            self._cache[code] = (time.monotonic(), snapshot)
        return snapshot


# ---------------------------------------------------------------- 1. 间隔 < TTL
def _max_gap_seconds_within_session(cron: str) -> float:
    """盯盘时段内，两次触发之间**最长**隔多久（秒）。

    ⚠️ 必须按"同一交易时段的同一节"分组算：不分组的话
    11:30→13:00（午休）与周五收盘→周一开盘会各自贡献一个巨大的间隔，
    测出来的 max 永远大于 TTL，判据就永远红（假红）。
    """
    start = datetime(2026, 9, 21, 0, 0)        # 周一
    hits: list[datetime] = []
    for index in range(7 * 24 * 60):
        moment = start + timedelta(minutes=index)
        if in_warm_window(moment) and cron_due(cron, moment):
            hits.append(moment)

    buckets: dict[tuple[object, bool], list[datetime]] = {}
    for moment in hits:
        key = (moment.date(), minutes_of(moment) <= MORNING_CLOSE)
        buckets.setdefault(key, []).append(moment)

    worst = 0.0
    for group in buckets.values():
        for earlier, later in zip(group, group[1:]):
            worst = max(worst, (later - earlier).total_seconds())
    return worst


def test_warm_interval_is_shorter_than_cache_ttl() -> None:
    """预热间隔必须**严格小于**缓存 TTL（本项目硬约束）。

    两个数字都**现读**：间隔由 registry 的 cron 在真实时钟上推出来，
    TTL 由 `IntradayConfig` 给 —— 不写死 120/180，否则改口径时这条测试
    只是在测上一版（本项目"配置漂移"的老毛病）。
    """
    cron = JOB_REGISTRY[JOB_NAME].cron
    ttl = float(IntradayConfig().data.daily_snapshot_ttl)

    gap = _max_gap_seconds_within_session(cron)
    assert gap > 0, f"{JOB_NAME} 的 cron 在盯盘时段内一次都不触发：{cron}"
    assert gap < ttl, (
        f"预热间隔 {gap:.0f}s 不小于缓存 TTL {ttl:.0f}s —— "
        "点开日K仍会踩在过期线上（冷加载），预热等于没做")


def test_warm_job_fires_on_weekdays_only() -> None:
    """cron 必须只在工作日触发（周末预热 = 白取数）。"""
    # 2026-09-26 是周六，2026-09-28 是周一
    assert not cron_due(JOB_REGISTRY[JOB_NAME].cron, datetime(2026, 9, 26, 10, 0))
    assert cron_due(JOB_REGISTRY[JOB_NAME].cron, datetime(2026, 9, 28, 10, 0))
    assert cron_due(JOB_REGISTRY[JOB_NAME].cron, datetime(2026, 9, 28, 13, 30))


def test_warm_job_is_read_only() -> None:
    """只读作业**不许**声明 `updates`。

    声明了写存储 ⇒ `job_deny_reason()` 会在没有写权限的实例上把这个作业
    整条裁掉（`CHG-0087` 的派生裁剪）—— 而预热恰恰应该在**每个实例**上跑
    （它填的是本进程的缓存，多实例并存不会互相踩）。
    """
    assert JOB_REGISTRY[JOB_NAME].updates == ()


# ---------------------------------------------------------------- 2. 窗口边界
@pytest.mark.parametrize("moment, expected", [
    (datetime(2026, 9, 28, 9, 14), False),   # 盘前 1 分钟
    (datetime(2026, 9, 28, 9, 15), True),    # 集合竞价起点（闭区间）
    (datetime(2026, 9, 28, 11, 30), True),   # 上午最后一分钟（闭区间）
    (datetime(2026, 9, 28, 11, 31), False),  # 午休
    (datetime(2026, 9, 28, 12, 59), False),  # 午休末
    (datetime(2026, 9, 28, 13, 0), True),    # 下午第一分钟
    (datetime(2026, 9, 28, 15, 0), True),    # 全天最后一分钟
    (datetime(2026, 9, 28, 15, 1), False),   # 收盘后
    (datetime(2026, 9, 26, 10, 0), False),   # 周六
])
def test_warm_window_boundaries(moment: datetime, expected: bool) -> None:
    """窗口边界取闭区间，且与 `core.trading_session.WATCH_WINDOWS` 同源。"""
    assert in_warm_window(moment) is expected


# ---------------------------------------------------------------- 3. 预热行为
def test_warms_every_watchlist_code_in_window() -> None:
    service = _FakeService(["600036", "000001", "300308"])
    report = asyncio.run(warm_watchlist_daily(
        service, now=datetime(2026, 9, 28, 10, 0)))

    assert report.warmed == 3
    assert report.failed == 0
    assert report.codes == 3
    assert sorted(service.calls) == ["000001", "300308", "600036"]


def test_second_run_within_ttl_does_not_refetch() -> None:
    """幂等：TTL 之内重复触发只命中缓存，**不产生新的取数**。

    这条判据的意义在于"作业被触发两次"是常态（重试、手工补跑、多实例），
    幂等靠的是服务层缓存语义，不是靠"记得别重复调"。
    """
    service = _FakeService(["600036", "000001"])
    asyncio.run(warm_watchlist_daily(service, now=datetime(2026, 9, 28, 10, 0)))
    first = len(service.calls)
    asyncio.run(warm_watchlist_daily(service, now=datetime(2026, 9, 28, 10, 1)))

    assert first == 2
    assert len(service.calls) == 2, "第二轮又取了一次数（缓存没被用上）"


def test_outside_window_skips_without_calling_service() -> None:
    """非盯盘时段：一只都不取（白取数会烧数据源，还会把台账刷成噪音）。"""
    service = _FakeService(["600036", "000001"])
    report = asyncio.run(warm_watchlist_daily(
        service, now=datetime(2026, 9, 28, 12, 30)))     # 午休

    assert report.skipped_reason
    assert report.warmed == 0
    assert service.calls == []
    assert "非盯盘时段" in report.render()


def test_empty_watchlist_skips() -> None:
    service = _FakeService([])
    report = asyncio.run(warm_watchlist_daily(
        service, now=datetime(2026, 9, 28, 10, 0)))
    assert "自选池为空" in report.skipped_reason
    assert service.calls == []


# ------------------------------------------------- 3b. 覆盖：自选 ∪ 最近点开
def test_warm_targets_include_recent_not_in_watchlist() -> None:
    """★ 覆盖扩到"最近点开过的"（`CHG-0099`）。

    现场：用户从搜索 / 量化选股 / 板块入口点开的票**不在自选池里**，
    所以它们以前永远是冷的（第一次 4.19 s，之后也还是 4.19 s）。
    """
    from src.intraday.warm import warm_targets

    service = _FakeService(["600036", "000001"],
                           recent=["300308", "600519", "600036"])
    targets, watch_count = warm_targets(service)

    assert targets[:2] == ["600036", "000001"], "自选在前（预算截断时先保它）"
    assert "300308" in targets and "600519" in targets
    assert targets.count("600036") == 1, "自选与最近重叠时必须去重"
    assert watch_count == 2


def test_warm_targets_recent_is_most_recent_first_and_capped() -> None:
    """最近列表按"最近在前"接在自选之后，且总数封顶（上限写进代码）。"""
    from src.intraday.warm import DAILY_WARM_MAX_CODES, warm_targets

    watch = [f"60000{i}" for i in range(5)]
    recent = [f"30000{i}" for i in range(10)]
    service = _FakeService(watch, recent=recent)

    targets, watch_count = warm_targets(service, max_codes=7)
    assert targets[:5] == watch
    assert targets[5:7] == ["30000" + "0", "300001"], "最近点开的按最近在前"
    assert watch_count == 5

    targets_all, _ = warm_targets(_FakeService(watch, recent=recent))
    assert len(targets_all) <= DAILY_WARM_MAX_CODES


def test_recent_code_gets_warmed_end_to_end() -> None:
    """最近点开过的票**真的被预热了**（判据是取数发生了，不是"列表里有它"）。"""
    service = _FakeService(["600036"], recent=["300308"])
    report = asyncio.run(warm_watchlist_daily(
        service, now=datetime(2026, 9, 28, 10, 0)))

    assert "300308" in service.calls
    assert report.warmed == 2
    assert report.warm_targets == 1 and report.recent_targets == 1
    assert "最近点开 1" in report.render()


def test_service_records_recent_daily_views_bounded(monkeypatch) -> None:
    """点开即记、有界、最近在前 —— 记录器本身的行为判据。"""
    from src.intraday.models import DailySnapshot
    from src.intraday.service import RECENT_DAILY_LIMIT, IntradayService

    service = IntradayService()
    service._config = service._config.model_copy(deep=True)  # noqa: SLF001
    service._reload_config = lambda: service._config          # type: ignore[method-assign]  # noqa: SLF001

    async def _fake_fetch(code, name, config, backend, **kwargs):  # noqa: ANN001
        return DailySnapshot(available=True, code=code, name=name,
                             trade_date="2026-09-29")

    monkeypatch.setattr("src.intraday.daily.fetch_daily_snapshot", _fake_fetch)

    async def _run():
        for code in ("600036", "000001", "600036"):
            await service.daily(code)
        for index in range(RECENT_DAILY_LIMIT + 5):
            await service.daily(f"3000{index:02d}")

    asyncio.run(_run())

    recent = service.recent_daily_codes()
    assert len(recent) == RECENT_DAILY_LIMIT, "记录必须有界（否则预热目标会无限长）"
    assert recent[0] == f"3000{RECENT_DAILY_LIMIT + 4:02d}", "最近点开的排最前"
    assert "600036" not in recent, "最早点开的应被挤出去"
    assert service.recent_daily_codes(3) == recent[:3], "limit 参数必须生效"


def test_one_failing_code_does_not_stop_the_rest() -> None:
    """单只失败不影响其余 —— 且失败要**出现在台账文案里**（不许只报数字）。"""
    service = _FakeService(["600036", "000001", "300308"], boom={"000001"})
    report = asyncio.run(warm_watchlist_daily(
        service, now=datetime(2026, 9, 28, 10, 0)))

    assert report.warmed == 2
    assert report.failed == 1
    assert any("000001" in item for item in report.errors)
    assert "000001" in report.render()


def test_available_false_is_not_counted_as_warmed() -> None:
    """「取到了但没数据」**不算**预热成功。

    实测语义：`available=False` 的快照不会被服务层写进 `_daily_cache`
    （`service.py` 只在 `snapshot.available` 时写），所以下一轮还会再取一次。
    把它算成成功 = 用"预热好了"盖住"这只票取不到数"。
    """
    service = _FakeService(["600036", "000001"], unavailable={"000001"})
    report = asyncio.run(warm_watchlist_daily(
        service, now=datetime(2026, 9, 28, 10, 0)))

    assert report.warmed == 1
    assert report.failed == 1


# ---------------------------------------------------------------- 4. 上限
def test_budget_cap_stops_starting_new_codes() -> None:
    """预算耗尽 → 一只都不再**启动**，且如实标 `budget_hit`（留给下一轮）。"""
    service = _FakeService([f"60000{i}" for i in range(6)])
    report = asyncio.run(warm_watchlist_daily(
        service, budget_sec=0.0, now=datetime(2026, 9, 28, 10, 0)))

    assert report.budget_hit is True
    assert report.warmed == 0
    assert report.skipped == 6
    assert "预算上限" in report.render()
    assert service.calls == []


def test_per_code_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """单只病态慢会被自己的上限切掉，不会拖垮整轮（惩罚=这只这次没数据）。"""
    monkeypatch.setattr("src.intraday.warm.DAILY_WARM_PER_CODE_SEC", 0.05)
    service = _FakeService(["600036"], hang={"600036"})
    started = time.monotonic()
    report = asyncio.run(warm_watchlist_daily(
        service, now=datetime(2026, 9, 28, 10, 0)))
    elapsed = time.monotonic() - started

    assert report.failed == 1
    assert elapsed < 2.0, f"单只超时没生效，整轮跑了 {elapsed:.1f}s"


def test_limits_are_written_in_code() -> None:
    """上限必须是代码里的常量（不是注释里的"一般不会超过"）。"""
    assert DAILY_WARM_CONCURRENCY >= 1
    assert 0 < DAILY_WARM_PER_CODE_SEC <= DAILY_WARM_BUDGET_SEC


def test_warm_round_fits_before_the_next_tick() -> None:
    """一轮预热的预算必须**小于** tick 间隔，否则会压到下一轮上。

    tick 间隔从 registry 的 cron **现算**（`_max_gap_seconds_within_session`），
    预算从模块常量读 —— 两个数字都不写死，改任一边都会被这条抓住。
    """
    gap = _max_gap_seconds_within_session(JOB_REGISTRY[JOB_NAME].cron)
    assert DAILY_WARM_BUDGET_SEC < gap, (
        f"预算 {DAILY_WARM_BUDGET_SEC:.0f}s 不小于 tick 间隔 {gap:.0f}s —— "
        "一轮没跑完下一轮又来了（调度器的重叠保护会让它被跳过，"
        "表现为'预热时有时无'）")


def test_warm_concurrency_is_justified_by_measurement() -> None:
    """并发度必须保持 3 —— 取值是**量出来的**，不是保守也不是激进。

    `CHG-0099` 把日K计算段移出了事件循环（护栏见
    `tests/unit/test_daily_freshness.py::test_daily_compute_runs_off_the_event_loop`），
    于是"并发放大停顿"这条约束消失，实测（自选 50 只）：

    | 并发 | 修复前 max 停顿 / >1s | 修复后 max 停顿 / >1s | 修复后整轮 |
    |---|---|---|---|
    | 1 | 1000 ms / 0 | 109 ms / 0 | 94.5 s（**超 90 s 预算**） |
    | 2 | 1890 ms / 12 | 188 ms / 0 | 56.0 s |
    | **3（现行）** | 1906 ms / 11 | **188 ms / 0** | **53.1 s** |

    所以约束只剩"整轮能不能在预算内跑完"：串行已经超预算（会被截断，
    尾巴永远是冷的），取 3。**要改这个值，请先把上表重测一遍。**
    """
    assert DAILY_WARM_CONCURRENCY == 3, (
        "并发度被改了：请先重测整轮用时与事件循环停顿（见本测试 docstring 的表），"
        "确认整轮仍在 DAILY_WARM_BUDGET_SEC 内、且 max 停顿远小于前端探针超时 4000ms")


# ---------------------------------------------------------------- 5. 真执行器
class _Runtime:
    def __init__(self, service: object | None) -> None:
        if service is not None:
            self.intraday = service


def _run_job(tmp_path, service: object | None,
             monkeypatch: pytest.MonkeyPatch) -> dict:
    """走**真执行器** `execute_job`（判据是"作业真的被调到"，不是"函数能跑"）。"""
    monkeypatch.setattr("src.intraday.warm.in_warm_window", lambda now=None: True)
    log = RunLog(str(tmp_path / "runs.jsonl"))
    return asyncio.run(execute_job(_Runtime(service), JOB_NAME, log, "schedule"))


def test_execute_warm_success(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = _run_job(tmp_path, _FakeService(["600036", "000001"]), monkeypatch)
    assert record["status"] == "success"
    assert record["records_processed"] == 2
    assert "预热 2 只" in (record["error_message"] or "")


def test_execute_warm_partial_when_some_fail(tmp_path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    service = _FakeService(["600036", "000001"], boom={"000001"})
    record = _run_job(tmp_path, service, monkeypatch)
    assert record["status"] == "partial", "热了一部分却记成功 = 把缺口吞掉"
    assert record["records_processed"] == 1


def test_execute_warm_failed_when_all_fail(tmp_path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    service = _FakeService(["600036", "000001"], boom={"600036", "000001"})
    record = _run_job(tmp_path, service, monkeypatch)
    assert record["status"] == "failed"


def test_execute_warm_without_service_marks_failed(tmp_path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime 里没有 intraday 服务 → 明确失败（而不是静默跳过）。"""
    record = _run_job(tmp_path, None, monkeypatch)
    assert record["status"] == "failed"
    assert "未装配" in (record["error_message"] or "")
