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
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 单次聚合的显示上限（防一次性把上百条推给前端）
DEFAULT_LIMIT: Final = 60

#: 研报按关注标的逐个拉，限量控频
MAX_BROKER_CODES: Final = 8

#: 自选清单（研报按标的拉，不传标的的话**一条研报都取不到**）
_WATCHLIST_PATH: Final = Path("configs/intraday.yaml")


def watchlist_codes(limit: int = MAX_BROKER_CODES) -> list[str]:
    """从自选清单取关注标的，供研报采集用。

    ## 为什么必须有这个兜底（实测踩到的）

    研报是**按标的**拉的（`watch_codes`），不是"全市场最新研报"那种接口。
    而 `/intel/feed` 的 `codes` 参数**从来没人传** —— 于是默认情况下
    池子里一条研报都没有，连带两个后果：

      · 「仅研报」筛选永远空
      · 「高可信 ≥80」也是空的 —— 因为整个池子的来源档最高只到 74
        （权威媒体档），而研报档是 84

    用户会以为"没有高可信信息""没有研报"，而实际是**根本没去取**。

    自选清单本来就是这个用途（做T面板在用同一份），直接复用；
    将来前端加了"关注标的"输入框，用它覆盖这里即可。
    """
    try:
        import yaml

        raw = yaml.safe_load(_WATCHLIST_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, ImportError):
        return []
    out: list[str] = []
    for row in (raw.get("watchlist") or []):
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        if code and code not in out:
            out.append(code)
        if len(out) >= limit:
            break
    return out


#: 取样池大小。`limit` 是**返回条数**，这是"从多少条里挑"。
#:
#: ⚠️ 必须有这么一个大于 `limit` 的池子，否则筛选项形同虚设：
#: 实测 `limit=60` 时池内最高分只有 78，于是「高可信 ≥80」**永远空**，
#: 而全量里其实有 30 条 80+ 的 —— 用户会以为"没有高可信信息"。
#: 取值也为可分页留余量（将来加"下一页"时不必再改这里）。
FILTER_POOL: Final = 500


@dataclass
class IntelFeed:
    """一次聚合结果。**字段刻意做窄**，不含任何来源标识。"""

    items: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[dict[str, str]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    fetched_at: str = ""
    degraded: bool = False
    #: 可信度分层计数（**筛选之前**、于池子上统计）。
    #:
    #: 单列出来的理由：筛选 tab 的角标必须与当前档位**无关** ——
    #: 拿筛过的 `items` 去数会让"切一次 tab 所有角标都变"，
    #: 用户会以为数据在动。
    credibility_dist: dict[str, int] = field(default_factory=dict)
    #: 相似新闻聚合统计（簇数 / 涉及条数 / 被收起的条数）。
    #:
    #: 单列出来是为了**可观测**：聚合是"悄悄减少条数"的操作，
    #: 没有这个统计就没人能发现阈值配错了（比如把不相关的事合在一起）。
    cluster_stats: dict[str, int] = field(default_factory=dict)
    #: 原文倾向分布 {偏多: n, 偏空: n, 中性: n}。
    #:
    #: ⚠️ **只统计 has_tone 的条目** —— 把未定算进多空比，
    #: 等于替用户做了一个我们并不确定的判断。
    tone_dist: dict[str, int] = field(default_factory=dict)

    def to_public(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "gaps": self.gaps,
            "counts": self.counts,
            "fetched_at": self.fetched_at,
            # 前端据此显示"数据不完整"提示（**不显示具体源名**）
            "degraded": self.degraded,
            "credibility_dist": self.credibility_dist,
            "cluster_stats": self.cluster_stats,
            "tone_dist": self.tone_dist,
        }


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


async def build_feed(*, watch_codes: list[str] | None = None,
                     limit: int = DEFAULT_LIMIT,
                     policy_date: str = "",
                     sort: str = "credibility",
                     filter: str = "all") -> IntelFeed:
    """并发聚合六源。**单源失败隔离**，失败进 `gaps`。

    ## `limit` 是**返回条数**，不是取样池大小

    ⚠️ 这是个容易搞错的地方，实测踩过：函数内部会先取 `FILTER_POOL`
    条作为取样池，**筛完**再截到 `limit`。第一版拿 `limit` 当池子，
    于是 `limit=60` 时整个池子最多 60 条 —— 而实测那 60 条里
    分数最高的只有 78 分，于是「高可信 ≥80」**永远是空列表**，
    而全量里其实有 30 条 80+ 的。用户会以为"没有高可信信息"。

    `sort` / `filter` 见 `_balanced_take` 与 `matches_filter`。
    """
    from src.infrastructure.connectors.intel_sources import fetch_all

    if not policy_date:
        policy_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    # ★ 研报按**标的**拉：不传标的就一条都没有，连带「仅研报」与
    #   「高可信 ≥80」两个筛选项**永远是空的**（实测：池内来源档最高
    #   只到 74 的权威媒体档，而研报档是 84）。用户会以为"没有研报/
    #   没有高可信信息"，实际是**根本没去取**。
    #   所以没有显式关注标的时，用自选清单兜底。
    if not watch_codes:
        watch_codes = watchlist_codes()

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
                # 真实来源名（**内部字段，不出接口**）：分级表靠中文属性词
                # 识别档次，写英文标识（`zsxq`）会落进保守档 38。
                source_name="知识星球-调研纪要",
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

    from src.domain.intel.credibility import level_of

    taken = _balanced_take(deduped, FILTER_POOL, sort=sort)

    # ── 可信度分层计数：在**筛选之前**、于池子上统计 ──
    #
    # 顺序很重要：先统计再筛选，这样角标与当前档位无关。
    # 反过来（先筛后统计）会让"切一次 tab 所有角标都变"。
    dist: dict[str, int] = {}
    for it in taken:
        raw = it.get("credibility") or {}
        try:
            lv = level_of(int(raw.get("score")))[0]
        except (TypeError, ValueError):
            lv = "doubt"
        dist[lv] = dist.get(lv, 0) + 1

    # ── 相似新闻聚合（**在池子上做**，不是在 limit 之后）──
    #
    # ⚠️ 顺序很重要：聚类要在**截断之前**、于整个池子上做。
    # 第一版放在返回前、只对 `limit` 条做，结果 301 条里只合出
    # 11 簇 / 22 条 —— 因为同一件事的各个转载分散在池子的不同位置，
    # 只比最后 60 条自然匹配不上几对。
    #
    # 聚合之后**把非代表条收起**（用户口径："相似相同观点的可以聚合成
    # 一条的，把信息源在关联数字里"）—— 所以它同时是**去噪**：
    # 同一件事在情报流里只占一行，而不是刷五遍。
    from src.domain.intel.related import attach

    cluster_stats = attach(taken)

    # 用聚类算出的**独立佐证数**重算可信度。
    #
    # 这是设计稿公式里 `+ 独立佐证数 × 4` 那一项，也是"多来源印证"
    # 唯一诚实的算法：同一份信息被转发 3 次**不算** 3 个独立来源
    # （设计稿 §5.4 明确警告过"共振虚高"）。
    # ⚠️ 必须**封顶**：低权威来源不能靠"被转得多"刷分 ——
    # 来源分即上限这条性质不能被佐证项破坏。
    from src.domain.intel.credibility import apply_corroboration

    for it in taken:
        cred = it.get("credibility")
        if cred:
            it["credibility"] = apply_corroboration(
                cred, it.get("corroboration"))

    # 收起非代表条（同一簇只留时间最新的那条）
    taken = [it for it in taken if it.get("is_cluster_lead", True)]

    # ── 原文倾向：**只读存储**，接口里不调模型 ──
    #
    # 本地模型单条实测 ~770ms，一页 60 条现算就是 +46 秒 ——
    # 接口会从"秒回"退化成"超时"。所以抽取走定时任务
    # （`tone_job.run_once`，2 小时一次）落进 `tone_store`，
    # 这里只按 `content_hash` 查，O(1)。
    #
    # 查不到就**不给倾向字段**（而不是编一个）—— 前端会说"尚未抽取"。
    # 规则层能定的那些由 `tone.rule_tone` 在抽取时一并处理，
    # 所以"没抽过"与"抽过但未定"在存储里是两种状态。
    from src.domain.intel import tone_store

    for it in taken:
        hit = tone_store.get(str(it.get("content_hash") or ""))
        if hit:
            it["tone"] = {
                "tone": hit.get("tone"),
                "has_tone": bool(hit.get("has_tone")),
                "phrases": list(hit.get("phrases") or []),
                "codes": list(hit.get("codes") or []),
                "confidence": hit.get("confidence"),
                "source": hit.get("source"),
                "explain": hit.get("explain"),
            }

    # 倾向分布（热度页的多空比用它）。**只统计 `has_tone` 的条目** ——
    # 把"未定"算进多空比等于替用户做了一个我们并不确定的判断。
    tone_dist: dict[str, int] = {}
    for it in taken:
        t = it.get("tone") or {}
        if t.get("has_tone"):
            k = str(t.get("tone"))
            tone_dist[k] = tone_dist.get(k, 0) + 1

    # ── 可信度筛选（在**池子**上做，不是在 limit 之后做）──
    taken = [it for it in taken if _passes_filter(it, filter)]
    taken = taken[:max(1, limit)]
    # ★ 取样后**按时间倒序返回**。
    #
    # 取样用可信度决定"谁能进这一页"，但**展示顺序仍是时间** ——
    # 因为界面要回答的是"最近发生了什么，其中哪些更可核实"。
    # 完全按可信度排会让一条三天前的官方公告压在今天所有快讯之上，
    # 那是另一种误导（用户会以为它刚发生）。
    # 可信度的作用体现在**筛选 tab** 与每条旁边的分数环上。
    taken.sort(key=lambda x: sort_key(x.get("published_at")), reverse=True)

    return IntelFeed(
        items=taken,
        gaps=gaps,
        counts=counts,
        fetched_at=_now_iso(),
        degraded=bool(gaps),
        credibility_dist=dist,
        cluster_stats=cluster_stats,
        tone_dist=tone_dist,
    )


def _passes_filter(item: dict[str, Any], key: str) -> bool:
    """一条情报是否落在某个可信度筛选档里。

    判据复用 `credibility.matches_filter` —— **前后端两套口径漂移**
    是这类筛选最容易出的问题（界面说 80 分算高可信、服务端按 85 筛，
    用户看到"高可信"里有 82 分的、却没有 84 分的）。
    """
    if key in ("", "all"):
        return True
    from src.domain.intel.credibility import Credibility, matches_filter

    raw = item.get("credibility") or {}
    try:
        score = int(raw.get("score"))
    except (TypeError, ValueError):
        return False      # 缺分数的**不进任何档**（不假装它有分）
    cred = Credibility(
        score=score,
        source_base=int(raw.get("source_base") or 0),
        content_base=int(raw.get("content_base") or 0),
        source_reason=str(raw.get("source_reason") or ""),
        content_reason=str(raw.get("content_reason") or ""))
    return matches_filter(cred, str(item.get("kind") or ""), key)


def _balanced_take(items: list[dict[str, Any]], limit: int,
                   sort: str = "credibility") -> list[dict[str, Any]]:
    """取前 N 条：**类型之间轮流，类型内部按 `sort` 决定先后**。

    ## 为什么不能只按时间截断

    纯 `sorted(...)[:limit]` 在真实数据上会**整类消失**。实测：单次聚合
    156 条里 `newswire` 占 140 条，若按时间取前 60，结果几乎是清一色快讯
    —— `broker_report`（券商研报）与 `research_note`（研究笔记）**一条都
    出不来**。而这两类恰恰是用户最想看的，快讯反而是廉价那类。

    原因是数量级差异：日报类快讯一天上百条，研报一天几条。时间序截断
    等价于"按产量分配版面"，产量高的必然挤掉产量低的。

    ## 为什么轮流取样是**全局**的（不在天内做）

    试过"先按天分桶、天内再轮流"，结果**更糟**。原因是实测数据长这样：

        2026-09-25   newswire ×140
        2026-09-24   policy   ×15
        2026-09-22   research_note ×30

    **每一天只有一种类型**（快讯天天有、政策按日发、笔记断续来）。
    于是"按天取"必然退化回单类型 —— `limit=60`（默认值）时返回
    60 条清一色快讯，政策和笔记**一条都没有**，多样性完全失效。

    所以在**这份数据**上，"天分组 / 类型多样 / 严格时间序"三者
    不可能同时满足。取舍是：

      · 服务端保证 **类型多样 + 组内时间序**（这个函数）
      · 界面**不做天分组标题**，改为每行显示日期
        （见 `IntelFeedTab.tsx` 的说明）—— 时序信息由每行承载，
        不靠分组标题，于是也就不存在"同一天被切碎"的问题

    ## `sort` 只影响**谁能进这一页**，不影响展示顺序

    `credibility` 模式下桶内按可信度降序，于是稀缺的高可信条目
    （交易所公告、持牌研报）优先入选。但返回后 `build_feed` 仍按时间重排
    —— 展示顺序必须是时间，否则会误导（见 `build_feed` 的说明）。
    """
    if limit <= 0:
        return []
    from src.infrastructure.connectors.intel_sources import sort_key

    def _rank(it: dict[str, Any]) -> tuple:
        """桶内排序键。**时间永远做兜底**，保证同分时顺序稳定可预测。"""
        t = sort_key(it.get("published_at"))
        if sort == "time":
            return (t,)
        cred = it.get("credibility") or {}
        try:
            score = int(cred.get("score"))
        except (TypeError, ValueError):
            score = -1        # 缺分数的排最后（不假装它有分）
        return (score, t)

    buckets: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        buckets.setdefault(str(it.get("kind") or "other"), []).append(it)
    # 桶内重排：`items` 整体按时间排过，但切桶后必须再排一次 ——
    # 一是各来源时间戳格式不同、不重排会随到达顺序漂移；
    # 二是 `sort=credibility` 时这里才是"谁先入选"的真正决定处。
    for k in buckets:
        buckets[k].sort(key=_rank, reverse=True)
    # 类型顺序：谁有最新一条谁先露头（不写死字典序，否则某个类型永远第一）
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
        if not progressed:      # 全部桶取空
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
