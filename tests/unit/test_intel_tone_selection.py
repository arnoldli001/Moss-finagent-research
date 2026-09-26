"""抽取任务的**候选选择**：类型之间轮流，别让快讯把券商作文饿死。

## 为什么必须有一条这样的用例（2026-09-26 实测）

待抽取队列当时构成：`newswire` 168 条、`research_note` **24 条**，单轮上限 40。
原实现是"按发布时间倒序取前 N" —— 快讯一分钟来一条，于是那 40 个额度
**永远被快讯占满**，24 条券商作文一条都排不进去。

表现（全部是"看得出来但不会报错"的形态）：

  · 摘要永远停在"原文截断"（`summary_text.kind = "excerpt"`），
    用户要的"本地模型压出的一句话摘要"从来没出现过；
  · `events` / `bullish` / `bearish` 全空 → **多空分析那一整块是空的**；
  · 方向只剩请求路径的词表兜底（`source = "rules"`）。

而用户要看的恰恰是小作文。所以这不是"慢"，是**结构上永远轮不到** ——
只有把"选择规则"本身钉住才能防住它复现。

不联网、不调模型：直接测 `_interleave_by_kind` 这个纯函数，
另加一条"端到端只看选谁"的朴素用例。
"""

from __future__ import annotations

from typing import Any

from src.domain.intel import tone_job


def _it(kind: str, minute: int, i: int) -> dict[str, Any]:
    """一条候选：`minute` 越大越新（调用方会按它倒序排）。"""
    return {
        "kind": kind,
        "content_hash": f"{kind}-{i}",
        "title": f"{kind} {i}",
        "summary": "正文",
        "published_at": f"2026-09-25T{10 + minute // 60:02d}:{minute % 60:02d}:00+0800",
        "credibility": {"score": 58},
    }


def test_interleave_gives_every_kind_a_slot() -> None:
    """★ 168 条快讯 + 24 条券商作文、额度 40 → 券商作文**必须**拿到名额。

    这正是那次实测的比例。原实现（纯时间序）下这 24 条一条都进不来。
    """
    news = [_it("newswire", minute=i // 3, i=i) for i in range(168)]
    notes = [_it("research_note", minute=i // 3, i=i) for i in range(24)]
    # 现实里调用方已按时间倒序排过；这里把快讯整体排在前面（它们更新）。
    todo = sorted(news + notes,
                  key=lambda x: x["published_at"], reverse=True)

    picked = tone_job._interleave_by_kind(todo)[:40]
    kinds = [p["kind"] for p in picked]

    assert "research_note" in kinds, (
        "券商作文一条都没被选中 —— 又回到「永远轮不到」那个状态了")
    assert kinds.count("research_note") >= 5, (
        f"券商作文只拿到 {kinds.count('research_note')} 个名额，太少")
    # 关键不是"谁多"，而是**没有任何一类能独占**。两类时轮流就是各一半 ——
    # 一轮 40 个额度 → 20 条快讯 + 20 条笔记。代价是这一轮会长（笔记是长文，
    # 实测一条约 2 分钟），但只有**积压那一轮**如此；补齐之后每轮只剩新增的几条。
    assert kinds.count("newswire") < len(picked), "快讯又独占了一整轮"
    assert set(kinds) == {"newswire", "research_note"}, f"只选中了 {set(kinds)}"


def test_interleave_keeps_time_order_inside_a_kind() -> None:
    """类型**内部**仍按传入顺序（调用方已排成最新优先），不许打乱。"""
    news = [_it("newswire", minute=i, i=i) for i in range(6)]
    todo = list(reversed(news))          # 最新在前
    picked = tone_job._interleave_by_kind(todo)
    assert [p["content_hash"] for p in picked] == \
        [n["content_hash"] for n in todo]


def test_interleave_handles_missing_kind() -> None:
    """`kind` 缺省/为空也要能跑（老数据、手工构造的条目）。"""
    items = [{"content_hash": "a"}, {"content_hash": "b", "kind": ""}]
    assert len(tone_job._interleave_by_kind(items)) == 2


def test_interleave_is_a_permutation() -> None:
    """只重排、不增不减、不重复 —— 漏一条就是"某条内容永远抽不到"。"""
    todo = [_it("newswire", minute=i, i=i) for i in range(7)] \
        + [_it("research_note", minute=i, i=i) for i in range(3)] \
        + [_it("broker_report", minute=i, i=i) for i in range(2)]
    out = tone_job._interleave_by_kind(todo)
    assert len(out) == len(todo)
    assert {id(x) for x in out} == {id(x) for x in todo}
