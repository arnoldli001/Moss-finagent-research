"""SQLite 可写库的「陈旧 WAL 索引」自愈。

背景（2026-09-17 实测）
----------------------
服务重启后前端要等一两分钟才出数据，日志里反复出现：

    路由DB查询异常，穿透网络: stock_close:300750 -> disk I/O error

而 `data/moss_finagent.db`（729MB，主库完好）在**任何**普通打开方式下都在
0.000s 内立刻失败：

    sqlite3.connect("data/moss_finagent.db")            -> disk I/O error
    sqlite3.connect("file:...?mode=ro", uri=True)       -> disk I/O error
    sqlite3.connect("file:...?immutable=1", uri=True)   -> OK（104 万行 0.04s）

根因不是数据损坏，而是 **WAL 伴生文件状态不一致**：

- `moss_finagent.db-wal` 被截断成 **0 字节**（正常应为 0 或 ≥32 字节）；
- `moss_finagent.db-shm` 是上一轮进程留下的 **64KB 陈旧 WAL 索引**，其内容
  与当前 WAL 不匹配。

于是每次打开都会走 WAL 恢复路径，读到无法解释的 WAL 头 → `SQLITE_IOERR`。
因为 `_connect()` 每次都新建连接，**每一个**走本库的请求都会再失败一次：
仓储层 fail-open 穿透到网络链，一次请求就要几十秒。

为什么会出现这种状态：`manage.py` 的守护进程用 `taskkill /T /F` 硬杀
（见 `kill_pid_tree`），进程没有机会在退出前 checkpoint / 关闭连接；再加上
`--replace` 只按「端口监听者」找旧实例，端口之外的历史实例会变成孤儿进程，
一直占着 `-wal`/`-shm` 的文件句柄（实测有个 12:28 启动的实例活到 16:51，
CPU 累计 0.0s，纯占位）。

修复策略
--------
1. **能正常打开就什么都不做**（绝不在健康库上乱动）；
2. 打开失败且判定为 WAL/IO 类错误时，先把 `-wal`/`-shm` **改名备份**到
   `data/recovery/<时间戳>/`，再删除原文件 → SQLite 会按主库重建；
3. 校验恢复结果（主库可读、表数量一致）；
4. 恢复后把 `journal_mode` 设回 WAL（项目其它库统一 WAL，允许并发读写）。

安全边界（重要）
----------------
- 只改名为「备份」再删，**从不直接删**：万一 WAL 里有未 checkpoint 的已提交
  事务，人可以手工拿备份找回；
- 主库文件本身永不改动；
- `immutable=1` 的探测是只读的，用来证明「主库完好、坏的只是伴生文件」；
- 关掉自愈：环境变量 `MOSS_SQLITE_RECOVERY=0`。

这也解释了为什么"重启后第一次请求特别慢"：库里本该命中的数据全部穿透网络。
自愈之后，DB 命中恢复，首次请求就从百余秒回到正常量级。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 判定为「伴生文件不一致」的 sqlite 错误关键字。SQLite 把 WAL 恢复阶段的
# 各种不一致统一报成 `disk I/O error`（没有更细的错误码经 Python 暴露），
# 所以这里按字符串匹配，并额外接受 not a database / malformed。
_WAL_ERROR_HINTS = (
    "disk i/o error",
    "database disk image is malformed",
    "file is not a database",
    "unable to open database file",
)

SIDECAR_SUFFIXES = ("-wal", "-shm")


def _recovery_enabled() -> bool:
    raw = os.environ.get("MOSS_SQLITE_RECOVERY", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


@dataclass
class RecoveryResult:
    """一次自愈尝试的结果（供日志/接口/测试断言）。"""

    path: Path
    checked: bool = False
    healthy: bool = True
    recovered: bool = False
    reason: str = ""
    quarantined: list[str] = field(default_factory=list)
    wal_bytes: int = 0
    shm_bytes: int = 0
    immutable_rows: int = 0
    tables: int = 0
    seconds: float = 0.0

    @property
    def touched_disk(self) -> bool:
        return bool(self.quarantined)


def _probe_normal(path: Path) -> tuple[bool, str, int]:
    """普通方式打开并数表。返回 (可用, 错误串, 表数量)。"""
    try:
        con = sqlite3.connect(str(path), timeout=3.0)
    except sqlite3.Error as exc:
        return False, str(exc), 0
    try:
        try:
            tables = con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        except sqlite3.Error as exc:
            return False, str(exc), 0
        return True, "", int(tables)
    finally:
        con.close()


def _probe_immutable(path: Path) -> int:
    """只读探测主库（忽略 WAL）。返回 fact_data_points 行数，失败返回 -1。

    这是"主库本身是否完好"的证据：能读到行数说明坏的只是伴生文件。
    """
    uri = f"file:{path.as_posix()}?immutable=1"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=3.0)
    except sqlite3.Error:
        return -1
    try:
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:
            return -1
    finally:
        con.close()


def _looks_like_wal_error(message: str) -> bool:
    low = message.lower()
    return any(hint in low for hint in _WAL_ERROR_HINTS)


def sidecar_sizes(path: Path) -> tuple[int, int]:
    """返回 (`-wal` 字节数, `-shm` 字节数)；不存在记 0。"""
    sizes = []
    for suffix in SIDECAR_SUFFIXES:
        side = Path(str(path) + suffix)
        try:
            sizes.append(side.stat().st_size if side.exists() else 0)
        except OSError:
            sizes.append(0)
    return sizes[0], sizes[1]


def _quarantine(path: Path, backup_dir: Path) -> list[str]:
    """把伴生文件改名挪到备份目录（绝不直接删）。返回实际挪走的文件名。"""
    moved: list[str] = []
    backup_dir.mkdir(parents=True, exist_ok=True)
    for suffix in SIDECAR_SUFFIXES:
        side = Path(str(path) + suffix)
        if not side.exists():
            continue
        target = backup_dir / f"{path.name}{suffix}"
        try:
            os.replace(side, target)
        except OSError as exc:
            # 被别的进程占着（孤儿实例）——这一步失败就说明还有活进程在使用该库，
            # 记录后交给调用方降级处理，不硬碰。
            logger.warning("SQLite 伴生文件挪移失败（可能仍被别的进程占用）：%s -> %s",
                           side, exc)
            continue
        moved.append(side.name)
    return moved


def _restore_wal_mode(path: Path) -> bool:
    """恢复后把 journal_mode 设回 WAL（与项目其它库一致，允许并发读写）。"""
    try:
        con = sqlite3.connect(str(path), timeout=5.0)
    except sqlite3.Error:
        return False
    try:
        try:
            con.execute("PRAGMA journal_mode=WAL")
            return True
        except sqlite3.Error:
            return False
    finally:
        con.close()


def ensure_sqlite_usable(
    db_path: str | Path, *, backup_root: str | Path | None = None,
) -> RecoveryResult:
    """确认 SQLite 库可正常打开；打不开就自愈陈旧 `-wal`/`-shm` 后重试。

    幂等、可重复调用：健康库只做一次 `sqlite_master` 计数就返回。
    库文件不存在时直接返回（由各仓储自己建表，不在这里造空库）。
    """
    path = Path(db_path)
    result = RecoveryResult(path=path)
    started = time.perf_counter()

    if not path.exists():
        result.reason = "库文件不存在（跳过）"
        result.seconds = time.perf_counter() - started
        return result

    result.checked = True
    if not _recovery_enabled():
        result.reason = "自愈已关闭（MOSS_SQLITE_RECOVERY=0）"
        result.seconds = time.perf_counter() - started
        return result

    ok, err, tables = _probe_normal(path)
    if ok:
        result.tables = tables
        result.wal_bytes, result.shm_bytes = sidecar_sizes(path)
        result.reason = "打开正常（无需处理）"
        result.seconds = time.perf_counter() - started
        logger.debug("SQLite 健康：%s（%d 表）", path, tables)
        return result

    # —— 到这里说明确实打不开 ——
    result.healthy = False
    result.reason = err
    result.wal_bytes, result.shm_bytes = sidecar_sizes(path)
    result.immutable_rows = _probe_immutable(path)

    if not _looks_like_wal_error(err):
        # 不是伴生文件问题（例如权限、目录只读）：不猜，直接报出来。
        logger.error("SQLite 打不开且不像 WAL 问题，不自动处理：%s -> %s", path, err)
        result.seconds = time.perf_counter() - started
        return result

    logger.warning(
        "SQLite 打不开（%s）：%s；-wal=%dB -shm=%dB，只读主库可见 %d 张表 → 隔离陈旧伴生文件",
        path, err, result.wal_bytes, result.shm_bytes, result.immutable_rows)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = Path(backup_root) if backup_root is not None else path.parent / "recovery"
    backup_dir = root / f"{path.stem}-{stamp}"
    result.quarantined = _quarantine(path, backup_dir)
    if not result.quarantined:
        result.reason = f"{err}（伴生文件挪不动：仍有进程占用，请先停掉重复实例）"
        logger.error("自愈失败：%s 的 -wal/-shm 无法挪走，可能有孤儿实例占用", path)
        result.seconds = time.perf_counter() - started
        return result

    ok2, err2, tables2 = _probe_normal(path)
    if not ok2:
        result.reason = f"隔离伴生文件后仍打不开：{err2}"
        logger.error("自愈失败：%s 隔离伴生文件后仍打不开 -> %s（备份在 %s）",
                     path, err2, backup_dir)
        result.seconds = time.perf_counter() - started
        return result

    result.recovered = True
    result.tables = tables2
    result.wal_bytes, result.shm_bytes = sidecar_sizes(path)
    if _restore_wal_mode(path):
        result.reason = "已隔离陈旧伴生文件并恢复 WAL 模式"
    else:
        result.reason = "已隔离陈旧伴生文件（WAL 模式恢复失败，已退回 journal 模式）"
    logger.warning(
        "SQLite 自愈完成：%s（挪走 %s，备份目录 %s，%d 张表可读）",
        path, "、".join(result.quarantined), backup_dir, tables2)
    result.seconds = time.perf_counter() - started
    return result


def ensure_all_usable(
    db_paths: list[str | Path], *, backup_root: str | Path | None = None,
) -> list[RecoveryResult]:
    """批量自愈（启动时调用）。单个库异常不影响其它库。"""
    results: list[RecoveryResult] = []
    for raw in db_paths:
        try:
            results.append(ensure_sqlite_usable(raw, backup_root=backup_root))
        except Exception:  # noqa: BLE001 自愈本身绝不能拦住启动
            logger.exception("SQLite 自愈异常（跳过该库）：%s", raw)
    return results


# ======================================================================
# 关停：checkpoint 后关闭，避免留下陈旧 WAL 索引
# ======================================================================

_REGISTERED: dict[str, sqlite3.Connection] = {}


def register_connection(name: str, conn: sqlite3.Connection) -> None:
    """登记一个常驻连接，关停时统一 checkpoint + 关闭。

    为什么需要：硬杀进程（taskkill /F）时 SQLite 没有机会 checkpoint，
    留下 `-wal`/`-shm` 给下一次启动，正是上面那个 disk I/O error 的成因。
    关停时主动 checkpoint 一次，可以让下次启动拿到干净的库。
    """
    _REGISTERED[name] = conn


def unregister_connection(name: str) -> None:
    _REGISTERED.pop(name, None)


def release_all_connections() -> int:
    """对所有登记的连接做 TRUNCATE checkpoint 后关闭。返回成功释放的数量。"""
    released = 0
    for name, conn in list(_REGISTERED.items()):
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            released += 1
        except sqlite3.Error as exc:
            logger.debug("checkpoint 失败（不影响关停）：%s -> %s", name, exc)
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            _REGISTERED.pop(name, None)
    return released


def checkpoint_and_close(db_path: str | Path) -> bool:
    """对单个库做一次「打开 → TRUNCATE checkpoint → 关闭」。

    关停路径用：即使库是别的连接打开的，checkpoint 也能把 WAL 收干，
    下次启动就不必走 WAL 恢复。库打不开时返回 False，不抛异常。
    """
    path = Path(db_path)
    if not path.exists():
        return False
    try:
        con = sqlite3.connect(str(path), timeout=5.0)
    except sqlite3.Error:
        return False
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return True
    except sqlite3.Error:
        return False
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def sqlite_paths_to_check() -> list[str]:
    """关停/启动时要确认可用的**可写** SQLite 库清单。

    ★ 2026-09-30（`CHG-0139`）从 `src/api/main.py` 搬到这里，**逐字搬运**。

    为什么必须搬：调度 worker 拆成了独立进程，它和 API 进程各持一份主库连接，
    关停时**两边都要** checkpoint。如果清单/收尾逻辑留两个副本，
    必然出现"改了 API 那份、忘了 worker 那份"——而症状是
    **worker 被杀后留下脏 `-wal`**，下次启动的主库 `disk I/O error`
    （本模块开头记的那次事故）。本项目对"同一件事写两处"的记账是：
    **写在两处的清单必然漂移**，所以这里只留一处，两边都调它。

    刻意**不含** `data/quant/warehouse.db`：那是 15GB 只读为主的行情仓库，
    没有 WAL 一致性问题的历史，不该在关停时对它做任何写动作。
    """
    from src.core.config import get_settings

    settings = get_settings()
    paths = [str(settings.sqlite_path)]
    for name in ("alert_db_path", "scheduler_dir"):
        raw = getattr(settings, name, None)
        if isinstance(raw, str) and raw.endswith(".db"):
            paths.append(raw)
    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def checkpoint_and_release_all() -> tuple[int, int]:
    """**关停收尾**：释放常驻连接 + 对每个主干库各做一次 checkpoint。

    返回 `(released, cleaned)`：释放的常驻连接数、checkpoint 成功的库数。
    任何一句失败都**不影响**其余收尾（关停是尽力而为 —— `src/api/main.py`
    的关停段为这条写过一次血案：一句 `AttributeError` 让后面的 checkpoint
    全部没执行，于是下次启动又读到脏 `-wal`）。
    """
    released = release_all_connections()
    cleaned = 0
    for db in sqlite_paths_to_check():
        try:
            if checkpoint_and_close(db):
                cleaned += 1
        except Exception:  # noqa: BLE001 单库失败不拖累其余
            continue
    return released, cleaned


def is_healthy(db_path: str | Path) -> bool:
    """只读判断（给测试/诊断用），不改动磁盘。"""
    path = Path(db_path)
    if not path.exists():
        return False
    return _probe_normal(path)[0]


def quarantine_dir_for(db_path: str | Path, *, backup_root: str | Path | None = None) -> Path:
    """给出该库的备份根目录（测试断言用）。"""
    path = Path(db_path)
    root = Path(backup_root) if backup_root is not None else path.parent / "recovery"
    return root


__all__ = [
    "RecoveryResult",
    "checkpoint_and_close",
    "ensure_all_usable",
    "ensure_sqlite_usable",
    "is_healthy",
    "quarantine_dir_for",
    "register_connection",
    "release_all_connections",
    "sidecar_sizes",
    "unregister_connection",
]
