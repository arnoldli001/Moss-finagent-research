"""崩溃隔离执行器：把「可能让进程原生崩溃」的第三方调用放到子进程里跑。

背景（2026-09-15 实测，3/3 复现）：
  akshare 的同花顺接口（如 stock_board_concept_info_ths）需要 py_mini_racer
  执行 JS 生成 hexin-v cookie。该库在 Windows + Python 3.12 下会**直接让整个
  Python 进程崩掉**（退出码 -1 / 0x80000003），**不是可捕获的异常** ——
  try/except 完全无效，整个投研服务会随之死掉。

结论：这类调用不能放在服务进程里。本模块用子进程隔离：
  - 子进程崩溃 / 超时 / 非零退出 → 返回 None，主进程安然无恙；
  - 正常返回 → 子进程用 JSON 把结果写到 stdout，主进程解析。
代价：每次调用多约 0.3~1s 进程启动开销（相对于崩溃风险完全值得），
且这些调用本身有分钟级TTL缓存，不会每请求都打。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from typing import Any

from src.core.errors import (
    BRIEF_DEFAULT,
    brief,
)

logger = logging.getLogger(__name__)

# 子进程里先注入的引导代码：保证 packages 可导入、统一 UTF-8、异常转成 JSON 错误。
# body 会被**整体再缩进一级**放进 try 里（否则多行 body 只有首行有缩进 → IndentationError）。
#
# 第 4 行装「东财直连回退」：akshare 的东财接口都跑在这个子进程里，而本机网络
# 对 `push2*.eastmoney.com` 做了 TLS SNI 阻断（详见 `src/core/eastmoney_direct.py`），
# 主进程装了也管不到子进程 —— 必须在这里再装一次。
_BOOTSTRAP_HEAD = """
import json, sys
sys.path.insert(0, ".")
try:
    from src.core.eastmoney_direct import install as _install_em_direct
    _install_em_direct()
except Exception:
    pass
def __emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str))
    sys.stdout.flush()
try:
"""
_BOOTSTRAP_TAIL = """
except BaseException as exc:
    __emit({"__error__": f"{type(exc).__name__}: {exc}"})
"""


def _indent(body: str, spaces: int = 4) -> str:
    pad = " " * spaces
    return "\n".join(
        (pad + line) if line.strip() else line for line in body.split("\n"))


async def run_json_subprocess(
    body: str, *, timeout: float = 25.0, label: str = "第三方调用",
) -> Any | None:
    """在子进程执行 body（需自行 __emit(结果)），返回解析后的 JSON 或 None。

    body 是 Python 源码片段，可用 `__emit(obj)` 回传结果。
    返回 None 表示：子进程崩溃 / 超时 / 输出非法 JSON / 业务异常。
    """
    script = _BOOTSTRAP_HEAD + _indent(body) + _BOOTSTRAP_TAIL
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-X", "utf8", "-c", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:  # noqa: BLE001 连进程都起不来（极少见）
        logger.warning("%s 子进程启动失败: %s", label, brief(exc, BRIEF_DEFAULT))
        return None
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        logger.warning("%s 子进程超时（%.0fs），已终止", label, timeout)
        return None
    except asyncio.CancelledError:
        # 被取消（服务关停 / 后台刷新任务被取消）也**必须收掉子进程**：
        # 只 kill 不 wait 会留下僵尸，而残留的 stdout/stderr 管道会让事件循环在
        # 关闭时一直等它 —— 表现为"关停/测试挂住不动"（实测：后台板块刷新被取消后
        # 整个 pytest 卡在 53% 十几分钟）。这是取消路径，不是超时路径，
        # 之前只处理了 TimeoutError。
        process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        raise
    if process.returncode != 0:
        # 原生崩溃（如 py_mini_racer）会走到这里，退出码通常为负
        tail = (stderr or b"").decode("utf-8", "replace")[-300:]
        logger.error(
            "%s 子进程异常退出（exit=%s）——大概率是 py_mini_racer 等原生库崩溃，"
            "已隔离，主进程继续运行。stderr尾部: %s",
            label, process.returncode, tail)
        return None
    text = (stdout or b"").decode("utf-8", "replace").strip()
    if not text:
        logger.warning("%s 子进程无输出（可能中途崩溃）", label)
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("%s 子进程输出非JSON: %s", label, text[:200])
        return None
    if isinstance(payload, dict) and "__error__" in payload:
        logger.warning("%s 子进程内报错: %s", label, str(payload["__error__"])[:200])
        return None
    return payload
