"""日志装配：让 `src.*` 的 INFO 级业务日志**真的落到后端日志里**。

## 为什么需要这个模块（实测，不是洁癖）

本仓库**全仓库没有** `logging.basicConfig()` / `dictConfig()`（`grep` 可复核），
于是 root logger 没有 handler ⇒ Python 只启用 **last-resort handler**，
而它**只兜 WARNING 及以上**：

    logger.info("采集成功 …")   → 被静默丢弃
    logger.warning("采集缺口 …") → 出现在 data/run/*.log

本项目已经为这道门付过一次代价（`src/api/routes/research.py` 里那段注释）：
「调度器已启动（含被裁作业清单）」写成 INFO ⇒ **逐字节搜 `data/run/*.log` = 0 处**，
而 WARNING 级的「作业…上一轮未结束」在。**"我改了" → "有没有人看到" 是同一道门。**

## 做法（最小侵入）

只给 **`src` 这个命名空间**挂一个 `StreamHandler`（stderr → 由 `manage.py`
重定向进 `data/run/backend*.log`），**不碰 root、不碰 uvicorn 的 logger**：

* 级别由 `MOSS_LOG_LEVEL` 控制，默认 `INFO`；
* 挂了 handler 之后，`src.*` 的记录**不再触发** last-resort（避免重复打印）；
* `propagate` 保持 True：uvicorn/用户自定义的 root handler 仍然收得到。

## 谁调它

`src/api/main.py` 模块导入期调用一次（幂等）—— 任何入口只要 import 了 app
（uvicorn / 测试 / 脚本）都得到同一份装配。

## ★ 为什么先把 stdio 改成 UTF-8（2026-10-01 实测修）

现场：`data/run/backend.log` **不是合法 UTF-8** —— 偏移 1510584 处是
`QMT批量快照失败：…`，那些字节是 **GBK**（`\\xc5\\xfa\\xc1\\xbf` = "批量"）。

根因：`handler = logging.StreamHandler()` 写的是 `sys.stderr`，
而**守护进程的 `sys.stderr.encoding` 在中文 Windows 上是 cp936(GBK)**。
ASCII 行在两种编码下逐字节相同 ⇒ 这份日志前 1.5 MB 全是 ASCII、
**看着像 UTF-8**，直到有人要读中文才发现读不了 —— 而那正是排障最要紧的时刻。

所以这里在挂 handler **之前**把 stdio 显式重配成 UTF-8（与 `errors="replace"`，
保证再也不会因为一个不可编码字符把日志写挂）。历史日志的混编由
`src/core/log_reader.py` 的读侧回退兜住（不可能回炉重写，而运维必须能在凌晨 grep）。
"""

from __future__ import annotations

import logging
import os

#: 我们自己的日志命名空间（所有业务模块都是 `src.xxx`）。
NAMESPACE = "src"

#: 标记属性：幂等的判据（重复 import 不会挂第二个 handler）。
_MARKER = "_moss_logging_configured"

_DEFAULT_LEVEL = "INFO"

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _force_utf8_stdio() -> list[str]:
    """把 `sys.stdout/stderr` 重配成 UTF-8；返回**实际改动过**的流名（供日志）。

    为什么不用环境变量解决：`PYTHONIOENCODING` 只在**解释器启动时**生效，
    而这个模块可能被任何入口 import（uvicorn / 计划任务 / 值守拉起），
    那些启动参数不由我们控制。就地重配是唯一"不依赖别人记得传环境变量"的做法
    —— 与本项目"写在文档里的纪律会被忽略，写成判据的不会"同一条思路。
    """
    import sys

    changed: list[str] = []
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            enc = (getattr(stream, "encoding", "") or "").lower()
            if enc.replace("-", "") == "utf8":
                continue
            reconfigure(encoding="utf-8", errors="replace")
            changed.append(name)
        except (ValueError, OSError):  # 已被重定向/关闭的流：不致命
            continue
    return changed


def configure_src_logging(level: str | None = None) -> logging.Logger:
    """给 `src` 命名空间装配 handler（幂等）。返回该 logger。

    `level` 优先于环境变量 `MOSS_LOG_LEVEL`；都拿不到就用 `INFO`。
    非法级别名**不吞**：回落 INFO 并留一条 warning（否则"设了但不生效"）。
    """
    logger = logging.getLogger(NAMESPACE)
    if getattr(logger, _MARKER, False):
        return logger

    wanted = (level or os.environ.get("MOSS_LOG_LEVEL") or _DEFAULT_LEVEL).upper()
    resolved = getattr(logging, wanted, None)
    if not isinstance(resolved, int):
        logging.getLogger(__name__).warning(
            "MOSS_LOG_LEVEL=%r 不是合法级别名 → 回落 %s", wanted, _DEFAULT_LEVEL)
        resolved = logging.INFO

    # ★ 顺序要紧：先改 stdio 的编码，再挂 handler —— 否则 handler 抓到的是
    #   旧的（cp936）编码。见模块头「为什么先把 stdio 改成 UTF-8」。
    switched = _force_utf8_stdio()

    handler = logging.StreamHandler()          # 默认 stderr
    handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(resolved)
    logger.propagate = True
    setattr(logger, _MARKER, True)
    logger.debug("src 命名空间日志已装配：level=%s", logging.getLevelName(resolved))
    if switched:
        logger.info("日志编码已锁定 UTF-8（改了 %s）—— 避免 backend.log 混入 GBK",
                    "、".join(switched))
    return logger


__all__ = ["NAMESPACE", "configure_src_logging"]
