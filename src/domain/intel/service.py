"""舆情情报聚合服务 —— 把 6 个源合成一份**可出接口**的情报流。

## 六源（全部实测可用，任一挂掉不影响其它）

| 源 | 依赖 | 实测 |
|---|---|---|
| 券商研报 | 无 | 227 行/只，含评级/机构/盈利预测 |
| 东财快讯 | 无 | 200 条，当日 |
| 同花顺快讯 | 无 | 20 条 |
| 新浪快讯 | 无 | 20 条 |
| 新闻联播 | 无 | 15 条全文（政策信号） |
| 知识星球 | token（7–14 天） | 增量水位线，330ms |

## 三条硬约束

1. **来源标识不出接口** —— 只出 `source_alias`（稳定假名），
   真实 group_id / URL / 机构内部标识全部留在内部。
   契约层由 `IntelItem.to_public()` 白名单构造保证。
2. **失败不静默** —— 单源失败进 `gaps`，接口如实返回"哪一类源不可用"，
   **不写成"今天没有内容"**。这是本项目"不猜"口径的延续。
3. **用户侧不显示故障** —— `gaps` 只给管理员语义（"某来源暂无更新"），
   前端不得把内部源名/错误原文展示给普通用户。

## 为什么不做 LLM 分析在这一层

本层只做"聚合 + 归一 + 去重"，**不调模型**。理由：
  · 采集要快（接口 2 秒内出结果），模型分析慢（本地 8B ~30s/批）；
  · 分析放调度任务里做，把结果存起来给接口读 —— 这样接口稳定、
    模型升级/重跑不影响在线延迟。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 单次聚合的显示上限（防一次性把上百条推给前端）
DEFAULT_LIMIT: Final = 60

#: 研报按关注标的逐个拉，限量控频
MAX_BROKER_CODES: Final = 8


@dataclass
class IntelFeed:
    """一次聚合结果。**字段刻意做窄**，不含任何来源标识。"""

    items: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[dict[str, str]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    fetched_at: str = ""
    degraded: bool = False

    def to_public(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "gaps": self.gaps,
            "counts": self.counts,
            "fetched_at": self.fetched_at,
            # 前端据此显示"数据不完整"提示（**不显示具体源名**）
            "degraded": self.degraded,
        }


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


async def build_feed(*, watch_codes: list[str] | None = None,
                     limit: int = DEFAULT_LIMIT,
                     policy_date: str = "") -> IntelFeed:
    """并发聚合六源。**单源失败隔离**，失败进 `gaps`。"""
    from src.infrastructure.connectors.intel_sources import fetch_all

    if not policy_date:
        policy_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    items: list[dict[str, Any]] = []
    gaps: list[dict[str, str]] = []

    # ── 1) 五个无依赖源（并发，内部已隔离失败）──
    try:
        raw_items, failures = await fetch_all(
            watch_codes=(watch_codes or [])[:MAX_BROKER_CODES],
            policy_date=policy_date)
    except Exception as exc:  # noqa: BLE001 整批失败也要给出可读结果
        from src.core.redaction import sanitize_error

        logger.warning("情报源整批聚合失败：%s", sanitize_error(exc))
        raw_items, failures = [], {"all": sanitize_error(exc)}

    for it in raw_items:
        items.append(it.to_public())

    # 失败源 → 缺口条目。**只给"类型"，不给源名与错误原文**：
    # 前端展示的是"某类来源暂无更新"，而非内部故障细节。
    for name, err in failures.items():
        kind = _kind_of(name)
        gaps.append({
            "kind": kind,
            "kind_label": KIND_LABELS.get(kind, kind),
            # message 面向**管理员**；普通用户界面只用 kind_label
            "message": f"{KIND_LABELS.get(kind, kind)}当前不可用",
        })

    # ── 2) 知识星球（增量，独立失败域）──
    try:
        from src.infrastructure.connectors.zsxq_incremental import (
            fetch_incremental, save_watermark,
        )

        inc = await asyncio.to_thread(fetch_incremental)
        for t in inc.topics:
            items.append({
                "kind": "research_note",
                "kind_label": KIND_LABELS["research_note"],
                "title": t.title,
                "summary": t.text,
                "published_at": t.created_at,
                "source_alias": "zsxq",
                "codes": [],
                "industry": "",
                "rating_origin": "",
                "agency": "",
                "content_hash": t.content_hash,
                "extra": {},
            })
        # 只在成功时推进水位线：失败推进会导致**永久丢内容**
        if inc.watermark and inc.new_count:
            await asyncio.to_thread(save_watermark, inc.watermark,
                                    note="intel_feed")
        if inc.truncated:
            gaps.append({
                "kind": "research_note",
                "kind_label": KIND_LABELS["research_note"],
                "message": "研究笔记回填未取完，下次运行将继续追平",
            })
    except Exception as exc:  # noqa: BLE001 含 TokenExpired
        from src.core.redaction import sanitize_error
        from src.infrastructure.connectors.zsxq_source import TokenExpired

        if isinstance(exc, TokenExpired):
            # 授权过期：**用户侧只看到"暂无更新"**；管理员侧另有邮件提醒
            logger.warning("知识星球授权失效，本次跳过该源")
            gaps.append({
                "kind": "research_note",
                "kind_label": KIND_LABELS["research_note"],
                "message": "研究笔记暂无更新",
                "admin_hint": "授权已失效，请运行 scripts/zsxq_authorize.py",
            })
        else:
            logger.warning("知识星球采集失败：%s", sanitize_error(exc))
            gaps.append({
                "kind": "research_note",
                "kind_label": KIND_LABELS["research_note"],
                "message": "研究笔记暂无更新",
            })

    # ── 3) 归一：按时间倒序 + 去重 + 截断 ──
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for it in sorted(items, key=lambda x: str(x.get("published_at") or ""),
                     reverse=True):
        h = str(it.get("content_hash") or "")
        if h and h in seen:
            continue
        if h:
            seen.add(h)
        deduped.append(it)

    counts: dict[str, int] = {}
    for it in deduped:
        k = str(it.get("kind") or "other")
        counts[k] = counts.get(k, 0) + 1

    return IntelFeed(
        items=deduped[:limit],
        gaps=gaps,
        counts=counts,
        fetched_at=_now_iso(),
        degraded=bool(gaps),
    )


#: 情报类型 → 中文标签（前端只认这个，不认内部源名）
KIND_LABELS: Final[dict[str, str]] = {
    "broker_report": "券商研报",
    "newswire": "财经快讯",
    "policy": "政策信号",
    "research_note": "研究笔记",
    "other": "其他",
}


def _kind_of(source_name: str) -> str:
    """内部源名 → 对外类型。**不把源名透出去。**"""
    if source_name.startswith("broker_"):
        return "broker_report"
    if source_name.startswith("newswire_"):
        return "newswire"
    if source_name.startswith("policy_"):
        return "policy"
    if source_name.startswith("zsxq"):
        return "research_note"
    return "other"


__all__ = [
    "DEFAULT_LIMIT",
    "KIND_LABELS",
    "IntelFeed",
    "build_feed",
]
