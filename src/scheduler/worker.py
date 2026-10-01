"""调度 **worker 进程**：只跑 `HEAVY_JOBS`，与在线 API 进程分开（`CHG-0139`）。

## 为什么需要它（2026-09-30 实测事故）

三小时内用户三次报"前端显示后端不可达"。仪器（`access_audit.latency_ms`）显示
13:54–13:59 请求延迟最高 **64.4 秒**、13:58 那分钟 **23 条里 22 条 >1 秒**，
全部最终 200（**排队，不是报错**）⇒ **事件循环被重作业占住** ⇒ 前端 4 秒探针必然超时。

按 `data/pilot/scheduler/runs.jsonl` 的 1847 条真实记录统计，占用最大的四个
**纯后台**作业合计 ≈ **50,000 秒**（是 `daily_warm` 的 6 倍）：

| 作业 | 累计秒 | 中位 | 最大 |
|---|---|---|---|
| `intel_tone_extract` | 24849 | **304s** | **2037s** |
| `event_alert_intraday` | 13201 | 86s | 620s |
| `quant_data_sync` | 5943 | 94s | 211s |
| `mainline_daily` | 5894 | **2947s** | 3086s |

它们对实时性**零要求**（产物落库/落盘），却是"服务此刻无法及时响应"的主因。
所以把它们挪到**本进程**：API 进程（`MOSS_SCHEDULER_ROLE=api`）不跑它们，
本进程（`MOSS_SCHEDULER_ROLE=worker`）**只跑**它们。

## 为什么是裸进程、不是 Celery

`src/scheduler/celery_app.py` 的通道早就存在，但要 **redis broker**（新依赖 + 新运维面）。
本进程**零新依赖**：同一份 `JOB_REGISTRY`、同一套 job 实现、同一个库，
只是换了个进程跑 —— 先把"争事件循环"这件事解决掉，需要横向扩展时再上 Celery。

## 与 API 进程共享什么（**必须一致，否则会双跑或漏跑**）

* **同一份库**：由 `manage.py start-worker --env pilot` 传同一套隔离环境变量
  （`MOSS_ENV` / `MOSS_SQLITE_PATH` / `MOSS_AUDIT_DIR` …）；
* **同一份运行记录**：`RunLog(f"{settings.scheduler_dir}/runs.jsonl")` ——
  两个进程写同一个文件（追加写，`RunLog` 负责锁），所以"谁跑了多久"仍然只有一份账；
* **同一套关停收尾**：`sqlite_recovery.checkpoint_and_release_all()`（实现只有一处）；
* **同一份 `loop_lag` 实现**（只是换了个标签）：本进程跑的是**最重的四个作业**，
  而"worker 里的卡顿"原先**不可见** —— 所以这里按 API 进程的同一份代码启动
  `src/core/loop_lag.py`（1 Hz 采样），并用 `tag=worker` 把落盘 indicator
  与日志区分开（落盘文件按**环境**共享，不区分进程就会把 worker 的卡顿
  记成在线 API 的卡顿）。
  ⚠️ **两个进程里这行读数的含义不同**：API 侧的卡顿是"用户看到的不可达"，
  而 worker 侧的卡顿是**拆分的设计后果**（重作业本来就在这个环上跑）——
  它的价值是①把"作业实际占了多久"变成直接观测 ②区分"作业卡住"与"worker 卡住"。
* **不共享**：HTTP 服务、进程内缓存。

## ★ 崩溃取证：**"上一次是怎么结束的"必须留下痕迹**（本模块的第二件事）

现场（2026-09-30，已核实）：`py_mini_racer`(V8) 并发导致**原生崩溃** ——
退出码 `0x80000003`（`STATUS_BREAKPOINT`）、**没有 Python traceback**。
pilot 的 worker 曾这样"无痕死亡"，值守把它拉起来之后公网 502 数分钟；
而事后**没有任何一份记录**能回答"它是什么时候、以什么方式结束的"。

### 机制：**先写下"我在跑"，正常收尾时再划掉**（标记法）

    启动（拿到单实例锁之后**立刻**）  写 `worker_last_run.json`：pid / 启动时刻 /
                                      env / role / jobs / 代码版本线索 —— **未标记**
    正常收尾（关停收尾跑完之后）      标记 `clean_exit=true` + 退出码
    下一次启动                        读上一条：**没被标记 ⇒ 上次是异常终止**

关键在那个**不对称**：正常结束**一定**会写下标记（写标记这句就在关停收尾里），
而异常终止**不一定**写得成 —— 原生崩溃、`taskkill /F`、断电都会让 Python 侧的
`finally`/`atexit` **根本不执行**。所以「有标记」是**证据**（这个进程走完了自己的
收尾），「没有标记」是**结论**（它没走完）—— 这正是本机制**在没有调试器的 Windows 上
也能拿到证据**的原因：它不要求崩溃方配合，只要求活着的那一方留下痕迹。

### 边界（诚实登记，别把结论用过头）

* 它回答「**有没有走完 Python 侧收尾**」，不回答"为什么死"：原生崩溃与
  `taskkill /F` 在这里**形状相同**（都没有标记）。所以记录里带上"上一条记录的
  进程**最后心跳时刻**"（心跳是独立线程写的，是"无痕死亡"唯一的时间锚点），
  再配合日志尾部去分；
* **退出码可能拿不到**：记录里的 `exit_code` 只有 Python 侧能写的时候才有 ——
  缺失本身就是"没走完收尾"的证据，不是记录没写好；
* 读不懂记录（半写/被手工改坏）**不等于**异常终止：与心跳同一条纪律
  （`read_status` 的"无法解析 ≠ 死了"），处置动作完全不同，所以判成"无法判定"；
* 它**不参与存活判断**（不是第四路证据）："上次异常终止"不能证明"现在没人跑"，
  "上次正常"也不能证明"现在有人跑"。存活只看进程/锁/心跳三路
  （`manage.worker_present()`）。

## ★ 拆分的代价：两类新的静默失效，本模块各自自带答案

拆分不是"把代码挪个地方就完了"——它把原先**绑在一起**的两件事拆开了：

* 原先「进程活着」就等于「重作业在跑」。拆开后本进程**没有端口**，
  它停摆时前端照旧全绿 ⇒ 答案：心跳文件（**线程**写）+ `/health` 的 `worker` 段；
* 原先「一次只有一个实例」由"一个进程"自动成立。拆开后起两次就是
  重作业**双跑**（含往 14 GiB 仓库双写）⇒ 答案：排他锁（`worker_lock`，
  操作系统级，进程怎么死都会释放，**没有"陈旧锁"这个状态**）。

## 纪律

* **角色不对就大声报错**：`MOSS_SCHEDULER_ROLE` 不是 `worker` 时它会跑全表
  ⇒ 与 API 进程**双跑**重作业；这里打 ERROR 而不是静默继续。
* **拿不到锁就退出（码 3）并且不静默**：报出**持有者 PID**，让人能直接去看。
* 未设角色时 `schedulable_jobs()` 返回全表（既有行为不变）—— 本进程**总是**显式设置。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from src.core.logging_setup import configure_src_logging

logger = logging.getLogger("src.scheduler.worker")

#: 拿不到单实例锁时的退出码（值守/看门狗据此区分"起不来"与"已经有一个在跑"）。
EXIT_ALREADY_RUNNING = 3

#: 停止握手文件名（落在 `settings.scheduler_dir` 下，与环境隔离同源）。
STOP_FILE = "worker.stop"

#: 轮询停止文件的间隔（秒）。取 5：停止通常发生在维护窗口，
#: 5 秒的延迟对 `manage.py stop` 完全可接受，而更密的轮询只是白烧 CPU。
STOP_POLL_SEC = 5.0

#: 取证记录文件名（与锁、心跳、停止文件**同一个目录** ⇒ 环境隔离自动生效）。
RUN_RECORD_FILE = "worker_last_run.json"

#: 本进程**最早的 Python 可见时刻**（本模块被 import 的那一刻）。
#:
#: 为什么不写"进程创建时刻"：Windows 上取它要么引 psutil、要么写
#: `GetProcessTimes` 的 ctypes —— 而取证要回答的是"这次运行从何时开始"，
#: import 时刻与真实创建时刻之间只隔解释器启动那一段。**诚实标注优于假装精确**：
#: 它与 `started_at` 的差值恰好就是"本进程启动到拿到单实例锁"耗了多久。
PROCESS_BOOT_AT = time.time()


def stop_file_path() -> Path:
    """停止文件路径 —— 与锁/心跳同一套优先级（先读 `SCHEDULER_DIR`）。

    同处一个目录是**必需**的：`manage.py stop` 与 worker 必须对"哪个文件"
    有同一个答案，否则停止信号发到了别的环境的目录里（两者都静默）。
    """
    raw = os.environ.get("SCHEDULER_DIR")
    if raw:
        return Path(raw) / STOP_FILE
    from src.core.config import get_settings

    return Path(get_settings().scheduler_dir) / STOP_FILE


# ─────────────────── 崩溃取证：上一次是怎么结束的（`RUN_RECORD_FILE`）───────────────────
#
# 完整机制与边界见模块 docstring 的「崩溃取证」一节。这里只强调两条纪律：
# ① **写要原子**（`.tmp` + `os.replace`）：读到半个 JSON 的读取方会把"解析失败"
#    当成"异常终止" ⇒ 自己制造一次假警报（与 `worker_heartbeat.write_once` 同因）；
# ② **取证绝不抛异常**：它是观测，坏掉不该影响启动/关停（本项目对观测件的统一纪律）。


def run_record_path() -> Path:
    """取证记录路径 —— **照 `worker_heartbeat.heartbeat_path()` 的做法**。

    ★ 为什么是"先读 `SCHEDULER_DIR`、再回落 `get_settings()`"：记录必须与
    锁/心跳/停止文件落在**同一个目录**（同一个环境的账只记在一处），
    否则会出现"记录说上次死了、锁说现在有人跑"这种把两个环境读成一件事的错位。
    `get_settings()` 是 `@lru_cache` 单例，而 `manage.py` 的父进程可能已经缓存了
    另一个环境的 `Settings` ⇒ 回落路径会指向别的环境 —— 完整论证写在那个函数里，
    本文件**不再推导第二套路径逻辑**（本项目为"数 `parents[N]` 数错一层就写歪"
    付过代价）。
    """
    raw = os.environ.get("SCHEDULER_DIR")
    if raw:
        return Path(raw) / RUN_RECORD_FILE
    from src.core.config import get_settings

    return Path(get_settings().scheduler_dir) / RUN_RECORD_FILE


def _load_record(path: Path) -> tuple[dict[str, Any] | None, str]:
    """读记录 → `(记录 或 None, 读不出来的原因)`。**绝不抛异常**。

    ⚠️ "读不懂"（半写/被手工改坏）与"没有文件"必须分开报：前者是"无法判定"，
    后者是"从来没起过"。合并成 `None` 就会把一次**无法判定**印成一条**结论**
    （与 `worker_heartbeat.read_status` 的"无法解析 ≠ 死了"同一条纪律）。
    原因串里 `missing` 是**哨兵**（调用方按它分支），其余直接是人话。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except OSError as exc:
        return None, f"读不到（{type(exc).__name__}）"
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None, "读不懂（半写、或被手工改过）"
    if not isinstance(data, dict):
        return None, "读不懂（内容不是一个对象）"
    return data, ""


def _write_record_atomic(path: Path, data: dict[str, Any]) -> None:
    """原子替换写记录（先写 `.tmp` 再 `os.replace`，同分区上 Windows 也原子）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _fmt_ts(ts: Any) -> str | None:
    """把 epoch 秒格式化成**本地时间**字符串（读不懂就 None，绝不让它变 1970）。"""
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))


def _last_beat_of(pid: Any) -> str | None:
    """上一条记录的进程**最后心跳时刻** —— 仅当心跳文件确实属于同一个 pid。

    ★ 为什么值得跨这一份证据：记录只回答"有没有走完收尾"，回答不了"它死在什么时候"。
    心跳由**独立线程**每 20 秒写一次，所以"最后一次跳动"就是"它至少活到了那时"
    —— 原生崩溃**不写任何东西**，这是那种死亡唯一的时间锚点。

    ⚠️ 必须核对 pid：心跳文件会被**下一次**启动覆盖（`begin_run()` 刻意在
    `heartbeat.start()` 之前调用，就是为了让这里还读得到上一条的），
    不核对就会把新进程的心跳算到旧记录的账上。
    """
    if pid is None:
        return None
    try:
        from src.scheduler.worker_heartbeat import read_status

        st = read_status()
    except Exception:  # noqa: BLE001 取证读不到心跳不该影响启动
        return None
    if not st.get("present") or st.get("pid") != pid:
        return None
    age = st.get("age_sec")
    if not isinstance(age, (int, float)):
        return None
    return _fmt_ts(time.time() - float(age))


def _verdict(state: str, note: str, record: dict[str, Any] | None, *,
             path: Path, in_progress: bool = False) -> dict[str, Any]:
    """把判读打包成**调用方只需读两个键**的结构（`state` / `note`）。"""
    rec = record or {}
    out: dict[str, Any] = {
        "state": state,
        "clean": state == "clean",
        "failed": state == "failed",
        "vanished": state == "vanished",
        "needs_attention": state in ("failed", "vanished"),
        "in_progress": in_progress,
        "pid": rec.get("pid"),
        "started_at_iso": rec.get("started_at_iso") or _fmt_ts(rec.get("started_at")),
        "exit_code": rec.get("exit_code"),
        "last_beat_at_iso": None,
        "path": str(path),
        "note": note,
    }
    if state == "vanished":
        # 只有"无收尾痕迹"这一态需要时间锚点（另两态的时间由记录自己给出）
        out["last_beat_at_iso"] = _last_beat_of(rec.get("pid"))
    return out


def _note_clean(pid: Any, started: str, code: Any) -> str:
    return f"最近一次 worker 运行正常结束（PID={pid}，启动于 {started}，退出码 {code}）"


def _note_failed(pid: Any, started: str, code: Any) -> str:
    return (f"⚠️ 最近一次 worker 运行**异常退出**（PID={pid}，启动于 {started}，"
            f"退出码 {code}）—— Python 侧收尾执行了，日志里有 traceback："
            f"查 data/run/scheduler-worker.log")


def _note_vanished(pid: Any, started: str, beat: str | None) -> str:
    return (f"⚠️ 最近一次 worker 运行**异常终止**：PID={pid}，启动于 {started}，"
            f"最后心跳 {beat or '（无心跳记录）'}，**没有留下退出码**"
            f"（Python 侧收尾没执行）⇒ 疑似**原生崩溃**（如 V8/`py_mini_racer` 的 "
            f"0x80000003，无 traceback）或 `taskkill /F` 硬杀；"
            f"查 data/run/scheduler-worker.log 尾")


def last_run_report(*, path: Path | None = None,
                    run_ended: bool | None = None) -> dict[str, Any]:
    """**最近一次「已经结束的运行」是怎么结束的**（只读）—— 启动日志与存活报告都读它。

    六态（`state` 字段）—— 与心跳的三态同一条纪律：**不许合并成 bool**，
    因为每种的处置动作都不同：

    | state | 含义 | 该做什么 |
    |---|---|---|
    | `never` | 没有记录（首次启动/记录被清掉） | 什么都不用做 |
    | `unreadable` | 记录存在但读不懂 | **无法判定**，别当成崩溃 |
    | `clean` | 正常结束（走完收尾、退出码 0） | 什么都不用做 |
    | `failed` | 异常退出，但收尾执行了（退出码非 0） | 日志里**有** traceback，去看它 |
    | `vanished` | ★ **异常终止、无任何 Python 收尾痕迹** | 疑似原生崩溃/硬杀：看日志尾 + 最后心跳 |
    | `running` | 本次还在跑，且**没有**上一轮判读可报 | 什么都不用做（旧格式/手写记录） |

    ## `run_ended`：**唯一**能把"还在跑"与"已死且无痕"分开的东西

    记录没被标记时有两种可能，**只看文件分不出来**：
      (a) 这次运行**还在跑**（记录当然是未标记的）；
      (b) 它已经死了、而且没留下任何收尾痕迹（这正是要报警的那一种）。
    所以判据必须从**外面**来，而免费的、无陈旧问题的证据只有那把**单实例锁**：
    本模块的写入不变量是「**持锁 → 写未标记记录 → … → 标记 → 放锁**」
    （见 `begin_run()` / `_run()` 的收尾顺序），于是：
      · **锁空着**（或调用方刚拿到它）⇒ 写记录的那个进程已经不在 ⇒ (b)「异常终止」；
      · **锁被持着** ⇒ 大概率就是它本人在跑 ⇒ (a)「进行中」，**不报警**。
    `run_ended`：`True`=调用方确知那次运行已结束；`False`=确知它还在跑；`None`=不知道
    （不知道时**不猜**：只报上一条已经结束的运行的结论，宁可不报也不误报）。

    返回值：`{"state", "clean", "failed", "vanished", "needs_attention",
    "in_progress", "pid", "started_at_iso", "exit_code", "last_beat_at_iso",
    "path", "note"}` —— `note` 是可以直接印给人看的一句话。

    ⚠️ 它**不参与存活判断**（见模块 docstring 的边界一节）：这里说的是"最近一次跑完
    的是怎么结束的"，与"现在有没有人在跑"是两件事（后者只看进程/锁/心跳三路）。
    """
    target = path or run_record_path()
    record, why = _load_record(target)
    if record is None:
        if why == "missing":
            return _verdict("never", "没有上一次运行的记录（首次启动，或记录文件被清掉）",
                            None, path=target)
        return _verdict("unreadable",
                        f"上一次运行的记录{why} ⇒ **无法判定**上次是怎么结束的"
                        f"（这不是崩溃，也别当成正常）", None, path=target)

    pid = record.get("pid")
    started = record.get("started_at_iso") or _fmt_ts(record.get("started_at")) or "未知时刻"
    in_progress = record.get("clean_exit") is not True
    own: dict[str, Any] | None = None
    if not in_progress:
        own = _verdict("clean", _note_clean(pid, started, record.get("exit_code")),
                       record, path=target)
    elif record.get("finished_at") is not None:
        own = _verdict("failed", _note_failed(pid, started, record.get("exit_code")),
                       record, path=target)
    elif run_ended:
        # 未标记 + 调用方确知它已经结束 ⇒ **这就是"无痕死亡"**
        own = _verdict("vanished", _note_vanished(pid, started, _last_beat_of(pid)),
                       record, path=target)
    if own is not None:
        return own

    # 这条记录对应的运行还没结束（或无法判定）⇒ 报**上一条已经结束的**（启动时冻结的）
    prev = record.get("previous")
    if isinstance(prev, dict) and prev.get("state"):
        out = dict(prev)
        out["path"] = str(target)
        out["in_progress"] = True
        return out
    return _verdict("running", "本次运行进行中（记录里没有上一轮判读）",
                    None, path=target, in_progress=True)


def begin_run(*, role: str, env: str, jobs: Sequence[str],
              path: Path | None = None) -> dict[str, Any]:
    """① 判读**上一条**记录（上次怎么死的）② 立刻写下"本次启动"（**未标记**）。

    返回 `last_run_report()` 形状的判读 —— 调用方**必须**把它打进日志：
    这是"无痕死亡"唯一的痕迹通道（原生崩溃不写日志、不写 traceback）。
    这个判读同时被**冻结**进新记录的 `previous` 字段：启动这一刻是"上一条记录
    没有标记意味着什么"**唯一可知**的时刻（本进程刚拿到锁 ⇒ 上一任持锁者必然
    已经不在），而之后任何时刻再读那条旧记录都无法再确定这一点。

    ★ 为什么在**拿到单实例锁之后立刻**写（而不是等 `build_runtime` 之后）：
    启动段本身就是一类死法（依赖/配置/磁盘），记录晚写一步，那一段的死就无痕。
    ★ 为什么必须在 `heartbeat.start()` **之前**调用：上一条的"最后心跳时刻"
      是它唯一的时间锚点，心跳文件一旦被本次覆盖就再也读不回来了。
    """
    target = path or run_record_path()
    # `run_ended=True`：调用方（`_run()`）**已经拿到单实例锁** ⇒ 上一任持锁者
    # 必然不在 ⇒ 上一条未标记的记录就是"无痕死亡"（这是唯一能确定这一点的时刻）。
    verdict = last_run_report(path=target, run_ended=True)
    record: dict[str, Any] = {
        "schema": 1,
        "pid": os.getpid(),
        "role": role,
        "env": env,
        "jobs": list(jobs),
        "started_at": time.time(),
        "started_at_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        "process_boot_at": PROCESS_BOOT_AT,
        "process_boot_at_iso": _fmt_ts(PROCESS_BOOT_AT),
        "python": sys.version.split()[0],
        # 代码版本线索：崩溃在部署中途时，"跑的是哪一份 worker.py"是第一个要问的
        # 问题，而本仓库没有运行期版本号（`pyproject` 的 0.1.0 从不随交付变）。
        "worker_module_mtime_iso": _fmt_ts(Path(__file__).stat().st_mtime),
        "clean_exit": False,
        "exit_code": None,
        "finished_at": None,
        # ★ 把"上一条记录意味着什么"**冻结**进来：这一刻是它唯一可知的时刻
        #   （本进程刚拿到锁），之后再读那条旧记录都无法再确定"它结束没结束"。
        "previous": verdict,
    }
    try:
        _write_record_atomic(target, record)
    except OSError:
        logger.warning("崩溃取证记录写不进去（不影响启动）：%s", target, exc_info=True)
    return verdict


def finish_run(*, exit_code: int, path: Path | None = None) -> bool:
    """把**本次**运行标记为"走完了 Python 侧收尾"。返回是否真的标记成功。

    ★ **只有 `exit_code == 0` 才算正常结束**（`clean_exit=True`）；非 0 记成
    "异常退出、但收尾执行了"（`finished_at` 有值、`clean_exit=False`）——
    两者在日志里的处置不同（后者**有** traceback 可看），所以不许合并。

    ★ 调用时机：关停收尾**之后**、`lock.release()` **之前**。
      放在放锁之前是**必需**的：否则新进程可能在"已放锁、未标记"的窗口里启动，
      读到未标记的记录 ⇒ 报出一次**假的「上次异常终止」**
      （本项目的取值纪律：**宁可不报，也不要误报**）。

    ★ 只认**自己 pid** 的记录（幂等）：晚到的第二次调用只更新退出码，
    不会把另一个进程刚写的记录改掉。**绝不抛异常**。
    """
    target = path or run_record_path()
    record, _why = _load_record(target)
    if record is None or record.get("pid") != os.getpid():
        # 记录丢了/不是自己的：**不要**补写一条假的正常结束 ——
        # "没有记录"与"记下了正常结束"是两件不同的事（前者下一次会报无法判定）。
        return False
    record["clean_exit"] = int(exit_code) == 0
    record["exit_code"] = int(exit_code)
    record["finished_at"] = time.time()
    record["finished_at_iso"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        _write_record_atomic(target, record)
    except OSError:
        logger.warning("崩溃取证记录标记失败（下一次启动会报「异常终止」）", exc_info=True)
        return False
    return True


async def watch_stop_file(stop: asyncio.Event, *, path: Path | None = None,
                          started_at: float | None = None,
                          interval: float = STOP_POLL_SEC) -> None:
    """轮询停止文件；看到**比本进程新**的停止文件就置位 `stop`。

    ## 为什么需要它（2026-09-30 实测：CTRL_BREAK 到不了守护进程）

    `manage.py` 的优雅停止走 `os.kill(pid, CTRL_BREAK_EVENT)`，而它要求
    **调用方与目标共享同一个控制台**。本项目所有后端/worker 都是用
    `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` 起的（没有控制台），
    值守又是从计划任务里跑（也没有控制台）⇒ 实测 `request_graceful_stop`
    **返回 False**，worker 只能被 `taskkill /F` 硬杀，而硬杀**不走关停收尾**
    （不 checkpoint）—— 正是 `src/core/sqlite_recovery.py` 开头那次
    `disk I/O error` 的成因。

    所以给 worker 补一条**不依赖控制台**的通道：文件。

    ## ★ 不变量：**旧文件不许杀新进程**

    停止文件是"一次性"的：如果它留在磁盘上（上次停止后没删掉、或人手建的），
    下一次启动的 worker 会在第一轮轮询时立刻自杀 —— 而**看起来像"启动失败"**。
    所以判据是「文件的 mtime **晚于**本进程启动时刻」：陈旧的停止文件被**忽略**，
    并且会被顺手删掉（否则它每轮都要被重新判断一次）。
    """
    target = path or stop_file_path()
    birth = time.time() if started_at is None else started_at
    while not stop.is_set():
        try:
            if target.exists():
                if target.stat().st_mtime >= birth:
                    logger.info("收到停止文件 %s（比本进程新）⇒ 开始优雅关停", target)
                    stop.set()
                    return
                # 陈旧文件：删掉并继续（不自杀，也不留垃圾）
                logger.warning("忽略陈旧的停止文件 %s（早于本进程启动时刻）", target)
                target.unlink(missing_ok=True)
        except OSError:
            logger.debug("停止文件检查失败（忽略）", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            continue


async def _shutdown(runtime: object, scheduler: object) -> None:
    """关停收尾 —— **与 API 进程同一套**（各句独立兜异常，绝不互相拖累）。

    为什么不能省：本进程同样持有主库与行情仓的连接，硬杀会留下
    `-wal`/`-shm`，下次启动读到 `disk I/O error`（`src/core/sqlite_recovery.py`
    开头记的那次事故）。而 worker 被停的方式通常是 `manage.py stop` 的
    CTRL_BREAK（`request_graceful_stop`）⇒ 只要能进到这里就来得及 checkpoint。

    ★ 循环延迟监控也在这一段收尾（`loop_lag.stop()` = 取消采样 + **强制 flush
    最后一次**）：漏掉它的话，停摆前最后一个周期的卡顿读数就随进程一起消失
    —— 而那正是"这次运行到底被作业占住了多久"的答案。放在最前面，
    因为它只碰内存与日志，不依赖后面任何一步成功。
    """
    try:
        from src.core import loop_lag

        await loop_lag.stop()
    except Exception:  # noqa: BLE001 观测收尾失败不该影响 checkpoint
        logger.warning("循环延迟监控停止失败（继续关停收尾）", exc_info=True)
    try:
        await scheduler.stop()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        logger.warning("调度器停止失败（继续关停收尾）", exc_info=True)
    for label, closer in (
        ("事件告警仓储", getattr(runtime, "event_repo", None)),
        ("做T权重档案仓储", getattr(runtime, "intraday_profile_repo", None)),
        ("资金流选择列表仓储", getattr(runtime, "fundflow_repo", None)),
        ("主数据点仓储", getattr(runtime, "repo", None)),
    ):
        fn = getattr(closer, "close", None)
        if fn is None:
            continue
        try:
            await fn()
        except Exception:  # noqa: BLE001 单个仓储失败不影响其余
            logger.warning("%s 关闭失败（继续关停收尾）", label, exc_info=True)
    try:
        from src.core.sqlite_recovery import checkpoint_and_release_all

        released, cleaned = checkpoint_and_release_all()
        logger.info("worker 关停：释放常驻 SQLite 连接 %d 个，checkpoint 主库 %d 个",
                    released, cleaned)
    except Exception:  # noqa: BLE001
        logger.warning("worker 关停 checkpoint 失败（不影响退出）", exc_info=True)
    try:
        from src.core.executors import shutdown_infra_executors

        shutdown_infra_executors()
    except Exception:  # noqa: BLE001
        logger.debug("关闭基础设施线程池失败（忽略）", exc_info=True)


async def _run() -> int:
    from src.api.runtime import build_runtime
    from src.core.config import get_settings
    from src.scheduler.registry import HEAVY_JOBS, process_role, schedulable_jobs
    from src.scheduler.run_log import RunLog
    from src.scheduler.service import CronScheduler
    from src.scheduler.worker_heartbeat import WorkerHeartbeat
    from src.scheduler.worker_lock import WorkerLock

    settings = get_settings()

    # ── ① 单实例锁（拿不到就退出，别把重作业跑两遍）──
    lock = WorkerLock()
    if not lock.acquire(env=settings.env):
        holder = lock.holder or {}
        logger.error(
            "已有调度 worker 在跑（PID=%s，启动于 %s）⇒ 本进程退出，"
            "**不重复执行**重作业（同一作业双跑会往行情仓双写）。"
            "确认对方已死后重试：`python manage.py stop` 再 `start-worker`。",
            holder.get("pid", "未知"), holder.get("started_at_iso", "未知"))
        return EXIT_ALREADY_RUNNING

    role = process_role()
    jobs = schedulable_jobs()
    heartbeat = WorkerHeartbeat(role=role or "(未设)", jobs=jobs)
    runtime = None
    scheduler = None
    #: 关停收尾时要写进取证记录的退出码（异常退出时被下面的 `except` 改成 1）。
    exit_code = 0
    try:
        # ── ①′ 崩溃取证：先判读"上次是怎么结束的"，再写下"本次启动" ──
        # 位置是刻意的：拿到锁之后**立刻**（启动段本身也是一类死法），
        # 且在 `heartbeat.start()` 之前（上一条的"最后心跳"此时还读得到）。
        verdict = begin_run(role=role or "(未设)", env=settings.env, jobs=list(jobs))
        logger.info("取证记录：%s（本次启动已登记，正常收尾时才会标记结束）",
                    run_record_path())
        if verdict["needs_attention"]:
            # 值守看的就是这一行：它是"无痕死亡"唯一的痕迹（原生崩溃不写日志）
            logger.warning("★ 上一次 worker：%s", verdict["note"])
        else:
            logger.info("上一次 worker：%s", verdict["note"])

        runtime = build_runtime()
        run_log = RunLog(f"{settings.scheduler_dir}/runs.jsonl")
        scheduler = CronScheduler(runtime, run_log)

        # ── ② 心跳（**线程**写：作业会把事件循环占住上千秒，环内写=把忙判成死）──
        heartbeat.start()
        logger.info("调度 worker 启动：env=%s role=%s 本进程负责 %d 个作业：%s",
                    settings.env, role or "(未设)", len(jobs), "、".join(jobs))
        logger.info("心跳文件：%s（每 %.0f 秒一次，独立线程）",
                    heartbeat.path, heartbeat.interval)
        if role != "worker":
            # 不静默：角色不对时它会跑全表（与 API 进程**双跑**重作业）
            logger.error(
                "调度 worker 的 MOSS_SCHEDULER_ROLE=%r 不是 'worker' ⇒ 它会执行全表 %d 个作业，"
                "与 API 进程**重复执行**重作业（%s）。请用 `manage.py start-worker` 启动。",
                role, len(jobs), "、".join(HEAVY_JOBS))

        await scheduler.start()

        # ── ②′ 循环延迟监控：**与 API 进程同一份实现**（`src/core/loop_lag.py`）──
        # 为什么 worker 也需要它：本进程跑的是四个最重作业，"worker 里的卡顿"
        # 原先**没有任何仪表**（唯一的仪器装在 API 进程里）⇒ 作业把循环占住
        # 多久、是不是它卡住了，全是猜的。标签 `TAG_WORKER` 让落盘 indicator
        # 与日志能区分进程（落盘文件按环境共享，见模块 docstring）。
        try:
            from src.core import loop_lag

            loop_lag.start(tag=loop_lag.TAG_WORKER)
        except Exception:  # noqa: BLE001 观测组件起不来不该阻断 worker
            logger.warning("事件循环延迟监控启动失败（忽略）", exc_info=True)
        stop = asyncio.Event()

        def _ask_stop(*_: object) -> None:
            logger.info("收到停止信号，正在退出…")
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, _ask_stop)
            except (NotImplementedError, RuntimeError):  # Windows 部分场景不支持
                signal.signal(sig, _ask_stop)
        # Windows：`manage.py stop` 的优雅路径是 CTRL_BREAK（`request_graceful_stop`），
        # Python 把它送到 SIGBREAK；不接住它就会走"硬杀式"退出（不 checkpoint）。
        sigbreak = getattr(signal, "SIGBREAK", None)
        if sigbreak is not None:
            try:
                signal.signal(sigbreak, _ask_stop)
            except (ValueError, OSError):  # 非主线程等场景
                logger.debug("SIGBREAK 处理注册失败（忽略）", exc_info=True)
        # ── ③ 停止握手（不依赖控制台的那条通道，见 `watch_stop_file`）──
        watcher = asyncio.create_task(
            watch_stop_file(stop, started_at=heartbeat.started_at),
            name="scheduler-worker-stop-watch")
        try:
            await stop.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            # CTRL_BREAK 在 Windows 上表现为 KeyboardInterrupt ⇒ 这里**必须**
            # 自己接住再往下走，否则关停收尾整段不会执行（含 WAL checkpoint）。
            logger.info("收到中断信号，正在退出…")
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
    except BaseException:
        # 异常退出（含被取消）：退出码要进取证记录 —— 它把"走完收尾的正常结束"
        # 与"异常退出、但收尾执行了"分开（后者的日志里**有** traceback 可看）。
        exit_code = 1
        raise
    finally:
        # ★ `_shutdown` 自己抛异常（或被取消）时，"标记 + 放锁"仍必须执行：
        #   记录没被标记 ⇒ 下一次启动会报一次**假的**「上次异常终止」，
        #   而"已放锁、未标记"那段窗口正是新进程唯一能启动的时机（见 `finish_run`）。
        try:
            if scheduler is not None:
                await _shutdown(runtime, scheduler)
        finally:
            heartbeat.stop()
            # ★ 标记在**放锁之前**：顺序本身就是判据（见 `finish_run` 的 docstring）
            finish_run(exit_code=exit_code)
            lock.release()
            # 停止文件是"一次性"的：本进程消费掉它就删掉，避免它杀下一次启动的 worker
            try:
                stop_file_path().unlink(missing_ok=True)
            except OSError:
                logger.debug("清理停止文件失败（忽略）", exc_info=True)
    return 0


def main() -> int:
    configure_src_logging()
    logger.info("调度 worker 进程启动（PID=%d）", os.getpid())
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        # 控制台中断：**不改记录** —— `_run` 的收尾已按"有没有走完 Python 收尾"记过一笔；
        # 这里补一个"正常"会把"收尾被中断"洗成正常结束（宁可不报，也不要洗白）。
        return 0
    except Exception:  # noqa: BLE001 启动失败必须留下完整现场
        logger.exception("调度 worker 异常退出")
        # 更正退出码：异常若发生在 `_run` 收尾**之后**，那里记下的会是"正常"。
        # 这一句只会把记录改向 `failed`，永远不会把 `failed` 洗成正常。
        finish_run(exit_code=1)
        return 1


if __name__ == "__main__":
    sys.exit(main())
