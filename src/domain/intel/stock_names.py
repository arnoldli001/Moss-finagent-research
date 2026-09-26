"""代码 → 中文股票名（**零网络**，读本地行情仓库的名录表）。

> 用户口径（2026-09-25）："第二张图 被提及的标的都是 6 位编码，
> 需要映射成中文股票名。"

## 为什么必须本地查表，不能现查接口

这一层是**展示层兜底**，会被逐行调用（一页几十个标的）。现查接口的问题是：

  * 慢且不可控 —— 一页渲染要等 N 次网络往返；
  * 会被限流 —— 免费接口对高频单票查询不友好；
  * **会在最需要它的时候失败** —— 上游热度源出问题的那天，恰好也是
    页面最需要把"这是什么票"讲清楚的那天。

所以名字从本地 `data/quant/warehouse.db` 的 `quant_stock_directory`
（5567 只，来自日常行情同步）读取，只读打开、进程内缓存。

## ⚠️ 查不到时**不要造名字**

退化为只显示代码，并把 `known=False` 传出去 —— 编一个"看起来很像"的
中文名，用户是发现不了的（这正是本项目对 LLM 抽取做原文校验的同一条理由）。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 本地名录库（只读打开）
WAREHOUSE: Final = Path("data") / "quant" / "warehouse.db"

#: 纯 6 位 A 股代码
_CODE_ONLY: Final = re.compile(r"^\d{6}$")

#: 进程内缓存：`{code: name}`。名录一天变不了几次，不值得每次开库。
_CACHE: dict[str, str] = {}
#: 缓存是否已加载过（区分"没加载"与"加载了但是空表"）
_LOADED: bool = False


def _load(warehouse: Path | None = None) -> dict[str, str]:
    global _LOADED
    if _LOADED:
        return _CACHE
    path = warehouse or WAREHOUSE
    if not path.exists():
        logger.info("个股名录库不存在（%s），代码将只显示数字", path)
        _LOADED = True
        return _CACHE
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        try:
            rows = conn.execute(
                "SELECT code, name FROM quant_stock_directory").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:  # 库被占用/表不存在都不该拖垮页面
        logger.warning("个股名录读取失败（%s），代码将只显示数字",
                       type(exc).__name__)
        _LOADED = True
        return _CACHE
    for code, name in rows:
        c = str(code or "").strip().zfill(6)
        n = str(name or "").strip()
        if c and n:
            _CACHE[c] = n
    _LOADED = True
    logger.debug("个股名录载入 %d 条", len(_CACHE))
    return _CACHE


def name_of(code: Any) -> str:
    """代码 → 中文名。查不到返回空串（**不编**）。"""
    c = str(code or "").strip()
    if not c:
        return ""
    if not _CODE_ONLY.match(c):
        # 带市场前缀（SZ000592）也认
        c = re.sub(r"^(SH|SZ|BJ)", "", c.upper())
    if not _CODE_ONLY.match(c):
        return ""
    return _load().get(c, "")


def resolve(text: str) -> tuple[str, str]:
    """把一个"可能是代码、也可能是名字"的标的解析成 `(名称, 代码)`。

    用途：模型从原文里抽出来的"股票"经常就是那串 6 位代码
    （原文里只写了代码，没写名字）。这种时候要在**不改变事实**的前提下
    补上中文名 —— 代码是原文明写的，名字只是查表翻译，不算编造。

    返回 `("", "")` 表示这既不是已知代码、也不是可用名字。
    """
    s = str(text or "").strip()
    if not s:
        return "", ""
    if _CODE_ONLY.match(s):
        return name_of(s), s
    # "贵州茅台(600519)" / "贵州茅台（600519）" 这种混写
    m = re.match(r"^(.*?)[（(]\s*(\d{6})\s*[)）]\s*$", s)
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2)
    return s, ""


def annotate(code: Any, name: Any = "") -> dict[str, Any]:
    """给接口层用的统一形态：`{code, name, known}`。

    `known=False` 时前端**只显示代码**，不要用代码假装名字。
    """
    c = str(code or "").strip()
    n = str(name or "").strip()
    if not n and c:
        n = name_of(c)
    return {"code": c, "name": n, "known": bool(n)}


def reset_cache() -> None:
    """清缓存（测试/换库用）。"""
    global _LOADED
    _CACHE.clear()
    _LOADED = False


__all__ = ["WAREHOUSE", "annotate", "name_of", "reset_cache", "resolve"]
