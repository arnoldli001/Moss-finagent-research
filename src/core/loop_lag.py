"""事件循环延迟监控 + 存活探针打点（`CHG-0137`）。

## 为什么必须有这个模块（一次真实的误判链）

2026-09-30 用户连续三次报"前端后端服务当前不可达"。我先后猜过：
隧道抖动 → 我误读日志空档（"120 秒被卡"，其实是 access 行**没有时间戳**）
→ 最后靠 `access_audit.latency_ms` 才看清：**13:54–13:59 请求延迟最高 64.4 秒、
13:58 那分钟 23 条里 22 条 >1 秒**，全部最终 200（**排队，不是报错**）。

根因是**作业与在线 API 共用同一个事件循环**：日K预热一轮跑了 **316.4 秒**
（预算 90 秒、cron 120 秒）⇒ 循环被它占着 ⇒ 前端 4 秒探针必然超时 ⇒ 横幅。

**而这一整条链上，唯一没被测量的东西恰恰是决定性证据本身**：

* `/api/v1/health/live`、`/healthz` 是**公开路径**，被访问审计跳过
  （`_PUBLIC_PATHS`）⇒ **决定横幅的那个探针，在审计里一条记录都没有**；
* "事件循环卡了多久"只能靠请求延迟反推 —— 而请求延迟要等到请求**完成**才有值。

所以本模块补两件仪器：

| 仪器 | 回答什么 | 落点 |
|---|---|---|
| **循环延迟采样**（1 Hz） | "服务此刻还能响应吗" | 超阈值 → 异常表 + 每分钟一行 |
| **探针打点** | "横幅依据的那个探针，实际延迟多少" | 分钟聚合，与循环延迟同一行日志 |
| **在飞登记**（`src/core/inflight.py`） | "卡住的那一刻，**谁**在飞" | 卡顿 WARNING 行 + 异常理由 |

## 为什么不逐条写审计

探针是**热路径**（前端每 5–15 秒一次，且它是 0 I/O 的）。给它加 I/O 就等于
把它变成"会拖慢自己的探针"。所以打点只在**内存里累加**（几次 dict 操作），
由监控任务每分钟 flush 一次。

## 判死线

`MOSS_LOOP_LAG_WARN_MS`（默认 500 ms）：前端探针超时是 4000 ms，取 1/8 作预警线 ——
它一旦持续触发，用户**必然**在下一个探针周期看到红条，所以这条告警是**提前量**，
不是事后统计。`MOSS_LOOP_LAG_OFF=1` 可整体关掉（测试/极端环境）。

## ★ 进程标签（`tag`）：同一份读数的**两种含义**（2026-09-30，worker 侧补仪器）

`role=worker` 的调度 worker（`src/scheduler/worker.py`）跑的是**最重的四个作业**，
恰恰最需要这个仪器，所以它**复用本模块**（不另写一套 1 Hz 采样）。但直接复用会
制造两个错答案 —— 而错答案比没有读数更糟：

1. **落盘混在一起**：`collection_anomalies.jsonl` 按**环境**隔离、不按**进程**隔离，
   而 indicator 原来写死 `api_event_loop` ⇒ worker 的卡顿会**冒充** API 的卡顿，
   读面板的人会去查在线进程（真凶在另一个进程）。
2. **含义不同**：api 侧卡顿＝用户看得见的「不可达」（去查在飞请求）；
   worker 侧卡顿＝重作业占住循环，这是**拆分的设计后果**（读数回答的是
   「作业实际占了多久」，且 worker 没有 HTTP 探针 ⇒ 探针计数恒为 0）。

所以 `start()` 收一个标签；默认 `TAG_API` 让既有调用点（`src/api/main.py`）不改一字、
**既有落盘 indicator 字符串 `api_event_loop` 逐字不变**（管理员界面按它读）。
这**不是文案偏好**：`_reason()` 里按标签分岔的两段话，是两个不同的排查方向。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: 采样间隔（秒）。1 Hz 足够捕捉"秒级卡顿"，且自身开销可忽略。
SAMPLE_SEC = 1.0
#: 预警线（毫秒）：前端探针超时 4000 ms 的 1/8。
WARN_MS = float(os.environ.get("MOSS_LOOP_LAG_WARN_MS", "500"))
#: 每分钟 flush 一次统计（不逐条写盘）。
FLUSH_SEC = 60.0

#: 在线 API 进程的标签（**默认值**：既有调用点与既有的落盘 indicator 都不变）。
TAG_API = "api"
#: 调度 worker 进程的标签（`src/scheduler/worker.py` 显式传入）。
TAG_WORKER = "worker"

#: 当前进程的标签 —— 由 `start(tag=...)` 设定，`process_tag()` 读。
_TAG = TAG_API


@dataclass
class LoopLagStats:
    """一个 flush 周期内的观测。"""

    samples: int = 0
    max_ms: float = 0.0
    over_warn: int = 0
    probe_n: int = 0
    probe_max_ms: float = 0.0
    last_lag_ms: float = 0.0
    worst_at: str = ""
    #: 逐条超阈值的样本（有界，用于异常留痕时写清"什么时候、卡了多久"）
    spikes: list[tuple[str, float]] = field(default_factory=list)

    def reset(self) -> None:
        self.samples = 0
        self.max_ms = 0.0
        self.over_warn = 0
        self.probe_n = 0
        self.probe_max_ms = 0.0
        self.spikes.clear()


_STATS = LoopLagStats()
_TASK: asyncio.Task | None = None
#: 尖峰逐条留痕的上限（防止一次长时间卡顿把内存/文件写爆）
MAX_SPIKES = 8


def note_probe(elapsed_ms: float) -> None:
    """存活探针处理完时调用（**只做内存累加**，不碰磁盘）。

    ⚠️ **它测不到"不可达"** —— 这是测量层的硬事实，写在这里免得后人误用：
    事件循环被阻塞时，请求**卡在 socket 接收缓冲区里**，根本进不到任何 Python 代码，
    所以 handler 内测出的耗时**永远是 ~0ms**（那正是它"0 I/O"的代价）。
    真正的仪器是本模块的 **1 Hz 循环延迟采样**；探针打点只提供两件事：
      ① **计数对照**：浏览器每分钟发 N 次、我们答了几次（差额 = 请求没到/没答）；
      ② **正对照**：handler 耗时真变大时，说明是 handler 自己的问题而非循环排队。
    """
    _STATS.probe_n += 1
    if elapsed_ms > _STATS.probe_max_ms:
        _STATS.probe_max_ms = elapsed_ms


def stats() -> LoopLagStats:
    """当前周期的观测（只读；给测试与 `/health` 用）。"""
    return _STATS


def process_tag() -> str:
    """本进程的标签（`api` / `worker`）—— 落盘 indicator 与日志都按它区分。

    为什么要有它：见模块 docstring 的「进程标签」一节（同一份读数在两类进程里
    指向两个不同的排查方向，而落盘文件是**按环境共享**的）。
    """
    return _TAG


def _record(kind: str, indicator: str, reason: str) -> None:
    try:
        from src.core.collection_anomalies import record

        record(kind, indicator, reason)
    except Exception:  # noqa: BLE001 观测失败绝不该影响服务
        logger.debug("采集异常落盘失败", exc_info=True)


def _reason(s: LoopLagStats, detail: str) -> str:
    """异常落盘的理由 —— **按进程标签分岔**（同一份读数，两种含义）。

    ★ 为什么值得分岔而不是共用一段话：这段话是维护者在「数据采集异常」区
    **唯一会读到的**解释，而 api 与 worker 的处置动作完全不同 ——
    worker 的循环被重作业占住是**拆分的设计后果**，把它写成「服务不可达」
    会把人引到在线进程去查一个并不存在的故障（本项目反复记过这一类代价：
    **结论比证据强**的印法）。
    """
    head = (f"事件循环单次延迟 >{int(WARN_MS)}ms 共 {s.over_warn} 次"
            f"（峰值 {s.max_ms:.0f}ms）；")
    if process_tag() == TAG_WORKER:
        return (
            head + "本进程是**调度 worker**（只跑 HEAVY_JOBS，没有 HTTP 服务）⇒ "
            "循环被作业占住是**拆分的设计后果**，不是「在线服务不可达」；"
            "这行读数的用途是①把「作业实际占了多久」变成直接观测 "
            "②区分「作业卡住」与「worker 卡住」（后者看心跳文件）。"
            f"样例：{detail or '—'}。"
            "在飞登记簿为空属正常（它只登记 HTTP 请求，worker 没有请求）——"
            "要查同时段在跑哪个作业，看同目录 runs.jsonl 的 status/duration。")
    from src.core.inflight import describe

    #: 探测延迟把"循环卡顿"和"网络排队"一起算进去了 —— 这正是横幅看到的量。
    return (head + f"同周期存活探针 {s.probe_n} 次、最慢 {s.probe_max_ms:.0f}ms"
            "（前端探针超时线 4000ms）。"
            f"样例：{detail or '—'}。"
            f"**峰值时刻在飞**：{describe() or '（无在飞请求）'} —— "
            "在飞的即嫌疑人（按 `latency_ms` 排序会把**排队**的受害者误判成根因）。"
            "另查 `[循环延迟]` 同时段的作业耗时（作业与在线 API 共用事件循环，"
            "作业墙钟预算见 `intraday/warm.py`）。")


def flush(*, force: bool = False) -> None:
    """把当前周期落一行日志；有超阈值样本时再记异常。"""
    s = _STATS
    if s.samples == 0 and s.probe_n == 0 and not force:
        return
    logger.info(
        "[循环延迟] 进程=%s 采样 %d 次  max=%.0fms  超阈值(%dms) %d 次  |  "
        "存活探针 %d 次  max=%.0fms",
        process_tag(), s.samples, s.max_ms, int(WARN_MS), s.over_warn,
        s.probe_n, s.probe_max_ms)
    if s.over_warn:
        detail = "、".join(f"{at} {ms:.0f}ms" for at, ms in s.spikes[:MAX_SPIKES])
        # ★ indicator 带进程标签：落盘文件按**环境**共享，不带标签就分不清
        #   "卡住的是在线 API"还是"卡住的是 worker"（默认标签下与既有字符串一致）
        _record("loop_lag", f"{process_tag()}_event_loop", _reason(s, detail))
    s.reset()


async def _loop() -> None:
    """1 Hz 采样：`sleep(interval)` 的实际耗时 - 期望 = 事件循环延迟。"""
    last_flush = time.monotonic()
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(SAMPLE_SEC)
        lag_ms = max(0.0, (time.monotonic() - t0 - SAMPLE_SEC) * 1000.0)
        s = _STATS
        s.samples += 1
        s.last_lag_ms = lag_ms
        if lag_ms > s.max_ms:
            s.max_ms = lag_ms
        if lag_ms >= WARN_MS:
            s.over_warn += 1
            if len(s.spikes) < MAX_SPIKES:
                s.spikes.append((time.strftime("%H:%M:%S"), lag_ms))
            # ★ `CHG-0146`：卡顿的**那一刻**去读在飞登记簿 —— 这是唯一能把
            #   "谁堵住了循环"与"谁只是排队"分开的观测点。没有它，
            #   `latency_ms` 最大的那个往往是**受害者**（实测：研究任务轮询
            #   接口 p50 8.3 秒，而它只是内存查表）。
            if process_tag() == TAG_WORKER:
                # worker 不登记 HTTP 请求 ⇒ 点不出"在飞的人"；这里的读数是
                # "重作业占了循环多久"（见模块 docstring 的进程标签一节）。
                logger.warning(
                    "[循环延迟] 进程=%s 本次 %.0fms（阈值 %dms）—— "
                    "本进程循环被重作业占住（worker 侧属预期，不是在线服务故障）",
                    process_tag(), lag_ms, int(WARN_MS))
            else:
                from src.core.inflight import describe

                who = describe()
                logger.warning(
                    "[循环延迟] 进程=%s 本次 %.0fms（阈值 %dms）—— 服务此刻无法及时响应；"
                    "卡顿时刻在飞：%s",
                    process_tag(), lag_ms, int(WARN_MS), who or "（无在飞请求）")
        if time.monotonic() - last_flush >= FLUSH_SEC:
            last_flush = time.monotonic()
            flush()


def start(*, tag: str = TAG_API) -> bool:
    """在**应用的事件循环里**启动监控（幂等）。返回是否真的启动了。

    `tag` 是**进程标签**（`TAG_API` / `TAG_WORKER`）：它决定落盘的 indicator
    （`<tag>_event_loop`，默认值与既有字符串逐字一致）与日志里的进程名。
    传它是**必需**的 —— 落盘文件按环境共享，不区分进程就会把 worker 的卡顿
    记成在线 API 的卡顿（详见模块 docstring 的「进程标签」一节）。
    """
    global _TAG, _TASK
    _TAG = (tag or TAG_API).strip() or TAG_API
    if os.environ.get("MOSS_LOOP_LAG_OFF") == "1":
        logger.info("[循环延迟] 监控已按 MOSS_LOOP_LAG_OFF=1 关闭（进程=%s）",
                    process_tag())
        return False
    if _TASK is not None and not _TASK.done():
        return False
    try:
        _TASK = asyncio.get_running_loop().create_task(
            _loop(), name=f"loop-lag-monitor-{process_tag()}")
    except RuntimeError:      # 没有运行中的事件循环（离线脚本/单测直接调用）
        logger.debug("[循环延迟] 无运行中的事件循环，跳过启动")
        return False
    logger.info("[循环延迟] 监控已启动（进程=%s）：%d Hz 采样，阈值 %dms，每 %.0fs 汇总一行",
                process_tag(), int(1 / SAMPLE_SEC), int(WARN_MS), FLUSH_SEC)
    return True


async def stop() -> None:
    """停掉监控、flush 最后一次，并把**进程标签收回默认**（lifespan 关停时调用）。

    ★ 为什么要在收尾时把 `_TAG` 放回 `TAG_API`：标签是**进程级单例**，而它是
    "**此刻在跑的这个仪器属于谁**"的声明 —— 监控停了，这个声明就不该继续有效。
    实测过一次代价（2026-09-30，本模块刚加上标签时）：一个跑过 worker 启动序列的
    用例把 `_TAG` 留在 `worker`，于是**同一进程里后面的用例**再调 `flush()` 时，
    落盘理由变成了 worker 版（不含在飞请求点名）⇒ 一条盯"卡顿理由必须点名在飞
    处理器"的判据变红，而它红得毫无道理（那边根本没有 worker）。
    这与本项目反复记的"进程级状态跨用例泄漏"是同一形状：**默认值必须能自己回来**。
    """
    global _TAG, _TASK
    if _TASK is None:
        return
    _TASK.cancel()
    try:
        await _TASK
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _TASK = None
    flush(force=True)          # ← 这一次仍然用**本进程的**标签（先 flush 再收回）
    _TAG = TAG_API


__all__ = ["SAMPLE_SEC", "TAG_API", "TAG_WORKER", "WARN_MS", "LoopLagStats",
           "flush", "note_probe", "process_tag", "start", "stats", "stop"]
