"""迅投QMT（xtquant）进程级访问护栏。

**为什么需要这个模块（2026-09-15 实测）**

服务进程曾在两次「用户新加一只票」后**无 traceback 猝死**（exit code 1，
uvicorn 连 "Shutting down" 都没来得及打印）。两次的现场都能看到 xtquant 在下载：

    GET /api/v1/intraday/daily?code=600176 HTTP/1.1 200 OK
      0%|          | 0/5 [00:00<?, ?it/s]        ← download_history_data 的进度条
    （进程结束，无任何 Python 异常栈）

排查发现 xtquant 有**两个各自持有一份客户端、且都在工作线程里调用**的入口：

  1. `src/infrastructure/connectors/xtquant_connector.py`（项目日线链，`/intraday/daily` 走它）
  2. `src/intraday/sources.py`（做T模块的分钟线/分时/快照）

两侧都没有任何串行化：用户快速切标的时，`asyncio.to_thread` 会把
`get_market_data_ex` / `download_history_data` / `get_full_tick` 并发压到同一个
QMT 终端上。xtquant 的下载接口是**原生（C++）实现，且非线程安全**，
一旦在原生层出错就是整个解释器消失 —— `try/except` 拦不住。

因此这里提供两件事：

- `qmt_lock()`：进程内唯一的一把可重入锁，**所有** xtquant 调用（含下载）都必须持有它，
  把「并发访问」这个变量直接消掉；
- `download_history_isolated()`：把补下载放到**独立子进程**里执行 ——
  子进程若原生崩溃，只死子进程，服务进程照常运行（与 `intraday/subproc.py`
  隔离 py_mini_racer 是同一个思路，但这里必须放在中性层，
  因为 project connector 与 intraday 模块都要用）。

调用方拿到的语义：`download_history_isolated()` **永不抛异常**，
失败返回 `(False, 原因)`，由上层决定回退到哪个行情源。
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from typing import Any

from src.core.errors import (
    BRIEF_TIGHT,
    brief,
)

logger = logging.getLogger(__name__)

# 进程内唯一：两个模块共用同一把锁才有意义（各自一把等于没锁）。
_QMT_LOCK = threading.RLock()

# 补下载默认超时（秒）。单个标的的历史补下载通常 1~5s，
# 给 45s 是为了容忍终端正在做别的下载任务；超时只影响这一次补下载。
DEFAULT_DOWNLOAD_TIMEOUT = 45.0


def qmt_lock() -> threading.RLock:
    """返回进程级 QMT 访问锁（可重入）。所有 xtquant 调用必须 `with qmt_lock():`。"""
    return _QMT_LOCK


def download_history_isolated(
    qmt_code: str, period: str, *,
    start_time: str = "", end_time: str = "",
    timeout: float = DEFAULT_DOWNLOAD_TIMEOUT,
) -> tuple[bool, str]:
    """在子进程里执行 xtdata 历史补下载。

    返回 `(是否成功, 失败原因)`；**任何情况下都不抛异常**。
    子进程的原生崩溃会表现为非零退出码，被转成失败原因而不是带走服务进程。

    ⚠️ 这是**阻塞**函数（`subprocess.run`，最长 `timeout` 秒）。在 async 代码里
    必须用 `download_history_awaited()`，否则会按住整个事件循环 —— 实测
    2026-09-17：盘后自动选股在启动后立刻跑，每次补下载最长阻塞 45 秒，
    前端 `/health` 排队 **111.8 秒**才返回（服务其实早已启动完成）。
    """
    script = (
        "from xtquant import xtdata\n"
        "xtdata.enable_hello = False\n"
        f"xtdata.download_history_data({qmt_code!r}, {period!r}, "
        f"start_time={start_time!r}, end_time={end_time!r}, "
        "incrementally=True)\n"
        "print('OK')\n"
    )
    with _QMT_LOCK:
        # 持锁执行：下载本身也是「对同一个 QMT 终端的调用」，
        # 与其它线程的读取并发正是要消除的那个变量。
        try:
            completed = subprocess.run(  # noqa: S603 固定解释器+固定参数，无 shell
                [sys.executable, "-X", "utf8", "-c", script],
                capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            logger.warning("QMT补下载超时(%.0fs): %s %s", timeout, qmt_code, period)
            return False, f"补下载超时({timeout:.0f}s)"
        except Exception as exc:  # noqa: BLE001 连子进程都起不来
            logger.warning("QMT补下载子进程启动失败(%s): %s", qmt_code, brief(exc, BRIEF_TIGHT))
            return False, f"补下载子进程启动失败：{brief(exc, BRIEF_TIGHT)}"
    if completed.returncode != 0:
        tail = (completed.stderr or b"").decode("utf-8", "replace").strip()[-200:]
        logger.warning(
            "QMT补下载子进程异常退出(code=%s): %s %s | stderr: %s",
            completed.returncode, qmt_code, period, tail or "（无输出）")
        return False, (f"补下载子进程异常退出(code={completed.returncode})"
                       f"{'：' + tail if tail else ''}")
    return True, ""


async def download_history_awaited(
    qmt_code: str, period: str, *,
    start_time: str = "", end_time: str = "",
    timeout: float = DEFAULT_DOWNLOAD_TIMEOUT,
) -> tuple[bool, str]:
    """`download_history_isolated` 的**不阻塞事件循环**版本（async 代码用它）。

    为什么必须单独有一个：`download_history_isolated` 里是 `subprocess.run`，
    在协程里直接调用会按住整个事件循环最长 `timeout` 秒。实测 2026-09-17：
    服务启动后盘后自动选股立刻开跑，`sources._load()` 的补下载把循环按住，
    期间连 `/health` 都 111.8 秒不返回 —— 用户看到的就是"重启后前端好久没数据"。

    语义与同步版完全一致（同样永不抛异常），只是丢到线程池里等。
    """
    import asyncio

    return await asyncio.to_thread(
        download_history_isolated, qmt_code, period,
        start_time=start_time, end_time=end_time, timeout=timeout)


def isolated_download_supported() -> bool:
    """是否可用隔离下载（xtquant 未安装时上层应直接走别的行情源）。"""
    try:
        import xtquant  # type: ignore # noqa: F401
    except ImportError:
        return False
    return True


# ==================================================================
# 订阅预热：让 QMT 把「当天」数据写进本地库
# ==================================================================

# 去重窗口（秒）：订阅是持久的，只需一次；失败时也不要在窗口内反复重试。
SUBSCRIBE_TTL = 60.0
_subscribed: dict[str, float] = {}


def subscribe_once(qmt_code: str, period: str, *,
                   client: Any = None, ttl: float = SUBSCRIBE_TTL) -> bool:
    """订阅某标的某周期，让 QMT 把最新（含**当天**）数据写入本地库。

    返回本次是否**新建**了订阅（调用方据此决定要不要等一小会儿再读 ——
    实测订阅后 1 秒内数据才落到本地）。

    ## 为什么必须有这一步（2026-09-16 两次实测）

    QMT **不会**自动把当天数据写进本地库；未订阅/补下载时 `get_market_data_ex`
    只返回上一交易日（甚至更早）的数据，而调用方往往察觉不到：

        分钟线：600036 当天 1m = 26 行（此前订阅过）；601988/600519 = 0 行
        日线  ：600036 1d 最新 = 2026-09-14（当天是 09-16，缺两天）
                588170 1d 最新 = 2026-09-15

    后果是"做T面板显示昨天一整天的分时""日K面板显示昨天的日线"。
    实测 `subscribe_quote(period=..., count=-1)` 能把它补齐（含历史）：

        1m：订阅前 0 行  → 1 秒后当日 38 行、覆盖 133 个交易日
        5m：订阅前 0 行  → 1 秒后当日  8 行、覆盖 172 个交易日
        1d：订阅前末根 20260914 → 1 秒后末根 **20260916**（当天形成中的bar）

    `count=-1` 是关键：它把历史一起推下来，于是"本地库非空但停在昨天"这种
    最隐蔽的情况也能修好。

    ## 为什么放在中性层、为什么不是 download_history_data
    两个模块都要用（`src/intraday/sources.py` 的做T链路、
    `src/infrastructure/connectors/xtquant_connector.py` 的项目日线链），
    分开各写一份去重表就会变成对同一个终端重复订阅。
    而 `download_history_data` 是**禁止在进程内调用**的接口（曾在无锁并发下
    原生崩溃带走整个服务进程，见本模块顶部记录），订阅属于与
    `get_full_tick`/`get_market_data_ex` 同一类只读接口，所以走这里。
    """
    key = f"{qmt_code}:{period}"
    now = time.monotonic()
    last = _subscribed.get(key)
    if last is not None and now - last < ttl:
        return False
    _subscribed[key] = now          # 先记时间：失败也不在同一窗口内反复重试
    if client is None:
        try:
            from xtquant import xtdata  # type: ignore
        except ImportError:
            return False
        xtdata.enable_hello = False
        client = xtdata
    with _QMT_LOCK:
        # 订阅也是对同一个 QMT 终端的原生调用，必须与读取串行化
        client.subscribe_quote(qmt_code, period=period, count=-1)
    return True


def reset_subscriptions() -> None:
    """测试用：清空订阅去重表（进程内订阅本身不会被取消）。"""
    _subscribed.clear()


def describe() -> dict[str, Any]:
    """自检信息（供 /health 或诊断脚本查看护栏是否生效）。"""
    return {
        "lock": type(_QMT_LOCK).__name__,
        "holders": getattr(_QMT_LOCK, "_is_owned", lambda: None)(),
        "download_isolated": True,
        "download_timeout": DEFAULT_DOWNLOAD_TIMEOUT,
        "subscribed": sorted(_subscribed),
    }
