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
        from src.infrastructure.connectors.intel_sources import IntelItem
        from src.infrastructure.connectors.zsxq_incremental import (
            fetch_incremental, save_watermark,
        )

        inc = await asyncio.to_thread(fetch_incremental)
        for t in inc.topics:
            # ⚠️ 必须走 `IntelItem.to_public()`，不能在这里手写 dict。
            #
            # 手写 dict 会**绕过脱敏与截断**（那两道闸都长在 `to_public` 上）：
            #   · `source_alias` 直接写明文 `"zsxq"` → 数据源泄漏
            #   · `summary` 不截断 → 实测最长 3400+ 字，移动端一条占满十屏
            # 首版就是这样：五个内置源的 `to_public` 修好了，这条旁路没有。
            # 统一从同一条出口走，将来加字段也只需要改一处。
            items.append(IntelItem(
                kind="research_note",
                title=t.title,
                summary=t.text,
                published_at=t.created_at,
                source_alias="research-note-zsxq",
                content_hash=t.content_hash,
            ).to_public())
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
    #
    # ⚠️ 排序键必须过 `sort_key()` 归一化，不能直接比 `published_at` 字符串。
    # 三种时间戳格式（`20260924` / `2026-09-25 04:26:03` /
    # `2026-09-25T13:15:41.340+0800`）直接按字符串排会出错：
    # `-`(0x2D) < `0`(0x30)，于是 `'2026-09-…'` 排在 `'20260924'` **之前**，
    # 界面按天分组后出现 `今天 / 09-24 / 今天 / 09-24` 来回跳。
    from src.infrastructure.connectors.intel_sources import sort_key

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for it in sorted(items, key=lambda x: sort_key(x.get("published_at")),
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
        items=_balanced_take(deduped, limit),
        gaps=gaps,
        counts=counts,
        fetched_at=_now_iso(),
        degraded=bool(gaps),
    )


def _balanced_take(items: list[dict[str, Any]], limit: int
                   ) -> list[dict[str, Any]]:
    """按类型**轮流取样**，而不是纯时间取前 N 条。

    ## 为什么不能只按时间截断

    纯 `sorted(...)[:limit]` 在真实数据上会**整类消失**。实测：单次聚合
    156 条里 `newswire` 占 140 条，若按时间取前 60，结果几乎是清一色快讯
    —— `broker_report`（券商研报）与 `research_note`（研究笔记）**一条都
    出不来**。而这两类恰恰是用户最想看的，快讯反而是廉价那类。

    原因是数量级差异：日报类快讯一天上百条，研报一天几条。时间序截断
    等价于"按产量分配版面"，产量高的必然挤掉产量低的。

    ## 做法

    各类型内部**保持时间倒序**（同类里新的在前），类型之间轮流各取一条。
    这样：稀缺类型（研报/笔记）一定能露面，充裕类型（快讯）也不会被饿死。
    """
    if limit <= 0:
        return []
    from src.infrastructure.connectors.intel_sources import sort_key

    buckets: dict[str, list[dict[str, Any]]] = {}
    for it in items:            # items 已按时间倒序，分桶后桶内仍有序
        buckets.setdefault(str(it.get("kind") or "other"), []).append(it)
    # 类型顺序按「各自最新一条的时间」定 —— 谁有最新消息谁先露头，
    # 而不是固定字典序（那会让某个类型永远排第一）。
    # 同样要过 `sort_key()`：三种时间戳格式直接比字符串会排错。
    order = sorted(
        buckets,
        key=lambda k: sort_key(buckets[k][0].get("published_at")),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    idx = 0
    while len(out) < limit:
        progressed = False
        for k in order:
            bucket = buckets[k]
            if idx < len(bucket):
                out.append(bucket[idx])
                progressed = True
                if len(out) >= limit:
                    break
        if not progressed:      # 全部桶都取空了
            break
        idx += 1
    return out


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
