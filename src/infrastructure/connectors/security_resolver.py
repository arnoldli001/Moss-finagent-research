"""A股证券识别：6位代码 / 中文简称 → (代码, 简称)。

前端用户常以中文名称提问（如"中际旭创"），但采集契约要求6位代码
（stock_close:300308、stock_news_em(code)）。名称表来自akshare
stock_info_a_code_name（约5500只）。

可靠性设计（akshare该接口偶发长时间不响应且无内置超时）：
- 成功拉取后落盘 data/security_names.json（7天内视为新鲜，直接读盘）；
- 冷启动/过期时网络等待上限15秒，超时则降级为过期缓存或None，不拖垮API；
- 超时线程继续运行，完成后仍会写盘，后续进程/重启即享缓存；
- akshare不可用时返回None，由调用方降级（不阻断主链路）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.errors import (
    BRIEF_TIGHT,
    brief,
)

logger = logging.getLogger(__name__)

# 6位A股代码（前后不能紧邻数字/字母，避免从长串中误切）
_CODE_RE = re.compile(r"(?<![0-9A-Za-z])([036]\d{5})(?![0-9A-Za-z])")

_CACHE_FILE = Path(__file__).resolve().parents[3] / "data" / "security_names.json"
_CACHE_TTL = timedelta(days=7)
_NETWORK_TIMEOUT_S = 15.0
_ASYNC_TIMEOUT_S = 25.0

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="name-table")
_lock = threading.Lock()
_pairs_cache: tuple[tuple[str, str], ...] | None = None


def extract_code(text: str) -> str | None:
    """从文本中提取首个6位A股代码段。"""
    if not text:
        return None
    m = _CODE_RE.search(text)
    return m.group(1) if m else None


def _fetch_and_persist() -> tuple[tuple[str, str], ...]:
    """拉取akshare名称表并原子落盘（在工作线程中执行，超时后仍可能完成写盘）。"""
    import akshare as ak

    df = ak.stock_info_a_code_name()
    pairs = tuple(zip(df["code"].astype(str), df["name"].astype(str), strict=True))
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "pairs": pairs,
    }
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_FILE.with_name(".security_names.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _CACHE_FILE)
    except OSError as exc:  # 落盘失败不影响当次解析
        logger.warning("证券名称表缓存落盘失败: %s", exc)
    return pairs


def _load_disk() -> tuple[tuple[tuple[str, str], ...] | None, bool]:
    """返回(名称对, 是否新鲜)；损坏/缺失返回(None, False)。"""
    try:
        payload = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        pairs = tuple((str(c), str(n)) for c, n in payload["pairs"])
        fetched = datetime.fromisoformat(payload["fetched_at"])
        fresh = datetime.now(timezone.utc) - fetched < _CACHE_TTL
        return pairs, fresh
    except (OSError, ValueError, KeyError):
        return None, False


def _name_pairs() -> tuple[tuple[str, str], ...]:
    """名称表：内存缓存 → 新鲜磁盘缓存 → 有界网络（超时降级过期缓存/抛错）。"""
    global _pairs_cache
    if _pairs_cache is not None:
        return _pairs_cache
    with _lock:
        if _pairs_cache is not None:
            return _pairs_cache
        disk, fresh = _load_disk()
        if fresh and disk:
            _pairs_cache = disk
            return disk
        future = _executor.submit(_fetch_and_persist)
        try:
            _pairs_cache = future.result(timeout=_NETWORK_TIMEOUT_S)
        except FuturesTimeout:
            if disk:  # 过期缓存仍可用；后台线程完成后会刷新磁盘
                logger.warning("证券名称表网络拉取超时(%ss)，使用过期本地缓存", _NETWORK_TIMEOUT_S)
                _pairs_cache = disk
                return disk
            raise
        return _pairs_cache


def resolve_stock_sync(text: str) -> tuple[str, str] | None:
    """同步解析：优先6位代码，其次精确简称，最后最长名称包含匹配。

    最长匹配防止短名误命中（如"长城"同时出现在"长城汽车/长城电工"问句中时，
    以更长的完整名称为准；仍有多个等长候选时取第一个，调用方可在日志中看到）。
    """
    if not text:
        return None
    code = extract_code(text)
    if code:
        try:
            name = next((n for c, n in _name_pairs() if c == code), "")
        except Exception:  # noqa: BLE001 名称表不可达时代码本身仍有效
            name = ""
        return code, name
    try:
        pairs = _name_pairs()
    except Exception as exc:  # noqa: BLE001 akshare缺失/断网/超时且无缓存
        logger.warning("证券名称表不可用，无法解析 '%s': %s", text[:30], brief(exc, BRIEF_TIGHT))
        return None
    # 精确匹配（target通常就是"中际旭创"）
    for c, n in pairs:
        if n and n == text.strip():
            return c, n
    # 包含匹配：取名称最长者，降低短词误伤
    best: tuple[str, str] | None = None
    for c, n in pairs:
        if n and len(n) >= 2 and n in text:
            if best is None or len(n) > len(best[1]):
                best = (c, n)
    return best


async def resolve_stock(text: str) -> tuple[str, str] | None:
    """异步入口（名称表首次加载走网络，放线程池并设上限，防止请求无限挂起）。"""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(resolve_stock_sync, text), timeout=_ASYNC_TIMEOUT_S
        )
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning("证券名称解析整体超时(%ss): %s", _ASYNC_TIMEOUT_S, text[:30])
        return None
