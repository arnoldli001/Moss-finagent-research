"""博查（Bocha）Web Search 客户端 —— 本仓库**第一个真正的搜索引擎能力**。

## 为什么它必须"先有闸门再有用处"

用户 2026-09-30 提供凭据，并申请了**免费 1000 次**的资源包。注意这是
**总量**（不是每天 1000）—— 所以本模块的第一职责不是"搜得准"，而是
**永远不把这个额度悄悄烧完**：

* **默认 fail-closed**：没配 key / 额度用尽 / 被限流 ⇒ **一次都不发**，
  返回"没搜到 + 人话原因"，绝不抛异常给上层；
* **硬上限写进代码、状态落盘**（`AGENTS.md`：上限必须写进代码，不能留在注释里；
  闸门状态必须落盘，放进程内存里的冷却重启即失效）；
* **结果落盘缓存**：同一个 query 重复问不再花第二次钱；
* **只允许后台路径调用**（换源/补采），**绝不挂交互路径** ——
  搜索是"几百毫秒到数秒 + 花钱"，与 10s 防撞钟的交互预算不是一回事。

## 与 `check_external_sources.py` 的分工（**单一判据，不要两份**）

* 本模块 = **生产取数**（带闸门与缓存）；
* `scripts/check_external_sources.py` = **体检**（问"这条路现在通不通"）。

两者都打同一个 endpoint。**权威结论只有一个**：能取到结果就是通。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.bochaai.com/v1/web-search"

#: ★ 免费资源包是 **1000 次总量**。留出余量给人工体检（`check_external_sources.py`）。
MAX_CALLS_TOTAL = 900
#: 每日上限：总量之外再加一道日闸，避免一天之内被某个循环烧光。
MAX_CALLS_PER_DAY = 20
#: 单次请求超时（秒）。搜索是后台补救，不许长时间挂住。
TIMEOUT_SEC = 15.0
#: 结果缓存 TTL（秒）。同一 query 在一天内不重复花钱。
CACHE_TTL_SEC = 24 * 3600.0

#: 传输层签名：`(url, payload, headers, timeout) -> (status, body_text)`。
#: **可注入** ⇒ 离线单测不需要联网、不花钱（本项目"不花钱的测试用例"纪律）。
Transport = Callable[[str, dict[str, Any], dict[str, str], float], tuple[int, str]]


@dataclass(frozen=True)
class SearchHit:
    """一条搜索结果。只保留"找数据源"真正用得上的字段。"""

    title: str
    url: str
    snippet: str = ""
    site: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url,
                "snippet": self.snippet, "site": self.site}


@dataclass
class SearchOutcome:
    """一次搜索的结果 + **为什么**（失败必须能说清卡在哪一层）。

    `blocked_by` 是**机器可读**的判别位（`AGENTS.md`：判据只认机器可读的标识，
    不认给人看的文案）。**为什么必须有它**：`reason` 是人话，会为了讲清楚而
    包含别的词 —— 例如"配置读取失败（这不是'未配置凭据'）"里就**含有**
    "未配置"四个字，拿 `"未配置" not in reason` 当判据会红，而代码是对的
    （本轮实测：我就是这么写的，判据自己先红了）。
    """

    ok: bool
    hits: list[SearchHit] = field(default_factory=list)
    reason: str = ""
    from_cache: bool = False
    spent: bool = False          # 本次是否真的花了额度
    blocked_by: str = ""         # "" 成功 / empty_query / cache_only / budget
    #                              / config_error / no_key / transport / http / parse
    #                              / all_failed（仅 router 汇总时用）
    served_by: str = ""          # 由哪一家搜索源服务（router 填；单源调用为空）

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason,
                "blocked_by": self.blocked_by, "served_by": self.served_by,
                "from_cache": self.from_cache, "spent": self.spent,
                "hits": [h.to_dict() for h in self.hits]}


# ----------------------------------------------------------------------
# 路径（一律走登记的 store，不自己数 parents[N]）
# ----------------------------------------------------------------------
def _cache_dir():
    from src.infrastructure.catalog.data_stores import PROJECT_ROOT, store_rel

    p = PROJECT_ROOT / store_rel("cache_root") / "search"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _budget_path():
    from src.infrastructure.catalog.data_stores import PROJECT_ROOT, store_rel

    d = PROJECT_ROOT / store_rel("run_dir")
    d.mkdir(parents=True, exist_ok=True)
    return d / "search_budget.json"


def _load_budget() -> dict[str, Any]:
    p = _budget_path()
    try:
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except Exception as exc:  # noqa: BLE001 预算文件坏了**不能**放行（fail-closed）
        logger.warning("搜索预算文件读取失败，按**已用尽**处理: %s", exc)
        return {"date": "", "calls_today": MAX_CALLS_PER_DAY,
                "calls_total": MAX_CALLS_TOTAL, "corrupt": True}
    return {"date": "", "calls_today": 0, "calls_total": 0}


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def budget_state() -> dict[str, Any]:
    """当前闸门状态（供 `/health` 与体检命令展示，**读得到就要显示得出来**）。"""
    b = _load_budget()
    today = _today()
    used_today = int(b.get("calls_today", 0)) if b.get("date") == today else 0
    used_total = int(b.get("calls_total", 0))
    return {"used_today": used_today, "limit_today": MAX_CALLS_PER_DAY,
            "used_total": used_total, "limit_total": MAX_CALLS_TOTAL,
            "remaining_total": max(0, MAX_CALLS_TOTAL - used_total),
            "exhausted": used_today >= MAX_CALLS_PER_DAY
                         or used_total >= MAX_CALLS_TOTAL}


def _record_call() -> None:
    """记一次真实调用（**先记后发**：宁可少算一次，也不许多花一次）。"""
    b = _load_budget()
    today = _today()
    used_today = int(b.get("calls_today", 0)) if b.get("date") == today else 0
    payload = {"date": today, "calls_today": used_today + 1,
               "calls_total": int(b.get("calls_total", 0)) + 1}
    try:
        _budget_path().write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("搜索预算写入失败（本次仍记为已花）: %s", exc)
    if payload["calls_total"] >= MAX_CALLS_TOTAL:
        logger.error("博查搜索额度**已用尽**（%d/%d）—— 后续一律不再发起请求",
                     payload["calls_total"], MAX_CALLS_TOTAL)


def _cache_key(query: str, count: int) -> str:
    return hashlib.sha1(f"{query}|{count}".encode()).hexdigest()


def _cache_get(query: str, count: int) -> SearchOutcome | None:
    p = _cache_dir() / f"{_cache_key(query, count)}.json"
    try:
        if not p.exists():
            return None
        raw = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - float(raw.get("ts", 0)) > CACHE_TTL_SEC:
            return None
        hits = [SearchHit(**h) for h in raw.get("hits", [])]
        return SearchOutcome(True, hits, "命中缓存", from_cache=True)
    except Exception:  # noqa: BLE001 缓存坏了当没有，不是错误
        return None


def _cache_put(query: str, count: int, hits: list[SearchHit]) -> None:
    try:
        (_cache_dir() / f"{_cache_key(query, count)}.json").write_text(
            json.dumps({"ts": time.time(),
                        "hits": [h.to_dict() for h in hits]}),
            encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("搜索结果缓存写入失败: %s", exc)


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

    ⚠️ **不要**在这里写 `os.environ.get`：`.env` 的值只会被 pydantic 读进
    Settings，**不会注入进程环境**，于是"`.env` 里明明写着却报未配置"
    —— 见 `src/core/config.py` 里 `zhipu_api_key` 那段的实测记录。

    ## ★★ 这里**故意不吞异常**（2026-09-30 实测踩到，代价是两轮误判）

    本函数原来写成：

        try:
            from src.core.config import settings   # ← 这个单例**不存在**
            return str(getattr(settings, "bocha_search_api_key", "") or "")
        except Exception:
            return ""

    两处错叠在一起：① 导入的是一个不存在的名字（真名是 `get_settings()`）；
    ② `except Exception: return ""` 把 **ImportError 吞成了"未配置凭据"**。

    后果：`api_key()` 恒为空 ⇒ 上层报「未配置 bocha_search」，而 `.env` 里
    明明写着、`Settings` 里也明明读到了（我把两轮时间花在查凭据上）。
    这正是本项目反复出现的形状：**"代码坏了"伪装成"配置没配"**，
    排查方向被整个带偏。

    所以现在：**导入失败就让它抛**，由 `web_search` 分成两个**不同**的结论
    （`读取失败` vs `未配置`）—— 「没量到」绝不与「量到 0」合并。
    """
    from src.core.config import get_settings

    return str(getattr(get_settings(), "bocha_search_api_key", "") or "")


def web_search(query: str, *, count: int = 5,
               transport: Transport | None = None,
               timeout: float = TIMEOUT_SEC,
               use_cache: bool = True,
               allow_spend: bool = True) -> SearchOutcome:
    """搜一次。**任何失败都返回 `ok=False` + 人话原因，绝不抛异常。**

    `allow_spend=False` 用于"只想看缓存"的路径（预热/巡检），不花额度。
    """
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
            reason=f"搜索额度闸门已关（今日 {st['used_today']}/{st['limit_today']}、"
                   f"总量 {st['used_total']}/{st['limit_total']}）—— 不再发起请求",
            blocked_by="budget")

    send = transport or _default_transport

    #: ★★ 「读取失败」与「未配置」**必须是两个结论**（机器判据看 `blocked_by`）。
    #: 把导入/配置错误当成"没配凭据"，会让排查方向整个跑偏（本项目实测踩过：
    #: `from src.core.config import settings` 这个不存在的名字被 `except` 吞掉，
    #: 于是报了两轮"未配置"，而 `.env` 里一直写着）。
    try:
        key = api_key()
    except Exception as exc:  # noqa: BLE001 分开报，不合并
        return SearchOutcome(
            False,
            reason=f"**配置读取失败**（不是凭据缺失）"
                   f"：{type(exc).__name__}: {exc}",
            blocked_by="config_error")
    if not key:
        return SearchOutcome(
            False, reason="未配置 bocha_search（环境变量与 .env 都没有）",
            blocked_by="no_key")
    _record_call()          # 先记后发：宁可少算一次，也不许多花一次
    try:
        status, body = send(
            ENDPOINT,
            {"query": q, "count": max(1, min(count, 20)),
             "summary": True, "freshness": "noLimit"},
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout)
    except Exception as exc:  # noqa: BLE001 网络问题不是"没有这个源"
        return SearchOutcome(False, reason=f"请求异常：{type(exc).__name__}: {exc}",
                             spent=True, blocked_by="transport")

    if status != 200:
        msg = body[:200]
        try:
            msg = json.loads(body).get("message", msg)
        except Exception:  # noqa: BLE001
            pass
        return SearchOutcome(False, reason=f"HTTP {status}：{msg}", spent=True,
                             blocked_by="http")

    try:
        payload = json.loads(body)
        values = (((payload.get("data") or {}).get("webPages") or {})
                  .get("value") or [])
    except Exception as exc:  # noqa: BLE001
        return SearchOutcome(False, reason=f"返回体解析失败：{exc}", spent=True,
                             blocked_by="parse")

    hits = [SearchHit(title=str(v.get("name", "")), url=str(v.get("url", "")),
                      snippet=str(v.get("snippet", ""))[:400],
                      site=str(v.get("siteName", "")))
            for v in values if v.get("url")]
    if use_cache and hits:
        _cache_put(q, count, hits)
    return SearchOutcome(True, hits, f"返回 {len(hits)} 条", spent=True)
