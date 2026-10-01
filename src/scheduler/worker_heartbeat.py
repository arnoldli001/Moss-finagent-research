"""调度 worker 的**存活信号**（`CHG-0139` 的第二半）。

## 它补的是哪个洞

把 4 个重作业挪出 API 进程之后，出现了一类**新的静默失效**：

* 从前：重作业与 HTTP 在同一个进程 ⇒ 这个进程死了前端会报「不可达」，
  重作业也一起停 —— **两件事是同一个信号**；
* 现在：worker 没有端口、没有前端入口 ⇒ 它死了**没有任何人会发现**，
  而那 4 个作业（`intel_tone_extract` / `event_alert_intraday` /
  `quant_data_sync` / `mainline_daily`，合计约 5 万秒占用）会**永远不再执行**。

也就是说：拆分**用一个新的静默失效**，换掉了一个旧的事故面。
所以拆分的交付里必须自带"它还在吗"这件事的答案 —— 本模块就是那个答案。

## ★ 为什么信号由**线程**写，不由事件循环写（这是本模块唯一的设计要点）

最自然的写法是 `asyncio.create_task(每 20 秒写一次)`。**那是错的**，而且错法很隐蔽：

worker 的事件循环**就是**重作业跑的地方 —— `mainline_daily` 实测中位
**2947 秒**、最大 3086 秒（见 `registry.HEAVY_JOBS` 的表）。在环内写心跳，
作业一开跑心跳就停 ⇒ 任何"看心跳"的人（或值守）都会把**正在干活的 worker**
判成**死掉的 worker**，于是重启它 —— 而这时上一轮作业可能还在写库。
**把忙判成死，比不监控更糟**（`cmd_ensure` 的 docstring 为同一条教训写过一次：
它因此**故意**不做"健康检查失败就重启"）。

这与本项目刚记过的另一条是**同一个形状**：`loop_lag` 第一版用"handler 里的
耗时"当循环延迟的判据，量出来永远是 0 ms —— 因为被堵住的请求根本进不到
handler。**在错的层上测量，然后得到一个看起来很确定的错答案。**
所以心跳写在**独立守护线程**里：它与作业是否在跑、循环是否被堵**完全无关**，
只回答一个问题：「这个进程还活着吗」。

## 三态（调用方必须区分，不许合并成 bool）

| 文件 | 含义 | 该做什么 |
|---|---|---|
| 不存在 | 从来没起过 worker | 去起（`manage.py start-worker`） |
| 新鲜（≤ `STALE_AFTER_SEC`） | 活着 | 什么都不做 |
| 陈旧 | 起过、现在没了（或写心跳的线程死了） | 看 `data/run/scheduler-worker.log` 尾 |

⚠️ 「陈旧」**不等于**"作业没在跑"：文件只说明进程心跳，作业是否成功要看
`data/<env>/scheduler/runs.jsonl` 的 `status`（同一份账，两个进程共写）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

#: 心跳文件名（落在 `settings.scheduler_dir` 下 ⇒ **环境隔离是自动的**：
#: pilot 与 dev 各写各的，与 `runs.jsonl` 同目录、同来源，不需要第二处约定）。
HEARTBEAT_FILE = "worker_heartbeat.json"

#: 写心跳的间隔（秒）。取 20：足够频繁（值守 1 分钟一轮就能看到状态），
#: 又不至于让磁盘上多出一堆无意义的写（每次只有约 200 字节，原子替换）。
INTERVAL_SEC = 20.0

#: 判"陈旧"的阈值（秒）。取 3 个间隔 + 余量：
#: 单次写失败/一次调度抖动不该被读成"进程死了"（与隧道值守"连续 4 次失败
#: 才重启"是同一条取值纪律：**宁可不报，也不要误报**）。
STALE_AFTER_SEC = 90.0


def heartbeat_path() -> Path:
    """心跳文件路径 —— **由 `settings.scheduler_dir` 派生**，不在调用点硬编路径。

    ## ★ 为什么先读 `SCHEDULER_DIR` 环境变量，再回落到 `get_settings()`

    因为 `get_settings()` 是 `@lru_cache` 的**进程内单例**：`manage.py` 的横幅
    是在**父进程**里算的（`_scheduler_scope_line` 用 `_temporary_environ` 临时
    注入 `--env pilot` 的变量），而父进程可能早就构建过一份 dev 的 `Settings`
    ⇒ 回落路径会指向 `data/dev/scheduler`，于是 pilot 的横幅会去看**另一个环境**
    的心跳，并如实报告一个错答案。

    这与 `manage.py::_scheduler_scope_line` docstring 里记的 `CHG-0129` 是
    **同一个坑的第二次出现**（那次是 `MOSS_ENV` 读成了父进程的值）：
    **判据必须在"即将生效"的那份环境里求值**。而 pydantic 的
    `scheduler_dir` 字段本来就来自 `SCHEDULER_DIR` 这个环境变量
    （`config.py` 的 `_env_field_factory`），所以这里读它**不是第二处事实源**，
    只是同一个来源的正确优先级。
    """
    raw = os.environ.get("SCHEDULER_DIR")
    if raw:
        return Path(raw) / HEARTBEAT_FILE
    from src.core.config import get_settings

    return Path(get_settings().scheduler_dir) / HEARTBEAT_FILE


def _payload(*, role: str, jobs: list[str], pid: int, started_at: float,
             beats: int) -> dict[str, Any]:
    return {
        "pid": pid,
        "role": role,
        "jobs": list(jobs),
        "started_at": started_at,
        "started_at_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started_at)),
        "beat_at": time.time(),
        "beats": beats,
    }


def write_once(*, role: str, jobs: list[str], pid: int | None = None,
               started_at: float | None = None, beats: int = 1,
               path: Path | None = None) -> Path:
    """写一次心跳（原子替换：先写 `.tmp` 再 `os.replace`）。

    ⚠️ 必须原子：读到**半个 JSON** 的读取方要么崩、要么把"解析失败"当成
    "进程死了" —— 两种都是自己制造的故障。`os.replace` 在 Windows 上同样是
    原子的（同分区）。
    """
    target = path or heartbeat_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    data = _payload(role=role, jobs=jobs,
                    pid=os.getpid() if pid is None else pid,
                    started_at=time.time() if started_at is None else started_at,
                    beats=beats)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)
    return target


class WorkerHeartbeat:
    """守护线程版心跳：`start()` 后每 `INTERVAL_SEC` 秒写一次，`stop()` 收尾。

    ★ 为什么是 `threading.Thread` 而不是 `asyncio.Task`：见模块 docstring
    （重作业会把事件循环占住上千秒，环内写心跳 ⇒ **把忙判成死**）。
    """

    def __init__(self, *, role: str, jobs: list[str],
                 interval: float = INTERVAL_SEC, path: Path | None = None) -> None:
        self.role = role
        self.jobs = list(jobs)
        self.interval = float(interval)
        #: 解析成**具体路径**（不是 None）：启动日志要把真实落点印出来，
        #: 否则"心跳在哪"又要人去翻代码（本项目为"报错里没有现场"付过代价）。
        self.path = path or heartbeat_path()
        self.started_at = time.time()
        self._beats = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── 内部：一次写入（首次立即写，之后按间隔）──
    def _loop(self) -> None:
        while not self._stop.is_set():
            self._beats += 1
            try:
                write_once(role=self.role, jobs=self.jobs, started_at=self.started_at,
                           beats=self._beats, path=self.path)
            except Exception:  # noqa: BLE001 心跳写失败绝不能拖垮 worker
                # 不 raise、不重试风暴：下一轮还会再试（间隔本身就是重试）。
                pass
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="scheduler-worker-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None


def read_status(*, now: float | None = None, path: Path | None = None,
                stale_after: float = STALE_AFTER_SEC) -> dict[str, Any]:
    """读心跳并判三态。**任何异常都不抛** —— 它是给健康检查用的。

    返回值（调用方只需看 `alive` / `present` / `note`）：

        {"present": bool, "alive": bool, "age_sec": float|None,
         "pid": int|None, "role": str|None, "jobs": [...], "beats": int|None,
         "started_at_iso": str|None, "path": str, "note": str}
    """
    target = path or heartbeat_path()
    out: dict[str, Any] = {
        "present": False, "alive": False, "age_sec": None,
        "pid": None, "role": None, "jobs": [], "beats": None,
        "started_at_iso": None, "path": str(target), "note": "",
    }
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        out["note"] = ("从未起过调度 worker ⇒ 重作业（HEAVY_JOBS）当前**无人执行**；"
                       "用 `python manage.py start-worker --env <env> --daemon` 启动")
        return out
    except OSError as exc:
        out["note"] = f"心跳文件读不到（{type(exc).__name__}）"
        return out

    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("心跳内容不是对象")
    except (ValueError, TypeError):
        # 半写/损坏：**明确说"读不懂"**，不要说成"死了"（两者处置完全不同）
        out["present"] = True
        out["note"] = "心跳文件内容无法解析（可能正在写入或被人手工改过）"
        return out

    beat_at = data.get("beat_at")
    out["present"] = True
    out["pid"] = data.get("pid")
    out["role"] = data.get("role")
    jobs = data.get("jobs")
    out["jobs"] = list(jobs) if isinstance(jobs, list) else []
    out["beats"] = data.get("beats")
    out["started_at_iso"] = data.get("started_at_iso")
    if not isinstance(beat_at, (int, float)):
        out["note"] = "心跳文件缺少 beat_at（无法判断新鲜度）"
        return out

    age = max(0.0, (time.time() if now is None else now) - float(beat_at))
    out["age_sec"] = round(age, 1)
    if age <= stale_after:
        out["alive"] = True
        out["note"] = (f"调度 worker 存活（PID={out['pid']}，"
                       f"{out['age_sec']} 秒前心跳，负责 {len(out['jobs'])} 个重作业）")
    else:
        out["note"] = (f"调度 worker 心跳已停 {out['age_sec']} 秒"
                       f"（阈值 {stale_after:.0f}s，PID={out['pid']}）⇒ 重作业"
                       f"（{len(out['jobs']) or '?'} 个）**当前无人执行**；"
                       "查 data/run/scheduler-worker.log")
    return out
