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


def resolve_stocks_sync(text: str) -> list[tuple[str, str]]:
    """★ 文本里出现的**全部**个股，按**出现顺序**去重返回（`CHG-0216`）。

    ## 为什么需要它（用户报障）

    > 用户输入含有 **2 个及以上**的个股…此时采集数据会存在**漏掉一些股票**的信息获取。
    > 例：「…未来半年能否持有高股息的**宁波银行**和**中国神华**？」标的 `601088`
    > —— 反馈中国神华缺个股估值与股息、宁波银行没有任何可引用的估值，
    > 而本地数据库里明显有数据。

    `resolve_stock_sync` 只返回**一个**（最长匹配）—— 那对"这一轮分析谁"是对的，
    但**不足以**回答"要采几只股票的数据"。于是规划层只能给一只排个股指标，
    另一只**一个指标都没有**，症状是"该股没有估值/股息数据"。

    ## 匹配规则与 `resolve_stock_sync` **同源**（不新造一套）

    * 只用同一份 `_name_pairs()` 名称表；
    * 同样**最长优先**（防"长城"误命中"长城汽车"）；
    * 差别只是**聚合方式**：这里从左到右扫一遍、命中即**跳过该段**
      （所以"长城汽车"不会被再拆出"长城"），而 `resolve_stock_sync`
      取全局最长的那**一个**。

    ⚠️ 6 位数字走**名称表核对**（不重复实现 `extract_code` 的前缀规则）：
    表里有这个代码才算个股 —— 表本身就是"A 股全集"的单一真值源。
    """
    if not text:
        return []
    try:
        pairs = _name_pairs()
    except Exception as exc:  # noqa: BLE001 名称表不可用 ⇒ 宁可返回空，不猜
        logger.warning("证券名称表不可用，无法做多标的解析 '%s': %s",
                       text[:30], brief(exc, BRIEF_TIGHT))
        return []

    by_code = {c: n for c, n in pairs if c}
    #: 首字 → 该字开头的名称（按长度降序 ⇒ 每步先试最长）
    by_first: dict[str, list[str]] = {}
    for _c, n in pairs:
        if n and len(n) >= 2:
            by_first.setdefault(n[0], []).append(n)
    for names in by_first.values():
        names.sort(key=len, reverse=True)
    name_to_code = {n: c for c, n in pairs if n}

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    i = 0
    n_len = len(text)
    while i < n_len:
        # ① 6 位代码（表里认得的才算）
        chunk = text[i:i + 6]
        if len(chunk) == 6 and chunk.isdigit() and chunk in by_code:
            if chunk not in seen:
                seen.add(chunk)
                found.append((chunk, by_code[chunk]))
            i += 6
            continue
        # ② 名称（最长优先；命中即跳过整段，避免子串再命中）
        hit = ""
        for name in by_first.get(text[i], ()):
            if text.startswith(name, i):
                hit = name
                break
        if hit:
            code = name_to_code.get(hit, "")
            if code and code not in seen:
                seen.add(code)
                found.append((code, hit))
            i += len(hit)
            continue
        i += 1
    return found


async def resolve_stocks(text: str) -> list[tuple[str, str]]:
    """`resolve_stocks_sync` 的异步入口（同 `resolve_stock` 的超时与线程池口径）。"""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(resolve_stocks_sync, text), timeout=_ASYNC_TIMEOUT_S
        )
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning("多标的解析整体超时(%ss): %s", _ASYNC_TIMEOUT_S, text[:30])
        return []
