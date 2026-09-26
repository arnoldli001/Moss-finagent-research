"""平台热议聚合：跑本地模型，把**各平台快讯**聚成"最热的个股与事件"。

> 用户口径（2026-09-25）：
> "为什么不把情报流、舆情热度 有明显利空或利多的信息，都通过事件告警弹出。
>  当前舆情热度监控没做好，不如直接融入到情报流里，把几大平台
>  （雪球/东方财富股吧/同花顺/财联社/百度人气榜/韭研公社）的热点事件和个股，
>  聚合汇总显示在情报流或事件告警里。"

## 为什么单独一个任务（而不是在接口里算）

与倾向/摘要同一个理由：本地模型一批 40 条要 30 秒 ——
接口里算会把请求拖到超时。所以走定时任务落库，接口只读。

## ⚠️ 这一版改了什么（以及为什么原版等于没跑）

原版的输入是**知识星球研报**（`build_feed` 里的 `research_note`），而且
**从来没被任何调度任务调用过** —— 只有调试脚本跑过它。两个后果叠加：

  · 页面上"热门个股/热议事件"永远是空的或极旧；
  · 空的原因看起来像"上游没数据"，实际是"这条链路压根没接上"。

现在输入换成**各平台的公开快讯**（财联社/富途/东财/同花顺/新浪），
这才是用户说的"各大金融平台的热议事件、新闻热搜事件"。
研报/笔记仍可作补充，但不作主料 —— 它们是"机构在看什么"，
不是"市场在热议什么"，两者经常不是同一批标的。

## 与 `tone_job` 的分工

    tone_job      逐条：这条原文什么语气、压缩成一句摘要
    本模块         整批：这一批材料里**谁被讨论得最多、结论偏哪边**

两者互补 —— 单条倾向回答"这条怎么读"，热度聚合回答"今天该看哪几只"。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

from src.domain.intel.hot_topics import (
    BATCH_SIZE,
    MAX_EVENTS,
    MAX_STOCKS,
    OUTPUT_SCHEMA,
    SYSTEM_PROMPT,
    HotStock,
    HotTopic,
    build_prompt,
    merge_stocks,
    parse_output,
)

logger = logging.getLogger(__name__)

#: 结果落盘位置
_STORE_DIR: Final = Path("data") / "intel"
_STORE_FILE: Final = "hot_topics.json"

#: 单次最多处理多少条材料（多批）
MAX_NOTES_PER_RUN: Final = 160

#: 只看最近多少小时的材料。
#:
#: ⚠️ 必须有时窗：快讯一天几百条，不设窗就会把三天前的旧闻和刚发生的事
#: 一起聚合，结果里的"热门"变成"存量最多的"，而不是"现在最热的"。
#: 24 小时覆盖一个完整交易日 + 前后夜盘。
WINDOW_HOURS: Final = 24

#: 送模型的材料类型（**平台快讯为主**，研报为辅）。
#:
#: `policy` 也收：新闻联播/上期所是"官方口径的热点"，与市场热议互补。
HOT_KINDS: Final[frozenset[str]] = frozenset(
    {"newswire", "broker_report", "policy", "research_note"})

#: 结果缓存（进程内）。键固定为 `"latest"` —— 热度是**当前快照**，
#: 不做历史序列（用户要的是"现在在讨论什么"）。
_CACHE: dict[str, Any] = {}


def store_path(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / _STORE_DIR / _STORE_FILE


def load(*, root: Path | None = None, force: bool = False) -> dict[str, Any]:
    """读最近一次聚合结果。读不到返回空 dict（页面照常渲染，只是没内容）。"""
    if _CACHE.get("latest") is not None and not force:
        return _CACHE["latest"]
    p = store_path(root)
    data: dict[str, Any] = {}
    if p.exists():
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                data = obj
        except (OSError, ValueError):
            logger.warning("热度聚合结果读取失败（按空处理）")
    _CACHE["latest"] = data
    return data


def save(data: dict[str, Any], *, root: Path | None = None) -> None:
    p = store_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(p)
    _CACHE["latest"] = data


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def recent_pool(items: list[dict[str, Any]], *,
                hours: int = WINDOW_HOURS,
                limit: int = MAX_NOTES_PER_RUN) -> list[dict[str, Any]]:
    """从情报条目里挑出"最近 N 小时的平台快讯"，最新的在前。

    时间解析走 `sort_key()`（它同时认紧凑/ISO 两种格式，认不出的给空串），
    所以按它倒序就是**跨格式安全**的排序。

    ⚠️ 时间解析不出来的条目给空串 → 落在窗口判定之外，但**不直接丢**：
    它们只是"无法判定新鲜度"，不是"过期"。补齐到 limit 为止，
    否则一个上游改了时间格式就会让整页变空（本项目踩过跨格式排序的坑）。
    """
    from src.infrastructure.connectors.intel_sources import sort_key

    cutoff = (datetime.now(timezone.utc).astimezone()
              - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    pool = [x for x in items
            if (x.get("kind") in HOT_KINDS)
            and (x.get("title") or x.get("summary"))]
    pool.sort(key=lambda x: sort_key(x.get("published_at")), reverse=True)
    in_window = [x for x in pool if sort_key(x.get("published_at")) >= cutoff]
    rest = [x for x in pool if sort_key(x.get("published_at")) < cutoff]
    return (in_window + rest)[:limit]


async def run_once(*, gateway: Any, items: list[dict[str, Any]] | None = None,
                   root: Path | None = None,
                   max_notes: int = MAX_NOTES_PER_RUN,
                   batch_size: int = BATCH_SIZE,
                   hours: int = WINDOW_HOURS) -> dict[str, Any]:
    """跑一轮聚合并落库。返回统计。

    `items` 是情报条目（含 `kind` / `title` / `summary` / `published_at`）。
    传 None 时自己去取（走 `build_feed`，与接口看到的是同一批数据）。
    """
    if items is None:
        from src.domain.intel.service import build_feed

        # ⚠️ `group_undetermined=False` **必须传**：收容组是展示层的东西，
        # 而聚合要的是"有哪些真实快讯"。不传的话池子只有 2 条。
        feed = await build_feed(limit=500, group_undetermined=False)
        items = feed.items

    pool = recent_pool(items, hours=hours, limit=max_notes)

    if not pool:
        return {"notes": 0, "batches": 0, "stocks": 0, "topics": 0,
                "rejected": {}, "at": _now()}

    all_stocks: list[HotStock] = []
    all_topics: list[HotTopic] = []
    rejected: dict[str, int] = {}
    batches = 0

    for start in range(0, len(pool), batch_size):
        chunk = pool[start:start + batch_size]
        # 源文本 = 这批材料的全文，用于"名字必须在原文里"的校验
        source_text = "\n".join(
            f"{c.get('title') or ''}\n{c.get('summary') or ''}"
            for c in chunk)
        try:
            resp = await gateway.complete(
                "light", SYSTEM_PROMPT, build_prompt(chunk),
                agent_id="intel_hot", json_mode=True,
                # ⚠️ schema 是**必需**的，不是锦上添花：见 hot_topics.OUTPUT_SCHEMA
                # 的实测记录（1.5B 在 json_mode 下返回语法合法的垃圾 JSON）。
                json_schema=OUTPUT_SCHEMA, max_tokens=1200)
            raw = str(getattr(resp, "content", "") or "")
        except Exception as exc:  # noqa: BLE001 单批失败不拖垮整轮
            logger.warning("热度聚合单批失败：%s", type(exc).__name__)
            rejected["batch_failed"] = rejected.get("batch_failed", 0) + 1
            continue
        batches += 1
        stocks, topics, st = parse_output(raw, source_text)
        all_stocks.extend(stocks)
        all_topics.extend(topics)
        for k, v in st.items():
            rejected[k] = rejected.get(k, 0) + v

    merged = merge_stocks(all_stocks)

    # 去重后的主题：同一标题只留一次（跨批会重复）
    seen_title: set[str] = set()
    topics_out: list[dict[str, Any]] = []
    for t in all_topics:
        if t.title in seen_title:
            continue
        seen_title.add(t.title)
        topics_out.append(t.to_public())
        if len(topics_out) >= MAX_EVENTS:
            break

    data = {
        "at": _now(),
        "notes_used": len(pool),
        "batches": batches,
        "window_hours": hours,
        "kinds": sorted({str(x.get("kind") or "") for x in pool}),
        # ⚠️ 只回带**校验通过**的条目。被拦掉的数量单独给，
        # 让管理员能看出"模型在编"（rejected 明显偏高时该调 prompt）
        "stocks": [s.to_public() for s in merged[:MAX_STOCKS]],
        "topics": topics_out,
        "rejected": rejected,
    }
    save(data, root=root)
    return {"notes": len(pool), "batches": batches,
            "stocks": len(data["stocks"]), "topics": len(data["topics"]),
            "rejected": rejected, "at": data["at"]}


__all__ = ["HOT_KINDS", "MAX_NOTES_PER_RUN", "WINDOW_HOURS", "load",
           "recent_pool", "run_once", "save", "store_path"]
