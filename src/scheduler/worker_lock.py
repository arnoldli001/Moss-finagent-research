"""worker **单实例锁**：同环境**只允许一个** worker（`CHG-0139`）。

## 为什么必须有（不是"保险"，是必需）

`HEAVY_JOBS` 里的 `quant_data_sync` 会**写行情仓**。两个 worker 同时跑 ⇒
同一个作业在同一分钟被执行两遍、往同一个 14 GiB 库里 upsert 两遍 ——
这正是 `CHG-0087` 记过的那次事故（dev 与 pilot 两个实例同时写仓库），
当时的成因是"两个 API 进程都跑更新作业"。

拆分把"谁跑这些作业"从**进程属性**变成了**角色 + 进程数**两件事：
角色由 `MOSS_SCHEDULER_ROLE` 决定（互斥，见 `registry.schedulable_jobs`），
而"只有一个"这件事没有任何东西保证 —— `manage.py start-worker` 手滑跑两次、
值守与手工启动撞在一起，都会静默变成两个。

## 为什么用**操作系统锁**，不用"PID 文件 + 判活"

PID 文件写法要处理一连串边界：文件在但进程已死（陈旧）、PID 被系统复用、
写入一半被杀留下半个 JSON …… 每一条都是一类新的静默失效。

而 `flock`/`msvcrt.locking` 的语义恰好就是我们要的那一条，**免费**：
锁属于**打开的文件句柄**，进程无论怎么死（含 `taskkill /F`、断电），
操作系统都会关掉句柄并释放锁 ⇒ **不存在"陈旧锁"这种状态**。

> 与 `scripts/pilot_watchdog.ps1` 的单实例锁（`[IO.File]::Open(..., 'None')`）
> 是同一个手法 —— 那个文件为"上一轮还在跑"这件事付过一次代价，这里的结论一样。

文件里仍然写一份 `{pid, env, started_at}`，但**只用于事后读现场**，
判断一律以"能不能拿到锁"为准（避免"文件内容"变成第二个事实源）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import IO, Any

LOCK_FILE = "worker.lock"

#: 加锁的字节偏移 —— **刻意远离内容区**（内容写在偏移 0 起的 JSON）。
#:
#: ★ 这个数字是判据逼出来的（2026-09-30 实测）：第一版锁在偏移 0，于是
#: `acquire()` 失败的进程**读不出持有者是谁** —— Windows 的字节范围锁对
#: **其他句柄的读**同样生效（`OSError: Permission denied`），而
#: "拿不到锁时说清是谁在跑"正是这段代码存在的理由之一（否则运维只能猜）。
#: 锁一个内容之外的字节，锁与内容就互不干扰。
LOCK_OFFSET = 4096


def worker_lock_path() -> Path:
    """锁文件路径 —— 由 `settings.scheduler_dir` 派生（环境隔离自动生效）。

    ⚠️ 与 `worker_heartbeat.heartbeat_path()` **同一套优先级**（先读
    `SCHEDULER_DIR` 环境变量）：两个文件必须落在**同一个目录**里
    —— 否则"锁在 dev、心跳在 pilot"这种错位会让 `/health` 报出一个
    与事实无关的结论。理由详见那个函数（`get_settings()` 是 `lru_cache`
    单例，父进程可能已缓存了别的环境）。
    """
    raw = os.environ.get("SCHEDULER_DIR")
    if raw:
        return Path(raw) / LOCK_FILE
    from src.core.config import get_settings

    return Path(get_settings().scheduler_dir) / LOCK_FILE


def _try_lock(handle: IO[bytes]) -> bool:
    """尝试对已打开的句柄加**非阻塞**排他锁。拿不到返回 False（绝不等待）。"""
    if os.name == "nt":
        import msvcrt

        handle.seek(LOCK_OFFSET)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle: IO[bytes]) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass  # 关掉句柄时操作系统反正会释放


class WorkerLock:
    """进程级排他锁；`acquire()` 拿不到就说明**已经有一个 worker 在跑**。

    典型用法（见 `src/scheduler/worker.py`）：

        lock = WorkerLock()
        if not lock.acquire():
            logger.error(...)   # 说清"谁在跑"，不要只说"启动失败"
            return 3
        try:
            ...
        finally:
            lock.release()
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or worker_lock_path()
        self._handle: IO[bytes] | None = None
        self.holder: dict[str, Any] = {}

    # ── 拿锁 ──
    def acquire(self, *, env: str | None = None) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # ⚠️ **不能用 `a+b`（追加模式）**：追加模式下 `seek(0)` 之后的写仍然
        #    落到文件末尾，于是"先 truncate 再写"会变成"截断成 0 字节"
        #    —— 锁文件内容永远是空的（拿不到锁的进程因此读不到持有者）。
        try:
            handle = self.path.open("r+b")
        except FileNotFoundError:
            handle = self.path.open("w+b")
        if not _try_lock(handle):
            # 拿不到：把**当前持有者**读出来，让报错能指出是谁（只说"已在运行"
            # 等于让人去猜 —— 本项目为"报错里没有现场"付过多次代价）
            try:
                handle.seek(0)
                raw = handle.read().decode("utf-8", errors="replace").strip()
                self.holder = json.loads(raw) if raw else {}
            except (OSError, ValueError):
                self.holder = {}
            finally:
                handle.close()
            return False

        self._handle = handle
        info = {"pid": os.getpid(), "env": env or os.environ.get("MOSS_ENV", ""),
                "started_at": time.time(),
                "started_at_iso": time.strftime("%Y-%m-%d %H:%M:%S")}
        # 覆盖写自己那一段（长度变化不影响锁：锁在 LOCK_OFFSET 那个字节上）
        #
        # ⚠️ **写失败绝不能让启动失败**（2026-09-30 实测踩到）：
        #   当时一个**旧版** worker 锁在偏移 0（那时还没有 LOCK_OFFSET），
        #   于是新进程"拿到了自己的锁、却在写内容时被拒"
        #   （`PermissionError: [Errno 13]`，因为偏移 0 正被别人锁着），
        #   结果 worker 直接崩掉 —— 而**锁本身是拿到了的**，本该正常干活。
        #   锁管"能不能跑"，内容只管"事后看得出是谁"：前者是判据，后者是线索，
        #   线索读不到/写不进时**不能让判据失效**。
        try:
            handle.seek(0)
            handle.truncate(0)
            handle.write(json.dumps(info, ensure_ascii=False).encode("utf-8"))
            handle.flush()
        except OSError:
            pass
        self.holder = info
        return True

    # ── 放锁 ──
    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        _unlock(handle)
        try:
            handle.close()
        except OSError:
            pass

    def __enter__(self) -> WorkerLock:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
