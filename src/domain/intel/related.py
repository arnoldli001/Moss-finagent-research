"""相似新闻聚合（多来源同一件事 → 一个聚类）。

对应设计：`docs/INTEL_CENTER_REDESIGN.md` §5.4「防重复计数」。

## 这个模块要解决的问题

同一条消息会被多个渠道转载、改写、加前缀。用户在情报流里看到的因此是
**五条看起来不同的条目**，而它们说的是同一件事。两个后果：

  1. **读起来累**：同一件事刷五遍，真正的新信息被稀释。
  2. **可信度虚高**（更严重）：设计稿的公式里有 `独立佐证数 × 4` 这一项，
     而"独立"必须靠聚类判定。不聚类就等于把"同一份信息被转发 3 次"
     记成 3 个独立来源 —— 那正是设计稿自己警告过的
     "同一份信息被当成三份独立证据，回测胜率必然虚高"。

## 为什么用字符 n-gram 而不是分词

中文分词要引入词典（jieba 之类），多一个依赖、多一处版本差异，
而且**同一件事的不同转载会带来不同的分词结果** —— 反而更难匹配。
字符 n-gram（默认 3-gram）不需要词典，对"同文转载"这种
**字面高度重合**的场景命中率很高。

## ⚠️ 阈值刻意保守：**误合比漏合严重得多**

  · 漏合 → 用户多看一条重复内容（体验损失，可接受）
  · 误合 → 两件**不同的事**被当成互相佐证，可信度被抬高
    （用户在错误的信息上建立判断，且看不出来）

所以 `JACCARD_MIN` 取 0.5 这个偏高的值，宁可少合。

## 第一步的限制（如实说明）

  · 只在**同一时间窗口**内比（默认 72 小时）—— 跨周的相似标题
    多半是"同类事件"而不是"同一事件"（例如每周都发的行业数据）
  · 只在**标题**上比，不比正文 —— 正文被各渠道改写得多，
    拿它比会显著增加误合
  · 这是**规则层**，不调模型，所以没有幻觉风险，结果可复算
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final

#: 参与比较的最小标题长度（字符）。
#:
#: 太短的标题（"市场快讯""盘中异动"）字面重合度高但**不是同一件事** ——
#: 那是误合的最大来源。实测快讯里有不少 6~8 字的通稿式短标题。
MIN_TITLE_CHARS: Final = 8

#: n-gram 的 n
SHINGLE_N: Final = 3

#: Jaccard 相似度阈值（**保守**：误合会抬高可信度，比漏合严重）
JACCARD_MIN: Final = 0.5

#: 时间窗口（小时）。超出这个跨度的相似标题视为"同类事件"而非"同一事件"
WINDOW_HOURS: Final = 72

#: 相关性上限（性能护栏）：只对**最新**这么多条做聚类
MAX_COMPARE: Final = 400

#: 单个聚类最多回带几条"其他来源"（多了没用，用户不会看第 6 条）
MAX_RELATED: Final = 4

#: 参与聚类的噪声字符：全角/半角标点、空白、常见栏目前缀
_PUNCT_RE: Final = re.compile(
    r"[\s\u3000·、，。；：？！“”‘’（）()\[\]【】《》<>\"'`~!@#$%^&*_+=|\\/—\-…]+")
#: 通稿式前后缀（各渠道转载时加的栏目名，与事件本身无关）
_PREFIX_RE: Final = re.compile(
    r"^(?:【[^】]{0,12}】|\[[^\]]{0,12}\]|独家|重磅|快讯|要闻|午间|早间|盘中)+")

#: 时间戳归一化（与 `intel_sources.sort_key` 同口径，但这里只取小时精度）
_TS_RE: Final = re.compile(
    r"^(\d{4})-?(\d{2})-?(\d{2})(?:[T ](\d{2}))?")


def _normalise(title: str) -> str:
    """标题归一化：去标点、去栏目前缀、去空白，只留字符本体。"""
    s = _PREFIX_RE.sub("", (title or "").strip())
    s = _PUNCT_RE.sub("", s)
    return s


def _shingles(norm: str) -> frozenset[str]:
    """字符 n-gram 集合。标题太短时返回空集（不参与聚类）。"""
    if len(norm) < MIN_TITLE_CHARS:
        return frozenset()
    n = SHINGLE_N
    return frozenset(norm[i:i + n] for i in range(len(norm) - n + 1))


def _hour_key(ts: str) -> int:
    """`published_at` → 粗粒度小时数（用于时间窗口比较）。

    解析不出来返回 `-1`：调用方据此**不参与**聚类（不猜）。
    """
    m = _TS_RE.match((ts or "").strip())
    if not m:
        return -1
    y, mo, d, hh = m.groups()
    try:
        # 只做**相对**比较，不需要真实历法 —— 按 31 天/月近似即可，
        # 因为窗口只有 72 小时，近似误差远小于窗口本身。
        return ((int(y) * 372 + int(mo) * 31 + int(d)) * 24
                + int(hh or 0))
    except ValueError:
        return -1


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


@dataclass
class Cluster:
    """一个相似新闻聚类。"""

    #: 代表条目（时间最新的那条）在传入列表里的下标
    lead: int
    #: 全部成员下标（含 lead），按传入顺序
    members: list[int] = field(default_factory=list)
    #: 聚类内**不同来源**的个数（按 `source_alias` 去重）
    source_count: int = 0

    @property
    def corroboration(self) -> int:
        """独立佐证数 = 不同来源数 − 1（自己不算自己的佐证）。"""
        return max(0, self.source_count - 1)


def cluster(items: list[dict[str, Any]], *,
            max_compare: int = MAX_COMPARE,
            jaccard_min: float = JACCARD_MIN,
            window_hours: int = WINDOW_HOURS) -> list[Cluster]:
    """把"同一件事的多来源报道"聚成若干簇。

    ## 算法：倒排索引预筛 + 并查集成簇

    朴素两两比较是 O(n²)：500 条要 12.5 万次集合运算。这里先用
    **n-gram 倒排索引**预筛候选对（只有共享至少一个 3-gram 的才可能相似），
    再用并查集连成簇。实测 400 条的候选对从 ~8 万降到几百。

    ## 为什么用并查集而不是"逐条找最像的"

    三个来源各自改写同一个事件，可能是 A≈B、B≈C 但 A 与 C 的相似度
    低于阈值。逐条归并会漏掉（A 单独成簇），并查集能把它们连成一簇 ——
    这正是"多来源同一件事"的常见形态（转载链）。

    ## `source_alias` 的作用

    同一来源自己发两条相似标题（系列报道）**不算佐证** ——
    所以 `source_count` 按 `source_alias` 去重。
    """
    n = min(len(items), max(0, max_compare))
    if n < 2:
        return []

    norms: list[str] = [""] * n
    shing: list[frozenset[str]] = [frozenset()] * n
    hours: list[int] = [-1] * n
    #: n-gram → 条目下标（只在**参与比较**的那些条目上建索引）
    index: dict[str, list[int]] = {}
    for i in range(n):
        it = items[i]
        norm = _normalise(str(it.get("title") or ""))
        norms[i] = norm
        s = _shingles(norm)
        shing[i] = s
        hours[i] = _hour_key(str(it.get("published_at") or ""))
        # 时间解析不出来的**不参与**聚类（不猜）
        if not s or hours[i] < 0:
            continue
        for g in s:
            index.setdefault(g, []).append(i)

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    # 候选对：共享至少一个 3-gram
    seen_pairs: set[tuple[int, int]] = set()
    for ids in index.values():
        if len(ids) < 2:
            continue
        # 同一 gram 命中的条目两两成对。倒排桶可能很大（常见 gram），
        # 所以对桶大小设上限：桶过大说明这个 gram 没有区分度，
        # 用它预筛只会带来 O(k²) 的噪声对。
        if len(ids) > 64:
            continue
        for a_idx in range(len(ids)):
            for b_idx in range(a_idx + 1, len(ids)):
                i, j = ids[a_idx], ids[b_idx]
                if i == j:
                    continue
                key = (i, j) if i < j else (j, i)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                # 时间窗口
                if abs(hours[i] - hours[j]) > window_hours:
                    continue
                if _jaccard(shing[i], shing[j]) >= jaccard_min:
                    union(i, j)

    # 收集成簇（保序：按代表条目在输入里的次序）
    groups: dict[int, list[int]] = {}
    for i in range(n):
        if not shing[i] or hours[i] < 0:
            continue
        groups.setdefault(find(i), []).append(i)

    out: list[Cluster] = []
    for root in sorted(groups):
        members = groups[root]
        if len(members) < 2:
            continue          # 单条不成簇（没有"相关新闻"可给）
        aliases = {str(items[m].get("source_alias") or "") for m in members}
        aliases.discard("")
        out.append(Cluster(
            lead=members[0],
            members=members,
            # 来源假名为空时按"每条各算一个来源"（保守：宁可少算佐证）
            source_count=len(aliases) or len(members),
        ))
    return out


def attach(items: list[dict[str, Any]], *,
           jaccard_min: float = JACCARD_MIN,
           window_hours: int = WINDOW_HOURS) -> dict[str, Any]:
    """就地给条目挂上"关联新闻"，**并标出哪些该收起**。返回统计。

    ## 折叠而非并列（用户口径 2026-09-25）

    "相似相同观点的可以聚合成一条的，把信息源在关联数字里，做好记录即可，
    点开数字可以展开看。"

    所以每条只保留**一条代表**（时间最新的那条），其余成员**不再单独占位**
    —— 它们的标题收进代表条的 `related` 里，靠 `related_count` 那个数字
    点开才看得见。这样同一件事在情报流里只占一行。

    ## 代表条得到什么

        related_count     相关新闻条数（**含未进入当前页的成员**）
        related           相关新闻（最多 `MAX_RELATED` 条，只带展示需要的字段）
        cluster_id        簇标识
        corroboration     独立佐证数 = 不同来源数 − 1（自己不算自己的佐证）
        is_cluster_lead   true = 它是这一簇的代表

    非代表条也拿到 `related_count`/`cluster_id`（万一它出现在别处），
    但没有 `is_cluster_lead` —— 调用方据此过滤。

    ⚠️ `related` 里**不含** `source_alias`：它是假名，不上屏。
    只带 `title` / `kind_label` / `published_at` / `credibility_score` ——
    够用户判断"这几条是不是同一件事、分别多可核实"。
    """
    clusters = cluster(items, jaccard_min=jaccard_min,
                       window_hours=window_hours)
    stats = {"clusters": 0, "clustered_items": 0, "folded": 0, "pairs": 0}

    def brief(idx: int) -> dict[str, Any]:
        it = items[idx]
        cred = it.get("credibility") or {}
        return {
            "title": str(it.get("title") or ""),
            "kind_label": str(it.get("kind_label") or ""),
            "published_at": str(it.get("published_at") or ""),
            "credibility_score": cred.get("score"),
        }

    for ci, cl in enumerate(clusters):
        stats["clusters"] += 1
        stats["clustered_items"] += len(cl.members)
        stats["pairs"] += len(cl.members) * (len(cl.members) - 1) // 2
        cid = f"c{ci}"
        for m in cl.members:
            others = [x for x in cl.members if x != m]
            items[m]["cluster_id"] = cid
            items[m]["related_count"] = len(others)
            items[m]["related"] = [brief(x) for x in others[:MAX_RELATED]]
            # 独立佐证数 = **不同来源数 − 1**（自己不算自己的佐证）
            items[m]["corroboration"] = cl.corroboration
        # 代表 = 成员里时间最新的那条（`members` 已按输入顺序，而输入是
        # 时间倒序的，所以第一条就是最新）。其余标记为"可收起"。
        lead = cl.members[0]
        items[lead]["is_cluster_lead"] = True
        for m in cl.members[1:]:
            items[m]["is_cluster_lead"] = False
            stats["folded"] += 1
    return stats


__all__ = [
    "JACCARD_MIN",
    "MAX_RELATED",
    "MIN_TITLE_CHARS",
    "Cluster",
    "attach",
    "cluster",
]
