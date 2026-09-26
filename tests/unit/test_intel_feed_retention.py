"""**留存并回**的端到端验证：窗口滑走的内容要能留在页面上。

> 用户口径（2026-09-26）：
>
>   "拉高上限 到100条且落库最多保留3天。"

## 这条用例要复现的真实场景

知识星球那条链路每轮只取"最新 N 条帖子"（`MAX_FETCH_PER_RUN`）。发帖一密，
早先取到的帖子就被挤出窗口 —— 而情报流是**每次请求现拼**的，所以它就
从页面上消失了（用户："原来的信息丢那里去了"）。

所以这里造两轮：

    第 1 轮  取到 A、B、C 三条 → 都上页面，同时落进留存
    第 2 轮  只取到 C（A、B 被新帖子挤出窗口）
             → A、B 必须**从留存补回来**，且标着 `retained=True`

留着这个标记是刻意的：页脚据此说"其中 N 条来自留存（本轮未取到）"——
不说的话用户会以为这些是刚抓到的。

⚠️ 存留路径由 `tests/conftest.py` 的 `_isolate_intel_item_store` 重定向到临时
目录，所以这条用例**不会碰**仓库里的 `data/intel/zsxq_items.jsonl`。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.domain.intel import item_store
from src.domain.intel import service as S
from src.infrastructure.connectors.zsxq_incremental import IncrementalResult


class _Topic:
    def __init__(self, i: int, *, days_ago: float = 0.0) -> None:
        self.title = f"券商作文 {i}"
        # 正文要够长：`content_filter.MIN_CONTENT_CHARS` 会把短正文当噪音丢掉。
        self.text = (f"这是第 {i} 篇券商作文的正文，用于验证留存并回，"
                     f"长度需要超过内容过滤的最小字数门槛，否则它会在"
                     f"内容过滤那一步就被丢掉，测试失败的原因看起来像别的。")
        self.created_at = (datetime.now(timezone.utc).astimezone()
                           - timedelta(days=days_ago)).isoformat(
                               timespec="seconds")
        self.content_hash = f"note{i}"


def _stub_round(monkeypatch: pytest.MonkeyPatch,
                topics: list[_Topic]) -> None:
    """把两个来源都短路：内置源返回空，知识星球返回给定的一批。

    ⚠️ 同时**作废源级缓存**（`source_cache`）。本文件的用例核心手法是
    "第 1 轮给三条、第 2 轮只给一条"来模拟窗口滑动，而源级缓存会把第 1 轮
    那三条按 TTL（45 秒）留着 —— 于是第 2 轮拿到的是旧数据，"窗口滑动"
    这个被模拟的场景根本没发生，断言全部对不上。

    在**换替身时**清缓存是语义正确的：上游数据变了，缓存本来就该失效。
    （根 `conftest` 的 autouse 夹具只保证用例之间不串，用例**内部**换数据
    要自己说清。）
    """
    from src.domain.intel import source_cache

    source_cache.reset()

    async def _fake_fetch_all(**_kw: Any):
        return [], {}

    def _fake_incremental(**_kw: Any) -> IncrementalResult:
        return IncrementalResult(
            topics=topics,
            watermark="2026-09-01T00:00:00+0800",
            pages_used=1,
            new_count=len(topics),
            newest=topics[-1].created_at if topics else "",
        )

    monkeypatch.setattr(
        "src.infrastructure.connectors.intel_sources.fetch_all", _fake_fetch_all)
    monkeypatch.setattr(
        "src.infrastructure.connectors.zsxq_incremental.fetch_incremental",
        _fake_incremental)
    monkeypatch.setattr(
        "src.infrastructure.connectors.zsxq_incremental.save_watermark",
        lambda *a, **k: None)


def _run(**kw: Any):
    return asyncio.run(S.build_feed(**kw))


def test_window_slide_does_not_lose_earlier_items(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 第 2 轮只取到 1 条时，前一轮那两条必须**从留存补回来**。"""
    a, b, c = _Topic(1), _Topic(2), _Topic(3)

    # ── 第 1 轮：三条都取到 ──
    _stub_round(monkeypatch, [a, b, c])
    feed1 = _run(limit=50, group_undetermined=False)
    titles1 = {it.get("title") for it in feed1.items}
    assert titles1 == {"券商作文 1", "券商作文 2", "券商作文 3"}, titles1
    assert not any(it.get("retained") for it in feed1.items), \
        "第 1 轮全都是本轮取到的，不该带留存标记"

    # ── 第 2 轮：窗口滑到只剩 C ──
    _stub_round(monkeypatch, [c])
    feed2 = _run(limit=50, group_undetermined=False)
    by_title = {it.get("title"): it for it in feed2.items}

    assert "券商作文 3" in by_title, "本轮真取到的那条不见了"
    assert "券商作文 1" in by_title and "券商作文 2" in by_title, (
        f"被窗口挤掉的两条没有从留存补回来：{sorted(by_title)}")
    assert by_title["券商作文 1"].get("retained") is True, \
        "留存补回来的条目必须带 retained 标记（页脚要如实说明）"
    assert not by_title["券商作文 3"].get("retained"), \
        "本轮真取到的那条不该被标成留存"
    # 本轮取到的那份优先：两处都有时不重复
    assert len([t for t in by_title if t == "券商作文 3"]) == 1


def test_retained_item_keeps_credibility_and_label(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """补回来的条目要能像普通条目一样参与筛选/展示。

    `credibility` 照抄存储值（重算会让老内容分数跳动），`kind_label`
    **现算**（改名要能作用于老数据）。
    """
    _stub_round(monkeypatch, [_Topic(7)])
    _run(limit=50, group_undetermined=False)

    _stub_round(monkeypatch, [])          # 本轮一条都没取到
    feed = _run(limit=50, group_undetermined=False)
    it = next(i for i in feed.items if i.get("content_hash") == "note7")
    assert it.get("kind_label") == "券商作文", "标签必须按当前口径现算"
    assert (it.get("credibility") or {}).get("score"), "可信度丢了就筛不出来"
    assert it.get("kind") == "research_note"


def test_store_row_is_rebuilt_not_reused(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """读取侧**重建**条目：存储里没有 `kind_label`，重建时才有。"""
    _stub_round(monkeypatch, [_Topic(9)])
    _run(limit=50, group_undetermined=False)
    row = item_store.load()["note9"]
    assert "kind_label" not in row, (
        "展示名被存进了存储 —— 改名就改不动老数据了")


def test_retention_survives_a_round_that_fetched_nothing(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """上游整轮返回空（实测约 1/6）时，页面上**不该**整块空掉 —— 还有留存。"""
    _stub_round(monkeypatch, [_Topic(4), _Topic(5)])
    _run(limit=50, group_undetermined=False)

    _stub_round(monkeypatch, [])          # 本轮上游空
    feed = _run(limit=50, group_undetermined=False)
    titles = {i.get("title") for i in feed.items}
    assert {"券商作文 4", "券商作文 5"} <= titles, (
        f"上游空一轮就把整块内容丢了（留存没接上）：{titles}")


def test_retained_rows_are_pruned_after_the_window(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """窗口外的留存行不该被并回来（3 天是硬边界）。"""
    _stub_round(monkeypatch, [_Topic(1), _Topic(2, days_ago=5)])
    _run(limit=50, group_undetermined=False)

    _stub_round(monkeypatch, [])
    feed = _run(limit=50, group_undetermined=False)
    titles = {i.get("title") for i in feed.items}
    assert "券商作文 1" in titles
    assert "券商作文 2" not in titles, (
        "5 天前的条目被并回来了 —— 时效窗口那条纪律被绕开了")
