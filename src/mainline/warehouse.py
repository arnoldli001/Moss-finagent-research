"""行情仓（`data/quant/warehouse.db`）只读连接的**唯一入口**。

## 为什么要有这个模块

`data/quant/warehouse.db` 是 15 GiB 的只读行情仓，但它的 WAL/共享内存在
"有别的进程并发访问"时会坏掉 —— 读者拿到 `sqlite3.OperationalError:
disk I/O error`。实测 2026-09-21：机器上有 5 个 `uvicorn --port 8100`
后端进程同时打开着它。

这个问题**咬了三次**，每次都只是"换个地方自己开连接"：

    第一次  `sources.WarehouseSource._connect`      → 已加 immutable 兜底
    第二次  `relevance.py` 里 4 处自建连接           → 提纯整条链路静默失效，
            导致「锂电池概念」把 MLCC 的风华高科选成龙头（用户报障）
    第三次  `member_pure.load_market_caps`           → 提纯重建直接崩

根因不是"漏了某处"，而是**没有唯一入口**：每个模块都觉得自己
`sqlite3.connect(..., mode=ro)` 一下就行。所以这里把它收成一个函数，
所有行情仓读取都走它 —— 再也不要自己开。

## 兜底策略

正常 `mode=ro` 失败（I/O 或 lock 类错误）时退化到 **`immutable=1`**：
告诉 SQLite「这个文件在读期间不会变」，从而**完全跳过 WAL 与加锁**。
对这个只读行情仓这是正确语义，也更快。

⚠️ 关键细节：`sqlite3.connect` 是**惰性**的，不真正打开文件 ——
故障要到第一次 `execute` 才暴露。所以这里必须**探测一次**（`SELECT 1`），
否则兜底根本不会触发。（这一点我在前两次修复里都差点漏掉。）

一旦退化过就**记住**（`prefer_immutable()`），后续直接用可用路径：
这是环境性故障、不会自愈，不记住的话每天要重复付出失败连接的代价。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

#: 默认行情仓路径（与 `sources.WarehouseSource` 的默认值一致）
DEFAULT_WAREHOUSE = "data/quant/warehouse.db"
#: 只读打开失败后是否改用 `immutable=1`（进程级记忆）
_IMMUTABLE = False
_IMMUTABLE_LOCK = threading.Lock()


def prefer_immutable() -> bool:
    """当前是否已切换到 `immutable=1` 模式（供状态输出/自检用）。"""
    return _IMMUTABLE


def _is_io_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "disk i/o error" in text or "locked" in text or "unable to open" in text


def open_warehouse(path: str | Path = DEFAULT_WAREHOUSE, *,
                   timeout: float = 60.0) -> sqlite3.Connection:
    """打开行情仓的只读连接（所有行情仓读取都必须走这里）。

    失败时抛 `sqlite3.Error` —— **调用方必须处理**，不要让它退化成
    "返回空列表然后继续算分"：那正是第二次事故里"静默降级"的形状。
    """
    global _IMMUTABLE
    candidate = Path(path)
    if not candidate.exists():
        raise sqlite3.OperationalError(f"行情仓不存在：{candidate}")

    params = "mode=ro&immutable=1" if _IMMUTABLE else "mode=ro"
    try:
        conn = sqlite3.connect(f"file:{candidate.as_posix()}?{params}",
                               uri=True, timeout=timeout)
        conn.row_factory = sqlite3.Row
        if not _IMMUTABLE:
            conn.execute("SELECT 1").fetchone()   # 惰性打开：必须探一次
        return conn
    except sqlite3.OperationalError as exc:
        if not _is_io_error(exc):
            raise
        with _IMMUTABLE_LOCK:
            first = not _IMMUTABLE
            _IMMUTABLE = True
        if first:
            logger.warning(
                "行情仓只读打开失败（%s），**本进程后续改用 immutable=1** "
                "—— 通常意味着有别的进程在并发写这个只读库（%s）。"
                "建议只保留一个后端进程。", exc, candidate)
        conn = sqlite3.connect(
            f"file:{candidate.as_posix()}?mode=ro&immutable=1",
            uri=True, timeout=timeout)
        conn.row_factory = sqlite3.Row
        return conn


__all__ = ["DEFAULT_WAREHOUSE", "open_warehouse", "prefer_immutable"]
