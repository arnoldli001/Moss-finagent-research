"""百度千帆「智能搜索」客户端 —— 搜索源的**第二家（备用）**（`CHG-0117`）。

## 为什么要有第二家

用户 2026-09-30 第二条要求原话：「**也可以联网查询获取两个数据源，作为备用**」。
`.trae/skills/backup-path-availability/SKILL.md` 的 R1 要求这条备用**必须真走一遍**
（本仓库已四次踩"备用只声明、从未走通"）。**实测已走通**（见 `__main__` 段的探针
与被 PRD 引用的记录）。

## 实测形态（不是照文档猜的 —— 官方文档页是 JS 壳，抓不到正文）

```
POST https://qianfan.baidubce.com/v2/ai_search/web_search
Authorization: Bearer <bce-v3/ALTAK-...>
{"messages":[{"role":"user","content":Q}],
 "search_source":"baidu_search_v2",
 "resource_type_filter":[{"type":"web","top_k":N}]}
→ 200 {"request_id": "...", "references":[{url,title,content,snippet,website,...}]}
```

## 闸门与博查**故意不共享**（R4：备用与主用不许共享单点）

| | 博查 | 百度千帆 |
|---|---|---|
| 额度性质 | **总量 1000 次**（一次性资源包） | **每天 100 次**（日配额） |
| 闸门 | 总量 900 + 每日 20 | **每日 60**（留 40 给人工体检） |
| 账本文件 | `run_dir/search_budget.json` | `run_dir/search_budget_baidu.json` |
| 结果缓存 | 各自独立 | 各自独立 |
| 凭据 | `bocha_search` | `baidusearch` |

**为什么不抽成一份共用闸门**：两者的**额度语义本来就不同**（总量 vs 日配额），
硬合并会逼出一个"两份配额取最严"的假口径；而且共享闸门等于**共享单点** ——
一家被限流会连带把另一家也锁住，那正是 R4 要防的。
**唯一的单一真值源**是**结果契约**（`SearchHit` / `SearchOutcome` 从 `bocha` 导入），
不是闸门。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from src.infrastructure.search.bocha import SearchHit, SearchOutcome

logger = logging.getLogger(__name__)

ENDPOINT = "https://qianfan.baidubce.com/v2/ai_search/web_search"
#: 百度侧是**日配额**（免费 100/天）⇒ 留 40 次给人工体检与其它用途。
MAX_CALLS_PER_DAY = 60
TIMEOUT_SEC = 20.0
CACHE_TTL_SEC = 24 * 3600.0

Transport = Callable[[str, dict[str, Any], dict[str, str], float], tuple[int, str]]


def _cache_dir():
    from src.infrastructure.catalog.data_stores import PROJECT_ROOT, store_rel

    p = PROJECT_ROOT / store_rel("cache_root") / "search" / "baidu"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _budget_path():
    from src.infrastructure.catalog.data_stores import PROJECT_ROOT, store_rel

    d = PROJECT_ROOT / store_rel("run_dir")
    d.mkdir(parents=True, exist_ok=True)
    return d / "search_budget_baidu.json"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _load_budget() -> dict[str, Any]:
    p = _budget_path()
    try:
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except Exception as exc:  # noqa: BLE001 读不到**不能**放行（fail-closed）
        logger.warning("百度搜索预算文件读取失败，按**已用尽**处理: %s", exc)
        return {"date": "", "calls_today": MAX_CALLS_PER_DAY, "corrupt": True}
    return {"date": "", "calls_today": 0, "calls_total": 0}


def budget_state() -> dict[str, Any]:
    b = _load_budget()
    used = int(b.get("calls_today", 0)) if b.get("date") == _today() else 0
    return {"used_today": used, "limit_today": MAX_CALLS_PER_DAY,
            "limit_total": None,      # 日配额制：没有"总量"这一说
            "used_total": int(b.get("calls_total", 0)),
            "exhausted": used >= MAX_CALLS_PER_DAY}


def _record_call() -> None:
    b = _load_budget()
    used = int(b.get("calls_today", 0)) if b.get("date") == _today() else 0
    try:
        _budget_path().write_text(json.dumps(
            {"date": _today(), "calls_today": used + 1,
             "calls_total": int(b.get("calls_total", 0)) + 1}), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("百度搜索预算写入失败（本次仍记为已花）: %s", exc)


def _cache_key(q: str, n: int) -> str:
    return hashlib.sha1(f"{q}|{n}".encode()).hexdigest()


def _cache_get(q: str, n: int) -> SearchOutcome | None:
    p = _cache_dir() / f"{_cache_key(q, n)}.json"
    try:
        if not p.exists():
            return None
        raw = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - float(raw.get("ts", 0)) > CACHE_TTL_SEC:
            return None
        return SearchOutcome(True, [SearchHit(**h) for h in raw.get("hits", [])],
                             "命中缓存", from_cache=True, served_by="baidu")
    except Exception:  # noqa: BLE001
        return None


def _cache_put(q: str, n: int, hits: list[SearchHit]) -> None:
    try:
        (_cache_dir() / f"{_cache_key(q, n)}.json").write_text(
            json.dumps({"ts": time.time(), "hits": [h.to_dict() for h in hits]}),
            encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("百度搜索结果缓存写入失败: %s", exc)


def _default_transport(url: str, payload: dict[str, Any],
                       headers: dict[str, str], timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def api_key() -> str:
    """从 **Settings** 读（`_env_field` ⇒ 同时吃环境变量与 `.env`）。

    ⚠️ **故意不吞异常** —— 与 `bocha.api_key()` 同一个教训：
    "代码坏了"绝不许伪装成"凭据没配"（本项目实测为此花过两轮）。
    """
    from src.core.config import get_settings

    return str(getattr(get_settings(), "baidu_search_api_key", "") or "")


def web_search(query: str, *, count: int = 5,
               transport: Transport | None = None,
               timeout: float = TIMEOUT_SEC,
               use_cache: bool = True,
               allow_spend: bool = True) -> SearchOutcome:
    """搜一次。契约与博查**完全一致**（同一个 `SearchOutcome`）。"""
    q = (query or "").strip()
    if not q:
        return SearchOutcome(False, reason="空 query", blocked_by="empty_query")

    if use_cache:
        hit = _cache_get(q, count)
        if hit is not None:
            return hit

    if not allow_spend:
        return SearchOutcome(False, reason="本次只允许读缓存（allow_spend=False）",
                             blocked_by="cache_only")

    st = budget_state()
    if st["exhausted"]:
        return SearchOutcome(
            False,
            reason=f"百度搜索日配额已用尽（今日 {st['used_today']}/"
                   f"{st['limit_today']}）—— 不再发起请求",
            blocked_by="budget")

    send = transport or _default_transport
    try:
        key = api_key()
    except Exception as exc:  # noqa: BLE001 分开报，不合并
        return SearchOutcome(
            False,
            reason=f"**配置读取失败**（不是凭据缺失）：{type(exc).__name__}: {exc}",
            blocked_by="config_error")
    if not key:
        return SearchOutcome(
            False, reason="未配置 baidusearch（环境变量与 .env 都没有）",
            blocked_by="no_key")

    _record_call()
    try:
        status, body = send(
            ENDPOINT,
            {"messages": [{"role": "user", "content": q}],
             "search_source": "baidu_search_v2",
             "resource_type_filter": [{"type": "web",
                                       "top_k": max(1, min(count, 20))}]},
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout)
    except Exception as exc:  # noqa: BLE001
        return SearchOutcome(False, reason=f"请求异常：{type(exc).__name__}: {exc}",
                             spent=True, blocked_by="transport")

    if status != 200:
        msg = body[:200]
        try:
            parsed = json.loads(body)
            msg = (parsed.get("error_msg") or parsed.get("message")
                   or parsed.get("error", {}).get("message") or msg)
        except Exception:  # noqa: BLE001
            pass
        return SearchOutcome(False, reason=f"HTTP {status}：{msg}", spent=True,
                             blocked_by="http")

    try:
        payload = json.loads(body)
        refs = payload.get("references") or []
    except Exception as exc:  # noqa: BLE001
        return SearchOutcome(False, reason=f"返回体解析失败：{exc}", spent=True,
                             blocked_by="parse")

    hits = [SearchHit(title=str(r.get("title", "")), url=str(r.get("url", "")),
                      snippet=str(r.get("snippet") or r.get("content") or "")[:400],
                      site=str(r.get("website", "")))
            for r in refs if isinstance(r, dict) and r.get("url")]
    if use_cache and hits:
        _cache_put(q, count, hits)
    return SearchOutcome(True, hits, f"返回 {len(hits)} 条", spent=True,
                         served_by="baidu")
