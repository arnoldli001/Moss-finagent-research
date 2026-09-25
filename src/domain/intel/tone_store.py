"""原文倾向的**抽取结果存储**（JSONL，按 `content_hash` 索引）。

## 为什么抽取要落库，而不是在接口里现算

本地模型单条实测 ~770ms。情报流一页 60 条，若在 `/intel/feed` 里逐条跑，
**会加 46 秒延迟** —— 接口直接从"秒回"退化成"超时"。

所以拆成两段，与既有设计一致（见 `intel.py` 模块 docstring 的
"为什么接口不做 LLM 分析"）：

    抽取（慢）  定时任务，2 小时一次，落这里
    读取（快）  接口只查这份文件，O(1) 按 hash 命中

## 为什么是 JSONL 而不是数据库表

  · 这份数据的读写模式极简：**按 `content_hash` 追加、按 hash 查**
  · 情报条目是**天级生命周期**（过期就不再看），不需要事务、不需要 join
  · 落 JSONL 可以 `tail -f` 直接看，排障比查表快
  · 保留策略简单：超过 N 天整体重写一次即可

## 存储的字段**只有倾向本身**，不含来源标识

`content_hash` 是内容指纹（不含来源），`tone`/`phrases`/`codes` 都来自
原文。**没有任何来源字段** —— 这份文件即使被读到也不泄漏渠道。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 存储位置（与 `data/intel/` 下的游标同级，已被 .gitignore 覆盖）
_STORE_DIR: Final = Path("data") / "intel"
_STORE_FILE: Final = "tone_results.jsonl"

#: 保留天数。情报条目的生命周期是天级 —— 超过这个天数的倾向结果
#: 不会再被任何请求命中（池子只取最近 500 条）。
RETAIN_DAYS: Final = 14

#: 内存缓存（进程内）。键 = `content_hash`，值 = 结果 dict。
#:
#: 为什么要它：接口每次请求都要按 hash 查 60 次，每次都读文件太浪费。
#: 缓存在**进程启动时**一次载入，之后只追加 —— 文件是追加写的，
#: 所以缓存不会与文件不一致（除非有别的进程在写，那种情况下
#: `invalidate()` 会被调度任务调用）。
_CACHE: dict[str, dict[str, Any]] = {}
_LOADED = False


def store_path(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / _STORE_DIR / _STORE_FILE


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load(*, root: Path | None = None, force: bool = False) -> dict[str, dict]:
    """载入全部结果到内存缓存（首次调用时读文件）。

    文件不存在 / 读坏了都**返回空字典**而不是抛错 ——
    倾向是"锦上添花"的数据，它缺失时情报流应当照常工作（用规则层兜底），
    不该让整个页面挂掉。
    """
    global _LOADED
    if _LOADED and not force:
        return _CACHE
    _CACHE.clear()
    p = store_path(root)
    if p.exists():
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue          # 单行坏了跳过，不放弃整个文件
                h = str(obj.get("content_hash") or "")
                if h:
                    # 后写的覆盖先写的（同一 hash 可能被重抽过）
                    _CACHE[h] = obj
        except OSError as exc:
            logger.warning("倾向结果读取失败（按空处理）：%s", type(exc).__name__)
    _LOADED = True
    return _CACHE


def get(content_hash: str, *, root: Path | None = None) -> dict[str, Any] | None:
    """按内容指纹取一条倾向结果。**没抽过返回 `None`**（不编造）。"""
    if not content_hash:
        return None
    return load(root=root).get(content_hash)


def save_many(results: list[dict[str, Any]], *,
              root: Path | None = None) -> int:
    """追加写入一批结果，返回写入条数。

    每条至少要有 `content_hash` 与 `tone`，否则跳过 ——
    没有 hash 的结果永远查不回来，写进去只是垃圾。
    """
    rows = []
    for r in results:
        h = str(r.get("content_hash") or "")
        if not h or not r.get("tone"):
            continue
        rows.append({"content_hash": h, "at": _now(), **r})
    if not rows:
        return 0

    p = store_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    for r in rows:
        _CACHE[r["content_hash"]] = r
    return len(rows)


def prune(*, root: Path | None = None, retain_days: int = RETAIN_DAYS) -> int:
    """删掉过期行（按 `at` 字段），返回删除条数。

    为什么按**整体重写**而不是原地删：JSONL 无法原地删除，
    而这份文件量级很小（实测每天几千行），重写成本可忽略。
    """
    p = store_path(root)
    if not p.exists():
        return 0
    cutoff = (datetime.now(timezone.utc).astimezone()
              - timedelta(days=max(1, retain_days)))
    kept: list[str] = []
    removed = 0
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for line in lines:
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except ValueError:
            removed += 1
            continue
        try:
            at = datetime.fromisoformat(str(obj.get("at") or ""))
        except ValueError:
            kept.append(s)        # 时间戳坏了保守保留（宁可多留）
            continue
        if at >= cutoff:
            kept.append(s)
        else:
            removed += 1
    if removed:
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""),
                       encoding="utf-8")
        tmp.replace(p)
        load(root=root, force=True)
    return removed


def stats(*, root: Path | None = None) -> dict[str, Any]:
    """存储概况（供管理员页与排障用）。"""
    data = load(root=root)
    tones: dict[str, int] = {}
    for v in data.values():
        t = str(v.get("tone") or "未定")
        tones[t] = tones.get(t, 0) + 1
    p = store_path(root)
    return {
        "count": len(data),
        "tones": tones,
        "path": str(p),
        "bytes": p.stat().st_size if p.exists() else 0,
    }


__all__ = [
    "RETAIN_DAYS",
    "get",
    "load",
    "prune",
    "save_many",
    "stats",
    "store_path",
]
