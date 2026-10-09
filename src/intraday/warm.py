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
| 单只并发 | `DAILY_WARM_CONCURRENCY = 3` | 见下表：计算段离开循环后（`CHG-0099`）并发不再放大停顿；`CHG-0137` **保持 3**（改大须先重测下表，判据会拦） |
| 目标总数 | `DAILY_WARM_MAX_CODES = 80` | 只决定"提交多少只"；能不能跑完由**硬预算**决定（见下） |
| 墙钟预算 | `DAILY_WARM_BUDGET_SEC = 90.0` | 必须**小于** tick(120 s)；**`CHG-0137` 起由代码强制**（`wait_for(gather, 剩余预算)`），撞了取消在飞任务 + 记 `job_budget` 异常 |
| 单只上限 | `DAILY_WARM_PER_CODE_SEC = 12.0` | `CHG-0137`：30 s 与 90 s 预算**互相矛盾**（22 只病态 × 30 s ÷ 并发 3 ⇒ 220 s 打底）；12 s = 实测冷取 4.2 s 的 2.9 倍 |

### ★ 最坏情况算术（`CHG-0137` 补上的一课）

上面那张表原来只算**理想情况**："约 1.06 s/只 ⇒ 80 只 ≈ 85 s < 90 s"。
但最坏情况是 `ceil(目标数 ÷ 并发) × 单只超时`：

| 版本 | 最坏一轮 | 相对预算 | 后果 |
|---|---|---|---|
| 改前（并发 3 / 单只 30 s） | `ceil(80/3) × 30` = **810 s** | **9 倍** | 且**代码不强制预算**（见下）⇒ 实测 56 只跑了 **316.4 s**，占住事件循环 ⇒ 前端探针超时 ⇒ 用户报"后端不可达" |
| 改后（并发 5 / 单只 12 s） | `ceil(80/5) × 12` = **192 s** | 2.1 倍 | **但预算被代码强制** ⇒ 一轮**不超过 90 s** + 收尾；少预热的只数如实计入 `skipped` 并记异常 |

**为什么"文档写了预算"还不够**：原实现在**提交下一只之前**看预算，而
`asyncio.create_task` 是**非阻塞**的 ⇒ 提交循环**毫秒级跑完** ⇒ 那条判据几乎永不触发，
而 `gather` 没有任何时间上限。**"写在文档里的上限"必须同时是"代码里的强制"**，
否则它只在一个理想世界里成立 —— 这次事故就是这句话的实证。

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
#: ★ `CHG-0149`（2026-09-30）：**3 → 1**。这是**安全性**修复，不是性能取舍。
#:
#: 实测（`scripts/_probe_v8_concurrency.py`，判据=退出码）：
#: **并发 3 → 3/3 次崩溃**（退出码 `0x80000003` STATUS_BREAKPOINT，无 traceback，
#: 原生栈落在 `py_mini_racer/mini_racer.dll`）；**串行 → 0/3 次崩溃**。
#: 机制：日线链有一跳用 `py_mini_racer`（V8）跑反爬 JS，而链路是
#: `asyncio.to_thread` 执行的 ⇒ 并发 3 = **3 个线程同时进 V8** ⇒ 硬崩。
#: 这也解释了 pilot 那些"无痕死亡"（强杀/原生崩溃都不留 traceback）。
#:
#: 代价与配套：整轮 ~53 s → **~94.5 s**，会撞 90 s 预算 ⇒ 与
#: `akshare_connector` 里的 **V8 串行闸门**（`_V8_GATE`）配套；闸门到位后
#: 可以再谈把并发提回去（届时**第一判据是崩溃率**，不是停顿）。
DAILY_WARM_CONCURRENCY = 1

#: 预热目标总数上限（自选池 + 最近点开过）。
#:
#: ⚠️ `CHG-0137` **更正一处旧口径**：原文写"并发 3 实测约 1.06 s/只 ⇒ 80 只 ≈ 85 s，
#: 仍在 90 s 预算内"—— 那是**理想情况**（每只都快）。最坏情况是
#: `ceil(80/3) × 30 s 超时 = 810 s`，**比 90 s 预算大 9 倍**；
#: 而当时**代码并不强制预算**（提交前看一眼，而 `create_task` 非阻塞 ⇒ 判据几乎不触发），
#: 所以"预算"在过去只是**文档里的假设**。现在预算由 `warm_watchlist_daily` 用
#: `asyncio.wait_for(gather, 剩余预算)` **强制**，本上限只决定"提交多少只"。
DAILY_WARM_MAX_CODES = 80

#: 一轮预热的墙钟预算（秒）。超了就停，剩下的留给下一轮。
#:
#: 必须**小于**作业的 tick 间隔（120 s），否则一轮会压到下一轮上。
#: 实测：串行预热自选 50 只用 **62.6 s**，留 27 s 余量。
#: ⚠️ 池子涨到约 70 只就会撞预算（那时前 70 只热、后面的永远是冷的）——
#: 撞了要调 TTL 与 tick（成对改，`test_warm_interval_is_shorter_than_cache_ttl` 会验），
#: **不要**只把预算调大（调大就等于跨 tick）。
#:
#: ★ `CHG-0137`：**这个常量现在是硬约束**（见 `warm_watchlist_daily` 的
#: `asyncio.wait_for`）。撞预算会：① 取消在飞任务 ② 记一条 `job_budget` 采集异常
#: ③ 在 `WarmReport` 里如实计数 —— 不再靠人读日志发现"这一轮少预热了几只"。
#:
#: ★★ `CHG-0221`（2026-10-08）：**90 → 45**。起因是用户报"自选股的分时图加载很慢"，
#: 量出来的账是：**预热占掉了 API 进程 70% 的墙钟**（09:31–10:28 实测 29 轮、合计
#: 2,396 s / 3,414 s；`[循环延迟]` 超阈值 378 次），而用户点开一只票的
#: `/intraday/snapshot` 在同一窗口是 **p50 = 3,670 ms、max = 25,965 ms**。
#:
#: 关键在于**旧的 90 s 并没有换来覆盖**：`CHG-0149` 把并发钉成 **1**
#: （并发 3 时 V8 崩溃 3/3，见 `DAILY_WARM_CONCURRENCY`），于是 57~64 只的池子
#: 串行取一轮要 ~95 s+ ⇒ 实测 **25/29 轮全部撞满 90 s**，每轮只热到 14~43 只，
#: 尾巴**永远**是冷的。也就是说：那 90 s 买到的不是覆盖，而是"每一轮都跑满"。
#:
#: 45 s = tick 的 37.5%（`test_warm_budget_leaves_headroom_in_the_tick` 会验），
#: 与 `PER_CODE_SEC(12)` 的搭配依然成立。**这一半不是根治**：真正让"用户进程"
#: 与"后台批处理"分开的是把预热搬到 `manage.py start-worker`（已登记待办），
#: 在那之前，保护交互路径靠的是 `yield_to_interactive()`（见下）。
DAILY_WARM_BUDGET_SEC = 45.0

#: 让路的轮询间隔（秒）。交互请求通常 0.1~4 s 就走完，0.2 s 足够跟手又不会空转。
DAILY_WARM_YIELD_POLL_SEC = 0.2

#: 单只票**最多**为交互请求让路多久（秒）。
#:
#: 为什么必须有上限：用户连续点击时，"一直有请求在飞"是常态 —— 没有上限
#: 就等于把预热停掉（那又回到"点开一只冷票要 4~7 s"的原点）。
#: 3 s ≈ 一次冷取（4.2 s）的 0.7 倍：让过这一波，下一只继续。
DAILY_WARM_YIELD_MAX_SEC = 3.0

#: 单只票的墙钟上限（秒）。日线链自己也有超时，这一层是"它自己没兜住"时的保险。
#:
#: ★ `CHG-0137`：**30 s → 12 s**。原因不是"12 s 更好"，而是 30 s 与预算**互相矛盾**：
#: 上游一挂（实测腾讯 501 / AkShare 返空），22 只 × 30 s / 并发 3 ⇒ 220 s 打底，
#: 预算 90 s 根本不可能成立。12 s = 实测冷取一只（4.2 s）的 **2.9 倍**，
#: 仍只切"病态慢"的那只；而"一只病态"对整轮的伤害从 30 s 降到 12 s。
#: 真正的护栏是硬预算：即使 12 s 也不够，一轮也**不会**超过 90 s。
DAILY_WARM_PER_CODE_SEC = 12.0


async def yield_to_interactive(
    *,
    poll_sec: float = DAILY_WARM_YIELD_POLL_SEC,
    max_sec: float = DAILY_WARM_YIELD_MAX_SEC,
) -> float:
    """有**交互请求**在飞时让路；返回实际等了多少秒（0 = 没人在等）。

    ## 为什么需要它（`CHG-0221`，用户原话"自选股的分时图加载很慢"）

    预热的本意是"让用户点开快"，而它与用户**共享同一条事件循环、同一批数据源
    槽位、同一个线程池**。实测代价（2026-10-08，pilot）：

        · 预热占掉 API 进程 **70%** 的墙钟（29 轮 / 2,396 s / 3,414 s）
        · 同一窗口用户点票的 `/intraday/snapshot`：**p50 3,670 ms、max 25,965 ms**
        · 同一份代码在**空闲**进程上只要 **144 ms**（light）/ 487 ms（完整）

    也就是说：**批量任务在跟它服务的那个请求抢资源** —— 这是"预热养缓存"
    这个设计自带的矛盾。修法不是把预热关掉（那会让每只票都冷），而是让它
    **知道自己是可以等的那一个**。

    ## 判据用的是 `inflight.interactive()`，不是"登记簿非空"

    见 `src/core/inflight.py::interactive()`：WebSocket 是长连接（实测挂过 38 分钟），
    拿它当判据会让预热**永远**让路；`task:...` 是别的后台任务，后台给后台让路没有意义。

    ## 三条边界

    * **空闲时零开销**：没有交互请求就只读一次登记簿（O(1) 字典）直接返回；
    * **有上限**：用户连续点击时"一直有请求在飞"是常态，没有上限就等于停掉预热
      （见 `DAILY_WARM_YIELD_MAX_SEC`）；
    * **绝不抛**：登记簿是观测器，它出问题不许把预热带崩。
    """
    waited = 0.0
    while waited < max_sec:
        try:
            from src.core import inflight

            busy = inflight.interactive()
        except Exception:  # noqa: BLE001 观测器故障不许影响预热
            return waited
        if not busy:
            return waited
        step = min(poll_sec, max_sec - waited)
        await asyncio.sleep(step)
        waited += step
    return waited


def _record_warm_budget_anomaly(report: "WarmReport", budget_sec: float) -> None:
    """撞预算时记一条 `job_budget` 采集异常（`CHG-0137`）。

    为什么必须留痕：撞预算意味着**这一轮有几只没预热到**（下一轮它们仍是冷的），
    而原来这件事**只有一行 INFO 日志**。用户口径是"不许静默降级"；
    更实际的理由是：本次事故里正是这一条把"服务不可达"和"预热跑不完"连起来的，
    没有它，两次报障之间只能靠人翻日志。
    """
    try:
        from src.core.collection_anomalies import record

        record(
            "job_budget", "daily_warm",
            f"日K预热一轮撞上 {budget_sec:.0f}s 墙钟预算：目标 {report.codes} 只、"
            f"预热 {report.warmed} 只、失败 {report.failed} 只、"
            f"未轮到 {report.skipped} 只，用时 {report.seconds:.1f}s。"
            "⚠️ 撞预算说明**这一轮占满的事件循环时间已达上限**（前端 4s 探针会超时），"
            "且未预热的票下一轮仍是冷的。先看是不是上游源在挂"
            "（失败样例见 `日K预热：` 那行日志），再决定调并发/单只超时/池子上限。")
    except Exception:  # noqa: BLE001 观测失败不影响预热
        logger.debug("预热预算异常落盘失败", exc_info=True)


#: 上一轮**没热成**（超时/异常）的标的，在这么多秒内被**降到队尾**（不是剔除）。
#:
#: `CHG-0145`。为什么需要它（pilot 实测 2026-09-30）：慢实例上总有 6~10 只票
#: 每轮都撞单只超时（`688825 / 300308 / 603083 / 600667 / 301511 …` 跨轮次重复），
#: 而它们**每只都要吃掉一次超时**（12 s × 6 只 ÷ 并发 3 ≈ 24 s）⇒
#: 90 s 预算里 1/4 被"注定失败的票"占掉，结果整轮只热了 22~37/56 ——
#: **健康的多数被少数拖累**。
#:
#: 降级而不是剔除：① 上游抖动是常见的，剔除会让一只票**永久**冷；
#: ② 队尾仍然会在预算有富余时被轮到；③ 窗口 900 s > 缓存 TTL 180 s 的 5 倍，
#: 足够让一次真实抖动过去。
FAILURE_DEFER_SEC = 900.0

#: `{code: 最近一次失败时刻}`。**只是排序提示**，不是正确性依赖 ——
#: 所以进程内、重启即清空是可接受的（与 `_daily_cache` 同类）。
_recent_failures: dict[str, float] = {}


def note_failure(code: str, *, now: float | None = None) -> None:
    """记一次"这只票这一轮没热成"（超时/异常），并顺手清掉过期条目。"""
    moment = time.monotonic() if now is None else now
    _recent_failures[str(code)] = moment
    for stale in [item for item, ts in _recent_failures.items()
                  if moment - ts >= FAILURE_DEFER_SEC]:
        _recent_failures.pop(stale, None)


def note_success(code: str) -> None:
    """热成功 ⇒ 立刻恢复常规优先级（下一轮不必再排队尾）。"""
    _recent_failures.pop(str(code), None)


def deferred_codes(*, now: float | None = None) -> set[str]:
    """当前仍在"降级窗口"内的标的。"""
    moment = time.monotonic() if now is None else now
    return {item for item, ts in _recent_failures.items()
            if moment - ts < FAILURE_DEFER_SEC}


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
    deferred: int = 0          #: 因**上一轮超时**而被降到队尾的只数
    #: 这一轮为**交互请求**让路让掉的时间（秒，`CHG-0221`）。
    #: 必须可见：不然"让路生效了吗"只能靠猜，而它恰恰是这一轮改动的判据。
    yielded_sec: float = 0.0
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        """运行台账里那一行说明（失败原因必须可见，不许只报数字）。"""
        if self.skipped_reason:
            return f"跳过预热：{self.skipped_reason}"
        head = (f"目标 {self.codes} 只（自选 {self.warm_targets} + 最近点开 "
                f"{self.recent_targets}）：预热 {self.warmed} 只"
                f"，失败 {self.failed} 只，用时 {self.seconds:.1f}s")
        if self.yielded_sec >= 0.5:
            head += f"（其中让路给交互请求 {self.yielded_sec:.1f}s）"
        if self.deferred:
            head += f"；{self.deferred} 只因上轮超时被降到队尾"
        if self.budget_hit:
            head += (f"；⚠️ 撞上 {DAILY_WARM_BUDGET_SEC:.0f}s 预算上限，"
                     f"{self.skipped} 只留给下一轮")
        if self.errors:
            head += "；失败样例：" + "；".join(self.errors[:3])
        return head


def watchlist_codes(service: Any) -> list[str]:
    """自选池的 6 位代码（去重保序）。

    **2026-10-08（`CHG-0224`）起改成"全体账号的并集"**：自选已按账号隔离，
    后台预热没有"谁"这回事，要热的是**所有人在看的票**；并集为空才回落
    `configs/intraday.yaml`（老机器兼容路径，见 `src/intraday/user_watch.py`）。
    """
    try:
        codes = [str(code) for code in service.all_watch_codes()]
    except Exception as exc:  # noqa: BLE001 配置读不到不该让作业炸
        logger.warning("日K预热读自选池失败：%s", type(exc).__name__)
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in codes:
        code = raw.strip()
        if not code:
            continue
        code = code.zfill(6) if code.isdigit() else code
        if code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


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
    # ★ `CHG-0145`：上一轮超时的票**降到队尾**（不剔除）—— 见 `FAILURE_DEFER_SEC`。
    #   预算被撞时按顺序截断，所以"降级"实际效果就是"预算先给健康的票"。
    deferred = deferred_codes()
    if deferred:
        targets = [*[c for c in targets if c not in deferred],
                   *[c for c in targets if c in deferred]]
    return targets, min(len(watch), len(targets))


async def _warm_one(service: Any, code: str, sem: asyncio.Semaphore,
                    errors: list[str],
                    gate: Any = None) -> bool:
    """预热一只票；成功 True。异常只记录（预热失败不该让整轮判失败）。

    `gate`：可选的 `async () -> float`，**在取数之前**调用 —— 有交互请求在飞时
    它会等（见 `yield_to_interactive`）。⚠️ 它在 `wait_for` **之外**：
    让路的时间不该算进"这一只票取数超时"（那是两件事，混在一起会让
    "让路让多了"伪装成"数据源超时"）。
    """
    async with sem:
        if gate is not None:
            try:
                await gate()
            except Exception:  # noqa: BLE001 让路失败照常预热，绝不因此少热一只
                logger.debug("预热让路失败（照常继续）", exc_info=True)
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
                        recent_targets=len(targets) - watch_count,
                        deferred=len([c for c in targets if c in deferred_codes()]))
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    # ★ `CHG-0221`：让路记账。用 dict 而不是 nonlocal ——
    #   `_warm_one` 是模块级函数（测试直接调它），闭包变量传不进去。
    yielded = {"sec": 0.0}

    async def gate() -> None:
        waited = await yield_to_interactive()
        if waited:
            yielded["sec"] += waited

    pending: list[asyncio.Task[bool]] = []
    for code in targets:
        # ★ 预算判据放在**每次启动新的一只之前**：已经在飞的让它跑完
        #   （半路取消会留下"取了一半"的状态，而它并不比跑完更便宜）。
        if time.monotonic() - started >= budget_sec:
            report.budget_hit = True
            break
        pending.append(asyncio.create_task(
            _warm_one(service, code, sem, report.errors, gate)))

    if pending:
        # ★★ 2026-09-30（`CHG-0137`）：**预算必须是硬约束，而不是提交前的礼貌检查**。
        #
        # 原实现只在"提交下一只之前"看预算，而 `asyncio.create_task` 是**非阻塞**的
        # ⇒ 提交循环**毫秒级跑完** ⇒ 那条判据**几乎永不触发**，而下面的 `gather`
        # **没有任何时间上限** ⇒ 最坏 `ceil(n/并发) × 单只超时` 全等完。
        #
        # 实测代价（2026-09-30 13:57，就是用户报"前端又不可达"的那次）：
        #   56 只 / 并发 3 / 22 只各撞 30 s 超时 ⇒ **一轮 316.4 s**，
        #   是 90 s 预算的 **3.5 倍**，而 cron 是 120 s ⇒ 长期"上一轮未结束"，
        #   **API 事件循环被它占着**，前端 4 s 探针必然超时 ⇒ 横幅"后端不可达"。
        #
        # 现在：给 `gather` 套上**剩余预算**的硬上限；到点就**取消在飞任务**，
        # 未跑完的按 `skipped` 如实计数（不静默）。这样"一轮 ≤ 预算"成为**代码事实**，
        # 而不是文档里的假设。
        left = max(0.0, budget_sec - (time.monotonic() - started))
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), timeout=left)
        except (asyncio.TimeoutError, TimeoutError):
            report.budget_hit = True
            for task in pending:
                task.cancel()
            results = await asyncio.gather(*pending, return_exceptions=True)
        report.warmed = sum(1 for item in results if item is True)
        # 异常在 `_warm_one` 里已吞掉并计数（返回 False）；`CancelledError` 是**预算截断**，
        # 归到 `skipped`（"没轮到"），不冒充失败 —— 两者处置不同（失败要查源，截断是设计）。
        report.failed = sum(1 for item in results if item is False)
        # ★ `CHG-0145`：把成败回写进"降级表"——失败的下一轮排队尾，
        #   成功的立刻恢复常规优先级。取消（预算截断）**两边都不记**：
        #   它不是失败，不该被降级。
        for _code, _item in zip(targets, results):
            if _item is True:
                note_success(_code)
            elif _item is False:
                note_failure(_code)
        _budget_cut = sum(1 for item in results
                          if isinstance(item, asyncio.CancelledError))

    if report.budget_hit:
        report.skipped = max(0, len(targets) - report.warmed - report.failed)
    report.seconds = time.monotonic() - started
    report.yielded_sec = yielded["sec"]
    logger.info("日K预热：%s", report.render())
    if report.budget_hit:
        #: "预算截断"必须**留痕**：否则"这一轮少预热了 N 只"只能靠人读日志发现
        #: （用户口径：不许静默降级）。记一条采集异常，管理员面板可见。
        _record_warm_budget_anomaly(report, budget_sec)
    return report
