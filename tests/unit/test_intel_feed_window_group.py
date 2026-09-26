"""情报流的**展示层收容**不该改变"有哪些内容"。

## 这个文件为什么存在

用户口径（2026-09-25）：

    "情报流 最多保留最近7天的新闻信息，超过7天的都过滤不要，
     来自财经日历刷屏的信息太多了。"
    "原文倾向未定的，可以聚合成一条，点开可查看。"

两个改动都作用在 `build_feed` 上，而 `build_feed` **同时**是
`/feed` 接口与三个内部消费者（倾向抽取 / 平台热议聚合 / 热议扫描）
的取数入口。第一次实现时我忘了这件事：

  · 收容组上线后，`_intel_tone_extract` 走默认参数拿到 **2 条**
    （1 条信号 + 1 个组），当轮只处理 2 条就收工，
    而任务日志显示"抽取 0 条"，看起来像"没有可抽的"；
  · `hot_scan` 同理，扫 2 条 → 热议个股几乎为空。

症状是"功能没报错、但没有内容"，这种最容易漏。所以用单测把
**"内部消费者拿到的是平铺池"**这条契约锁住。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.intel import service as S


def _item(idx: int, *, days_ago: float = 0.0, tone: dict | None = None,
          kind: str = "newswire") -> dict:
    ts = (datetime.now(timezone.utc).astimezone()
          - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    # ⚠️ 正文要够长：`content_filter.MIN_CONTENT_CHARS=50` 会把短正文判成
    # `too_short` 丢掉 —— 第一版桩数据写的是"正文 1"，于是流水线在
    # 内容过滤那一步就把样本清空了，测试失败的原因看起来像"窗口过滤太狠"。
    body = (f"这是第 {idx} 条公开快讯的正文内容，用于验证情报流的"
            f"时效窗口与倾向收容逻辑，长度需要超过内容过滤的最小字数门槛。")
    return {
        "kind": kind,
        "kind_label": {"newswire": "财经快讯",
                       "research_note": "券商作文"}.get(kind, kind),
        "title": f"标题 {idx}",
        "summary": body,
        "published_at": ts,
        "source_alias": "src-abc12345",
        "platform": "东方财富",
        "codes": [],
        "industry": "",
        "rating_origin": "",
        "agency": "",
        "content_hash": f"hash{idx}",
        "extra": {},
        "credibility": {"score": 74, "source_base": 74, "content_base": 74,
                        "source_reason": "权威财经媒体",
                        "content_reason": "含事实要素"},
        "tone": tone,
    }


class _StubItem:
    """`fetch_all` 的返回元素要能 `to_public()`（流水线就是这么用的）。

    这里刻意**不**构造真的 `IntelItem`：那会连带把脱敏、可信度打分、
    摘要截断都拉进来，而本文件要测的是窗口与收容**这两段**。
    用桩把上游压平，测的东西才不会被别处的改动带偏。
    """

    def __init__(self, payload: dict) -> None:
        self._p = payload

    def to_public(self) -> dict:
        return dict(self._p)


def _patch_pipeline(monkeypatch, items: list[dict]) -> None:
    """把 `build_feed` 的取数与外部依赖短路，只留我们要测的两段。"""
    async def _fake_fetch_all(**_kw):
        return [_StubItem(x) for x in items], {}

    monkeypatch.setattr(
        "src.infrastructure.connectors.intel_sources.fetch_all", _fake_fetch_all)
    # 知识星球走独立失败域，测试里让它"取不到"，只留内置源
    monkeypatch.setattr(
        "src.infrastructure.connectors.zsxq_incremental.fetch_incremental",
        lambda: (_ for _ in ()).throw(RuntimeError("测试不取知识星球")))
    monkeypatch.setattr(
        "src.infrastructure.connectors.zsxq_incremental.save_watermark",
        lambda *a, **k: None)


def _run(**kw):
    return asyncio.run(S.build_feed(**kw))


# ======================================================================
# 时效窗口
# ======================================================================

def test_items_older_than_window_are_dropped(monkeypatch) -> None:
    """超过窗口（**现在是 3 天**）的条目必须被过滤掉（含横跨九年的研报归档）。"""
    items = [
        _item(1, days_ago=0.1),
        _item(2, days_ago=2.5),
        _item(3, days_ago=4),        # 窗口外（旧口径 7 天时它在窗口内）
        _item(4, days_ago=400),      # 窗口外
        _item(5, days_ago=3000),     # 窗口外（模拟 2017 年的研报）
    ]
    _patch_pipeline(monkeypatch, items)
    feed = _run(limit=50, group_undetermined=False)
    titles = {x["title"] for x in feed.items}
    assert titles == {"标题 1", "标题 2"}, titles


def test_window_is_three_days(monkeypatch) -> None:
    """边界值锁死：常量改了要有人知道。

    用户口径改过一次（2026-09-25 修订）：先是"最多保留最近 7 天"，
    现在是"只保留3天，超过日期的直接溢出丢弃"。断言改成 3 的同时，
    上面那条用例的样本也跟着挪了 —— 否则它会因为"样本仍在旧窗口里"
    而给出一个看似通过的假信号。
    """
    assert S.FEED_WINDOW_DAYS == 3


def test_all_items_outside_window_reports_a_gap(monkeypatch) -> None:
    """某一类**全部**落在窗口外要如实报缺口，不能静默消失。

    用户此前问过"研究笔记怎么在前端看不到了？" —— 静默消失正是那个体验。

    ⚠️ 缺口文案里的标签**跟着展示名走**（2026-10-01 改为「券商作文」）：
    它是**用户直接看到的字**，只改 `KIND_LABELS` 会让提示里留下一个
    界面上再也找不到的旧名字。
    """
    items = [_item(1, days_ago=30, kind="research_note"),
             _item(2, days_ago=40, kind="research_note")]
    _patch_pipeline(monkeypatch, items)
    feed = _run(limit=50, group_undetermined=False)
    assert feed.items == []
    assert any("券商作文" in (g.get("message") or "") for g in feed.gaps), feed.gaps


# ======================================================================
# 收容组
# ======================================================================

def test_undetermined_items_are_grouped_into_one(monkeypatch) -> None:
    """给不出方向的条目收成**一条**，且子条目带得上（点开可查看）。"""
    items = [_item(i) for i in range(1, 11)]      # 10 条都没有 tone
    _patch_pipeline(monkeypatch, items)
    feed = _run(limit=50)
    groups = [x for x in feed.items if x.get("is_group")]
    assert len(groups) == 1, feed.items
    g = groups[0]
    assert g["group_count"] == 10
    assert len(g["group_items"]) == 10
    # 子条目要够"点开能读"：标题 + 摘要 + 时间
    row = g["group_items"][0]
    assert row["title"] and row["summary"] and row["published_at"]


def test_directional_items_are_not_grouped(monkeypatch) -> None:
    """有方向的条目**绝不能**被收进组 —— 组的前提就是"我们没说它偏多偏空"。"""
    items = [_item(i) for i in range(1, 9)]        # 8 条无方向（够成组）
    items.append(_item(99, tone={"tone": "偏多", "has_tone": True,
                                 "neutral": False}))
    _patch_pipeline(monkeypatch, items)
    feed = _run(limit=50)
    groups = [x for x in feed.items if x.get("is_group")]
    assert len(groups) == 1, feed.items
    pooled = [r["title"] for r in groups[0]["group_items"]]
    assert "标题 99" not in pooled, "有方向的条目被收进组了"
    assert any(x["title"] == "标题 99" for x in feed.items if not x.get("is_group"))


def test_neutral_items_are_hidden_entirely(monkeypatch) -> None:
    """已判定为**中性**的既不入组、也不单列（用户："中性的不要显示"）。"""
    items = [_item(i) for i in range(1, 9)]
    items.append(_item(50, tone={"tone": "中性", "has_tone": False,
                                 "neutral": True}))
    _patch_pipeline(monkeypatch, items)
    feed = _run(limit=50)
    everything = list(feed.items)
    for g in [x for x in feed.items if x.get("is_group")]:
        everything.extend(g["group_items"])
    assert all("标题 50" != x.get("title") for x in everything), "中性条目漏出来了"


def test_small_group_is_not_collapsed(monkeypatch) -> None:
    """太少（< 阈值）不成组：给一个"2 条"的分组比直接显示那两条更烦人。"""
    items = [_item(1), _item(2)]
    _patch_pipeline(monkeypatch, items)
    feed = _run(limit=50)
    assert not any(x.get("is_group") for x in feed.items)
    assert {x["title"] for x in feed.items} == {"标题 1", "标题 2"}


# ======================================================================
# 内部消费者契约（本文件的核心）
# ======================================================================

def test_flat_pool_is_available_to_internal_consumers(monkeypatch) -> None:
    """`group_undetermined=False` 必须返回**未收容**的平铺池。

    这是"分析任务不该被展示层收容影响"的执行者。默认值给接口用，
    内部消费者传 False —— 两边都要有用例，否则哪一边被改掉都不会响。
    """
    items = [_item(i) for i in range(1, 21)]
    _patch_pipeline(monkeypatch, items)

    grouped = _run(limit=50)
    flat = _run(limit=50, group_undetermined=False)

    assert sum(1 for x in grouped.items if x.get("is_group")) == 1
    assert not any(x.get("is_group") for x in flat.items)
    # 关键：平铺池要**看得到全部内容**，而不是被收成一行
    assert len(flat.items) == 20, len(flat.items)
    assert len(grouped.items) == 1, len(grouped.items)


def test_tone_job_and_hot_job_use_flat_pool() -> None:
    """源码级断言：三个内部消费者都显式传了 `group_undetermined=False`。

    为什么用源码断言而不是行为断言：这三处的调用点在异步任务里，
    要跑起来得造一整套 runtime。而**忘记传**恰恰是最容易发生、
    又最难在运行日志里看出来的错误（日志只说"抽取 0 条"）。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for rel in ("src/scheduler/jobs.py",
                "src/domain/intel/hot_job.py",
                "src/api/routes/intel.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "group_undetermined=False" in src, (
            f"{rel} 里有 build_feed 调用没传 group_undetermined=False —— "
            "收容组会把它看到的条目收成一行，分析任务会静默空转")


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
