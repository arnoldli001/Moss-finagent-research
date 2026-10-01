"""日K快照预热：把「点开日K先等 4 秒」变成「点开就是热的」。

## 为什么需要它（2026-09-29 实测，不是担心）

| 情形 | 实测耗时 |
|---|---|
| 冷（缓存过期 / 从未预热） | **4.19 s**（payload 96 KB） |
| 热（180 s TTL 内） | **0.07 s** |

`IntradayService.daily()` 的缓存是**进程内、按 code、TTL = `daily_snapshot_ttl`(180 s)**，
而全仓库**唯一的写入点就是 `daily()` 自己**（`service.py` 里 `_daily_cache` 的读写各一处）。
没有任何定时作业会调它：

  · 交易时段每 5 分钟的 `intraday_t_scan` 走的是 `service.watchlist()`（**分钟级**链）；
  · `mainline_warm` 预热的是主线挖掘快照。

于是"服务器一直开着、到了交易时间就自动取数"这件事，对分钟级自选池与主线**成立**，
**对日K不成立** —— 每次冷加载都要现场走一遍日线链 + 250 根 bar 量价规则
+ 30 根 bar 信号回放，用户看到的就是那句「正在取日线并跑量价规则…」。

## 三个上限（写进代码，不留在注释里 —— 取值全是量出来的）

| 上限 | 取值 | 依据（2026-09-29 实测，自选池 50 只） |
|---|---|---|
| 刷新间隔 | 作业 cron `*/2` = **120 s** | 必须**小于** `daily_snapshot_ttl`(180 s)。相等或更大 = 每轮都踩在过期线上（本项目硬约束："预取的续期间隔必须小于缓存 TTL"） |
| 单只并发 | `DAILY_WARM_CONCURRENCY = 3` | 见下表：计算段离开循环后（`CHG-0099`）并发不再放大停顿，而串行 94.5 s **已超预算** |
| 目标总数 | `DAILY_WARM_MAX_CODES = 80` | 与预算配对：约 1.06 s/只 ⇒ 80 只 ≈ 85 s < 90 s |
| 墙钟预算 | `DAILY_WARM_BUDGET_SEC = 90.0` | 必须**小于** tick(120 s)，否则一轮压到下一轮上；实测 50 只 53.1 s，留 37 s 余量 |

### 为什么并发 3（并发度是量出来的，不是拍的）

`CHG-0099` 把日K的计算段移出了事件循环（`analyse_daily` + `_attach_day_extras`
丢线程池，见 `daily.py::fetch_daily_snapshot`），于是"并发会放大停顿"这条约束**消失了**：

| 并发 | 修复**前**（计算占着循环） | | 修复**后**（`CHG-0099`） | |
|---|---|---|---|---|
| | 整轮 | max 停顿 / >1s 次数 | 整轮 | max 停顿 / >1s 次数 |
| 1 | 62.6 s | 1000 ms / 0 | 94.5 s | **109 ms / 0** |
| 2 | 54.9 s | 1890 ms / 12 | 56.0 s | **188 ms / 0** |
| **3（现行）** | 52.0 s | 1906 ms / 11 | **53.1 s** | **188 ms / 0** |

修复后并发 1/2/3 的最大停顿都是 GIL 切换粒度（109~188 ms），与前端红条探针的
4000 ms 差一个数量级 ⇒ 约束只剩"**整轮能不能在 90 s 预算内跑完**"：
串行 94.5 s **已经超预算**（会被截断，尾巴永远是冷的），所以取 **3**（53.1 s）。

## 覆盖范围：自选池 ∪ 最近点开过的（`CHG-0099`）

原先只预热自选池 —— 而用户点开的票可能来自搜索 / 量化选股 / 板块入口，
那些票**第一次仍是冷加载**。现在把"点开过"也记进预热目标
（`IntradayService._remember_daily_view`，有界 `RECENT_DAILY_LIMIT = 20`）：

```
预热目标 = 自选池（置顶在前） + 最近点开过但不在自选池里的（最近在前）
          去重保序，总数封顶 DAILY_WARM_MAX_CODES = 80
```

**诚实边界**：某只非自选票的**第一次**点开仍然是冷的（4.19 s）——
预热只能让"看过第二次及以后"变热，这不是缺陷而是这套机制的固有上限
（除非去猜"用户接下来会点哪只"）。

## 只读与失败语义

本模块只调 `service.daily(code)`（**读路径**）：不写库、不写盘、不推送、不改配置。
预热失败**只记录**，绝不影响任何请求 —— 它是优化，不是前置条件。
唯一的副作用是 `service._daily_cache` 被填热，而那正是它的目的。

⚠️ **不传交互路径的防撞钟预算**（`core.intel_limits.QUERY_DEADLINE_SEC`）：
定时/预热路径正是"重活该在这里做"的地方，掐掉它预热就永远养不起来
（见 `intel_limits` 模块 docstring 与 `tests/unit/test_query_deadline.py`）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from src.core.trading_session import WATCH_WINDOWS, in_window, minutes_of

logger = logging.getLogger(__name__)

#: 预热并发上限（同时最多几只票在取日线）。上限写进代码，不留在注释里。
#:
#: `CHG-0099` 之后取 **3**：计算段已经离开事件循环（`daily.py` 用 `asyncio.to_thread`），
#: 所以"并发放大停顿"不再是约束 —— 实测并发 1/2/3 的最大停顿都是 109~188 ms、
#: **0 次 >1s**，而串行整轮 94.5 s 已超 90 s 预算（会被截断）。详见模块 docstring 的表。
DAILY_WARM_CONCURRENCY = 3

#: 预热目标总数上限（自选池 + 最近点开过）。
#:
#: 与预算一起构成"一轮跑得完"的保证：并发 3 实测约 1.06 s/只 ⇒ 80 只 ≈ 85 s，
#: 仍在 `DAILY_WARM_BUDGET_SEC`(90 s) 内。超出的部分按顺序截断（自选在前）。
DAILY_WARM_MAX_CODES = 80

#: 一轮预热的墙钟预算（秒）。超了就停，剩下的留给下一轮。
#:
#: 必须**小于**作业的 tick 间隔（120 s），否则一轮会压到下一轮上。
#: 实测：串行预热自选 50 只用 **62.6 s**，留 27 s 余量。
#: ⚠️ 池子涨到约 70 只就会撞预算（那时前 70 只热、后面的永远是冷的）——
#: 撞了要调 TTL 与 tick（成对改，`test_warm_interval_is_shorter_than_cache_ttl` 会验），
#: **不要**只把预算调大（调大就等于跨 tick）。
DAILY_WARM_BUDGET_SEC = 90.0

#: 单只票的墙钟上限（秒）。日线链自己也有超时，这一层是"它自己没兜住"时的保险。
#: 取 30 s：实测冷取一只 4.2 s，30 s 只切"病态慢"的那只，不误杀正常源。
DAILY_WARM_PER_CODE_SEC = 30.0


def in_warm_window(now: datetime | None = None) -> bool:
    """当前是否处于**该预热日K**的时段（与前端面板的刷新窗口同口径）。

    用 `core.trading_session.WATCH_WINDOWS`（09:15~11:30 / 13:00~15:00）——
    全站交易时段边界的唯一权威定义。**不再自己算分钟数**：
    那正是这个模块原先在 5 个以上文件里各写一份、改一处漏一处的东西。
    """
    moment = now or datetime.now()
    if moment.weekday() >= 5:      # 周末
        return False
    minutes = minutes_of(moment)
    return any(in_window(minutes, window) for window in WATCH_WINDOWS)


@dataclass
class WarmReport:
    """一轮预热的结果（进运行台账，人话）。"""

    codes: int = 0
    warm_targets: int = 0          #: 来自自选池的只数
    recent_targets: int = 0        #: 来自"最近点开过"的只数
    warmed: int = 0
    failed: int = 0
    skipped: int = 0
    seconds: float = 0.0
    budget_hit: bool = False
    skipped_reason: str = ""
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        """运行台账里那一行说明（失败原因必须可见，不许只报数字）。"""
        if self.skipped_reason:
            return f"跳过预热：{self.skipped_reason}"
        head = (f"目标 {self.codes} 只（自选 {self.warm_targets} + 最近点开 "
                f"{self.recent_targets}）：预热 {self.warmed} 只"
                f"，失败 {self.failed} 只，用时 {self.seconds:.1f}s")
        if self.budget_hit:
            head += (f"；⚠️ 撞上 {DAILY_WARM_BUDGET_SEC:.0f}s 预算上限，"
                     f"{self.skipped} 只留给下一轮")
        if self.errors:
            head += "；失败样例：" + "；".join(self.errors[:3])
        return head


def watchlist_codes(service: Any) -> list[str]:
    """自选池的 6 位代码（去重保序）。

    从 `service.config.watchlist` 读 —— 与前端「自选池」、`intraday_t_scan`
    用的是**同一份配置**，所以"预热的"与"用户会点的"是同一批票。
    """
    try:
        items = list(getattr(service.config, "watchlist", []) or [])
    except Exception as exc:  # noqa: BLE001 配置读不到不该让作业炸
        logger.warning("日K预热读自选池失败：%s", type(exc).__name__)
        return []
    codes: list[str] = []
    seen: set[str] = set()
    for item in items:
        raw = str(getattr(item, "code", "") or "").strip()
        if not raw:
            continue
        code = raw.zfill(6) if raw.isdigit() else raw
        if code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def warm_targets(service: Any, *,
                 recent_limit: int | None = None,
                 max_codes: int = DAILY_WARM_MAX_CODES) -> tuple[list[str], int]:
    """预热目标 = **自选池 ∪ 最近点开过的**（去重保序）。返回 `(代码列表, 自选只数)`。

    顺序即优先级：预算被撞时按顺序截断，所以自选池在前（置顶项又在其最前，
    顺序由用户在自选池里决定），最近点开过的排在其后 —— 自选池是"用户明确收藏的"，
    最近列表是"用户刚看过的"，两者都该热，撞预算时以显式收藏为先。

    为什么需要"最近点开过"（`CHG-0099`）：用户点开的票可能来自搜索 / 量化选股 /
    板块入口，那些票**第一次**仍是冷加载，之后就理应被预热 ——
    记"点开"行为是这里最便宜的信号（零配置、不必猜、不用枚举全市场）。
    """
    watch = watchlist_codes(service)
    seen = set(watch)
    recent: list[str] = []
    try:
        getter = getattr(service, "recent_daily_codes", None)
        candidates = list(getter(recent_limit) if getter else [])
    except Exception as exc:  # noqa: BLE001 读不到最近列表不该让作业炸
        logger.warning("日K预热读最近点开列表失败：%s", type(exc).__name__)
        candidates = []
    for raw in candidates:
        code = str(raw or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        recent.append(code)

    targets = [*watch, *recent]
    if len(targets) > max_codes:
        # 截断按顺序（自选在前）：把"哪些没被预热"变成可预测的事实，
        # 而不是随机丢几只
        targets = targets[:max_codes]
    return targets, min(len(watch), len(targets))


async def _warm_one(service: Any, code: str, sem: asyncio.Semaphore,
                    errors: list[str]) -> bool:
    """预热一只票；成功 True。异常只记录（预热失败不该让整轮判失败）。"""
    async with sem:
        try:
            snapshot = await asyncio.wait_for(
                service.daily(code), timeout=DAILY_WARM_PER_CODE_SEC)
        except asyncio.TimeoutError:
            errors.append(f"{code} 超过 {DAILY_WARM_PER_CODE_SEC:.0f}s")
            return False
        except Exception as exc:  # noqa: BLE001 单只失败不影响其余
            errors.append(f"{code} {type(exc).__name__}")
            return False
        # `available=False` 是"取到了但没数据"（如停牌/新股），不算预热成功：
        # 它**不会**被服务层写进缓存，所以下一轮还会再试一次 —— 如实计数，
        # 别把"没数据"报成"预热好了"。
        return bool(getattr(snapshot, "available", False))


async def warm_watchlist_daily(
    service: Any, *,
    codes: Iterable[str] | None = None,
    budget_sec: float = DAILY_WARM_BUDGET_SEC,
    concurrency: int = DAILY_WARM_CONCURRENCY,
    max_codes: int = DAILY_WARM_MAX_CODES,
    now: datetime | None = None,
) -> WarmReport:
    """把**自选池 ∪ 最近点开过**的日K快照预热进 `service._daily_cache`。

    幂等：缓存 TTL 之内重复调用只会命中缓存（实测 0.07 s/只，不产生网络请求），
    所以同一分钟内被触发两次不会重复取数。

    `codes` 只给测试用（显式指定目标，跳过 `warm_targets`）；生产走自选池 + 最近点开。
    `now` 同理（测试要能钉死"盘中/非盘中"）。
    """
    started = time.monotonic()
    if not in_warm_window(now):
        return WarmReport(skipped_reason="非盯盘时段（09:15~11:30 / 13:00~15:00）")

    if codes is None:
        targets, watch_count = warm_targets(service, max_codes=max_codes)
    else:
        targets = [str(c) for c in codes]
        watch_count = len(targets)
    if not targets:
        return WarmReport(skipped_reason="自选池为空且无最近点开记录")

    report = WarmReport(codes=len(targets), warm_targets=watch_count,
                        recent_targets=len(targets) - watch_count)
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    pending: list[asyncio.Task[bool]] = []
    for code in targets:
        # ★ 预算判据放在**每次启动新的一只之前**：已经在飞的让它跑完
        #   （半路取消会留下"取了一半"的状态，而它并不比跑完更便宜）。
        if time.monotonic() - started >= budget_sec:
            report.budget_hit = True
            break
        pending.append(asyncio.create_task(_warm_one(service, code, sem, report.errors)))

    if pending:
        results = await asyncio.gather(*pending, return_exceptions=True)
        report.warmed = sum(1 for item in results if item is True)
        # 异常在 `_warm_one` 里已吞掉并计数；这里只兜"连计数都没走到"的情形
        report.failed = len(results) - report.warmed

    if report.budget_hit:
        report.skipped = max(0, len(targets) - report.warmed - report.failed)
    report.seconds = time.monotonic() - started
    logger.info("日K预热：%s", report.render())
    return report
