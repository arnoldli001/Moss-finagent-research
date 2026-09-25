"""相似新闻聚合的回归测试。

## 为什么这些断言值得写

聚合做两件有副作用的事，两件都需要被锁住：

  1. **悄悄减少条数**（把同一件事的多个转载收起成一条）——
     阈值配错就会把**不相关**的事合在一起，用户看到"关联 3 条"
     点开却是三件不同的事。
  2. **抬高可信度**（`+ 独立佐证数 × 4`）—— 这是设计稿公式里的第三项，
     也是"多来源印证"唯一诚实的算法。但它一旦不封顶，
     一条低权威内容靠"被转载很多次"就能刷到 100 分，
     那恰好破坏了"来源分即上限"这条最重要的性质。

**误合比漏合严重得多**：漏合只是用户多看一条重复内容；
误合是两件不同的事被当成互相佐证，用户在错误的信息上建立判断，
而且从界面上看不出来。所以阈值测试全部偏向"宁可不合"。
"""

from __future__ import annotations

import pytest

from src.domain.intel.credibility import apply_corroboration
from src.domain.intel.related import (
    JACCARD_MIN,
    MAX_RELATED,
    MIN_TITLE_CHARS,
    attach,
    cluster,
)


def _item(title: str, *, ts: str = "2026-09-25 10:00:00",
          alias: str = "src-a", score: int = 54) -> dict:
    return {
        "title": title,
        "kind_label": "财经快讯",
        "published_at": ts,
        "source_alias": alias,
        "credibility": {"score": score, "source_base": 54,
                        "content_base": 78},
    }


# ======================================================================
# 能合：同一件事的不同转载
# ======================================================================

def test_same_story_from_two_sources_is_clustered() -> None:
    """同一件事的两个转载必须合成一簇（实测的真实样本）。"""
    items = [
        _item("今夏热浪预计致德国损失约250亿欧元", alias="src-a"),
        _item("报告：今夏热浪预计致德国损失约250亿欧元", alias="src-b"),
    ]
    cl = cluster(items)
    assert len(cl) == 1, "同一件事没合成一簇"
    assert cl[0].source_count == 2
    assert cl[0].corroboration == 1


def test_same_story_with_punctuation_difference_is_clustered() -> None:
    """标点差异不该影响判定（实测样本）。"""
    items = [
        _item("港股生物医药概念股走高 金斯瑞生物科技涨超9%", alias="src-a"),
        _item("港股生物医药概念走高，金斯瑞生物科技涨超9%", alias="src-b"),
    ]
    assert len(cluster(items)) == 1


def test_leading_section_tag_is_ignored() -> None:
    """转载时加的栏目前缀（【】、快讯、独家…）不该影响判定。"""
    items = [
        _item("央行开展3000亿元中期借贷便利操作", alias="src-a"),
        _item("【快讯】央行开展3000亿元中期借贷便利操作", alias="src-b"),
    ]
    assert len(cluster(items)) == 1


def test_transitive_chain_forms_one_cluster() -> None:
    """A≈B、B≈C 但 A 与 C 低于阈值时，仍应连成**一簇**。

    这是"转载链"的常见形态：逐条找最像的会漏掉（A 单独成簇），
    并查集能把它们连起来。
    """
    base = "半导体设备国产化率加速提升订单能见度延续至明年"
    items = [
        _item(base, alias="src-a"),
        _item(base + "设备与材料端受益", alias="src-b"),
        _item(base + "设备与材料端受益明显", alias="src-c"),
    ]
    cl = cluster(items)
    assert len(cl) == 1, f"转载链没连成一簇：{[c.members for c in cl]}"
    assert cl[0].source_count == 3


# ======================================================================
# 不能合：误合会伪造可信度
# ======================================================================

def test_different_events_are_not_clustered() -> None:
    """**不同的事必须分开** —— 误合会让两条互相"佐证"。"""
    items = [
        _item("今夏热浪预计致德国损失约250亿欧元", alias="src-a"),
        _item("港股生物医药概念股走高 金斯瑞生物科技涨超9%", alias="src-b"),
        _item("日均240万人次 中秋国庆假期民航旅客量将创同期新高", alias="src-c"),
    ]
    assert cluster(items) == [], "不相关的事被合到一起了"


def test_short_titles_never_cluster() -> None:
    """过短的标题（通稿式栏目名）**不参与**聚类。

    "市场快讯""盘中异动"这类标题字面重合度高但不是同一件事 ——
    实测快讯里有不少 6~8 字的通稿式短标题，是误合的最大来源。
    """
    items = [
        _item("盘中异动", alias="src-a"),
        _item("盘中异动", alias="src-b"),
        _item("市场快讯", alias="src-c"),
    ]
    assert cluster(items) == [], "短标题被误合了"
    assert MIN_TITLE_CHARS >= 8, "阈值被调低了，误合风险上升"


def test_different_events_outside_time_window_are_not_clustered() -> None:
    """跨周的相似标题多半是"同类事件"（例如每周都发的行业数据），
    不是"同一事件" —— 超出时间窗口不参与聚类。"""
    same = "某行业周度数据：库存环比下降2.1%"
    items = [
        _item(same, ts="2026-09-25 10:00:00", alias="src-a"),
        _item(same, ts="2026-09-05 10:00:00", alias="src-b"),
    ]
    assert cluster(items) == [], "跨窗口的同名标题被合了"


def test_min_threshold_is_conservative() -> None:
    """阈值必须偏保守 —— 误合的代价远大于漏合。"""
    assert JACCARD_MIN >= 0.4, (
        f"JACCARD_MIN={JACCARD_MIN} 偏低：误合会把不同的事当成互相佐证")


# ======================================================================
# 来源计数：同一来源的系列报道不算佐证
# ======================================================================

def test_same_source_twice_is_not_corroboration() -> None:
    """同一来源自己发两条相似标题（系列报道）**不算**独立佐证。

    这正是设计稿 §5.4 警告的"同一份信息被当成多份独立证据"。
    """
    items = [
        _item("今夏热浪预计致德国损失约250亿欧元", alias="src-same"),
        _item("报告：今夏热浪预计致德国损失约250亿欧元", alias="src-same"),
    ]
    cl = cluster(items)
    assert len(cl) == 1
    assert cl[0].source_count == 1, "同一来源被算成两个来源"
    assert cl[0].corroboration == 0, "同一来源产生了佐证加分"


# ======================================================================
# attach：折叠 + 关联明细
# ======================================================================

def test_attach_folds_non_leads_and_exposes_related() -> None:
    """`attach` 要标出代表条、把其余标成"可收起"，并给出关联明细。"""
    items = [
        _item("今夏热浪预计致德国损失约250亿欧元", alias="src-a", score=58),
        _item("报告：今夏热浪预计致德国损失约250亿欧元", alias="src-b",
              score=54),
    ]
    stats = attach(items)
    assert stats["clusters"] == 1
    assert stats["folded"] == 1, "非代表条没被标成可收起"

    lead, follower = items[0], items[1]
    assert lead["is_cluster_lead"] is True
    assert follower["is_cluster_lead"] is False
    assert lead["related_count"] == 1
    assert follower["related_count"] == 1
    assert len(lead["related"]) == 1
    assert lead["related"][0]["title"].startswith("报告：")
    assert lead["corroboration"] == 1


def test_related_brief_carries_no_source_identifier() -> None:
    """`related` **明细里**不含来源标识（假名也不给）—— 数据源保密。

    ⚠️ 只查 `related` 本身，不查整个条目：`source_alias` 在条目上是
    **契约里本来就有的字段**（前端用它做同源判断、不上屏），
    把它一起判成泄漏会让这条测试失去意义。
    要锁的是"展开那一块**没有**它"。
    """
    import json

    items = [
        _item("今夏热浪预计致德国损失约250亿欧元", alias="src-secret-a"),
        _item("报告：今夏热浪预计致德国损失约250亿欧元", alias="src-secret-b"),
    ]
    attach(items)
    for it in items:
        blob = json.dumps(it["related"], ensure_ascii=False)
        for leak in ("source_alias", "src-secret", "source_name", "cluster"):
            assert leak not in blob, f"关联明细泄漏 {leak!r}：{blob}"


def test_related_is_capped() -> None:
    """关联明细有条数上限（用户不会看第 6 条，界面也不该长成那样）。"""
    base = "半导体设备国产化率加速提升订单能见度延续至明年"
    items = [_item(base + "x" * i, ts=f"2026-09-25 1{i}:00:00",
                   alias=f"src-{i}") for i in range(8)]
    attach(items)
    for it in items:
        assert len(it["related"]) <= MAX_RELATED, "关联明细没有上限"


# ======================================================================
# 佐证加分：必须封顶
# ======================================================================

@pytest.mark.parametrize("base,corr,expect", [
    (54, 0, 54),      # 没有佐证不加分
    (54, 1, 58),      # +4
    (54, 3, 66),      # +12
    (54, 5, 74),      # +20（封顶）
    (54, 20, 74),     # 再多也不加 —— ★ 封顶是关键
    (90, 5, 100),     # 不越过 100
])
def test_corroboration_bonus_is_capped(base: int, corr: int,
                                       expect: int) -> None:
    """`+ 佐证 × 4`，**封顶 +20**。

    不封顶的后果：一条 54 分的自媒体内容被 20 个渠道转载就能拿到
    54 + 80 = 134 → 100 分，比交易所公告还高。而"被转得多"与
    "更可核实"根本不是一回事 —— 假消息往往传得最快。
    """
    pub = {"score": base, "tone_allowed": base >= 50,
           "explain": "x", "source_base": 54, "content_base": 78}
    out = apply_corroboration(pub, corr)
    assert out["score"] == expect, f"({base},{corr}) → {out['score']}"
    assert out["score"] <= 100


def test_corroboration_updates_tone_flag() -> None:
    """加分后要**重算** `tone_allowed` —— 否则界面说明与判据不一致。"""
    pub = {"score": 46, "tone_allowed": False, "explain": "x"}
    out = apply_corroboration(pub, 3)      # 46 + 12 = 58
    assert out["score"] == 58
    assert out["tone_allowed"] is True, "加分后没重算倾向分析开关"
    # 反向：没跨过阈值时保持 False
    out2 = apply_corroboration({"score": 30, "tone_allowed": False,
                                "explain": "x"}, 1)
    assert out2["score"] == 34
    assert out2["tone_allowed"] is False


def test_corroboration_handles_missing_value() -> None:
    """佐证缺失（`None`）时**不动分数**，并把它写成 0 —— 不能崩。"""
    pub = {"score": 54, "tone_allowed": True, "explain": "x"}
    out = apply_corroboration(pub, None)
    assert out["score"] == 54
    assert out["corroboration"] == 0
