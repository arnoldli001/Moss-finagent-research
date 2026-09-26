"""知识星球**条目留存**的回归测试（`item_store`）。

> 用户口径（2026-09-26）："拉高上限 到100条且落库最多保留3天。"

这一层要防住的事，全都是**静默**的：

  · 保留期与情报流时效窗口**不一致**（一个 3 天一个 7 天）→ 页面上会出现
    "窗口外"的条目，或者留存白占地方；
  · 留存行**覆盖**本轮真正取到的同一条（字段更旧）→ 新取到的那份被老快照顶掉；
  · prune 把时间戳读不懂的行当过期删掉 → 那是**真的丢用户内容**；
  · 半份文件（写一半被杀）→ 后面所有行都读不出来，看起来像"留存全坏了"。

不联网、不落生产文件：全部用 `tmp_path`。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.domain.intel import item_store as S


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    S.reset_cache()
    yield
    S.reset_cache()


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc).astimezone()
            - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _item(i: int, *, days_ago: float = 0.0, kind: str = "research_note") -> dict:
    return {
        "content_hash": f"h{i}",
        "title": f"标题 {i}",
        "summary": f"摘要 {i}",
        "published_at": _iso(days_ago),
        "kind": kind,
        "source_alias": "src-abc",
        "platform": "",
        "codes": [],
        "industry": "",
        "agency": "",
        "rating_origin": "",
        "credibility": {"score": 58, "source_base": 54, "content_base": 78},
        # 下面两个是**内部/派生**字段，必须**不**落库
        "extract_text": "全文正文" * 100,
        "market_terms": ["储能"],
        "kind_label": "券商作文",
    }


# ======================================================================
# ① 保留期必须与情报流时效窗口一致
# ======================================================================

def test_retain_days_matches_feed_window() -> None:
    """★ 3 天这个数字在两处各写了一遍（刻意的），必须一致。

    `service` 是聚合层、本模块是存储层，import 过来会让"窗口调了、存储跟着变"
    这种**需要有人看懂再决定**的改动变成一次静默生效（同
    `body_store.RETAIN_DAYS`）。所以加一条测试钉住。
    """
    from src.domain.intel import body_store, service

    assert S.RETAIN_DAYS == service.FEED_WINDOW_DAYS, (
        f"留存 {S.RETAIN_DAYS} 天 ≠ 情报流时效窗口 "
        f"{service.FEED_WINDOW_DAYS} 天 —— 页面上会出现窗口外的条目")
    assert body_store.RETAIN_DAYS == S.RETAIN_DAYS, (
        "两份留存（全文 / 条目快照）的保留期不一致 —— "
        "「查看原文」会与「并回情报流」对同一条给出不同答案")


# ======================================================================
# ② 写与读
# ======================================================================

def test_persist_then_load_roundtrip(tmp_path: Path) -> None:
    stats = S.persist([_item(1), _item(2)], root=tmp_path)
    assert stats["written"] == 2
    rows = S.load(root=tmp_path, force=True)
    assert set(rows) == {"h1", "h2"}
    assert rows["h1"]["summary"] == "摘要 1"
    assert rows["h1"]["credibility"]["score"] == 58


def test_internal_keys_are_not_stored(tmp_path: Path) -> None:
    """★ 白名单：`extract_text` / `market_terms` / `kind_label` **不落库**。

    `extract_text` 是清洗后的全文 —— 存下来等于把全文复制一份到另一个文件，
    而这个文件是"会被接口按需读出来并重建条目"的；`kind_label` 是展示名，
    存了改名就改不动老数据。
    """
    S.persist([_item(1)], root=tmp_path)
    raw = S.store_path(tmp_path).read_text(encoding="utf-8")
    assert "extract_text" not in raw
    assert "market_terms" not in raw
    assert "kind_label" not in raw
    row = S.load(root=tmp_path, force=True)["h1"]
    assert "extract_text" not in row and "kind_label" not in row


def test_same_hash_is_not_rewritten(tmp_path: Path) -> None:
    """同指纹不重复写：这条链路每轮都把"最新 N 条"再送一次。"""
    assert S.persist([_item(1)], root=tmp_path)["written"] == 1
    assert S.persist([_item(1)], root=tmp_path)["written"] == 0


def test_row_without_hash_is_skipped(tmp_path: Path) -> None:
    """没有指纹就查不回来 —— 跳过它（收容组那种合成条目就是这一类）。"""
    assert S.build_row({"title": "x"}) is None
    assert S.persist([{"title": "x", "summary": "y"}], root=tmp_path)["written"] == 0


def test_broken_file_reads_as_empty(tmp_path: Path) -> None:
    """文件读坏了按空处理（留存是锦上添花，不该让情报流挂掉）。"""
    p = S.store_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("这不是 JSON\n{\"content_hash\": \"h1\"}\n", encoding="utf-8")
    rows = S.load(root=tmp_path, force=True)
    assert list(rows) == ["h1"], "坏行应该跳过，好行要留下"


# ======================================================================
# ③ prune：按 published_at，不是按入库时间
# ======================================================================

def _write_directly(tmp_path: Path, items: list[dict]) -> None:
    """绕开 `persist` 直接落盘。

    ⚠️ `persist` **自带按需 prune**（见 `_prune_due`），所以"先 persist 再断言
    prune 删了几条"会把清理算两遍 —— 第一版就是这么写的，两条用例都拿到 0。
    要单独测 `prune`，就得用不触发自动清理的 `save_many`。
    """
    rows = [r for r in (S.build_row(i) for i in items) if r]
    S.save_many(rows, root=tmp_path)


def test_persist_prunes_by_itself(tmp_path: Path) -> None:
    """`persist` 会顺带清理过期行（不然"只写不清"要几天后才看得出来）。"""
    stats = S.persist([_item(1, days_ago=0.5), _item(2, days_ago=5)],
                      root=tmp_path)
    assert stats["written"] == 2
    assert stats["pruned"] == 1, "persist 没有顺带清理过期行"
    assert set(S.load(root=tmp_path, force=True)) == {"h1"}


def test_prune_drops_rows_outside_the_window(tmp_path: Path) -> None:
    _write_directly(tmp_path, [_item(1, days_ago=0.5), _item(2, days_ago=5)])
    assert S.prune(root=tmp_path) == 1
    assert set(S.load(root=tmp_path, force=True)) == {"h1"}


def test_prune_judges_by_published_not_by_stored_time(tmp_path: Path) -> None:
    """★ 判据是 `published_at`（条目自己的时间），不是入库时间 `at`。

    按 `at` 剪枝会留下"5 天前发布、今天才取到"的行 —— 它下一次请求必然被
    时效窗口丢掉，只是白占地方。
    """
    _write_directly(tmp_path, [_item(1, days_ago=5)])   # at = 现在，published = 5 天前
    assert S.prune(root=tmp_path) == 1
    assert S.load(root=tmp_path, force=True) == {}


def test_prune_keeps_rows_with_broken_timestamp(tmp_path: Path) -> None:
    """时间戳读不懂的行**保守保留**（宁可多留，也不要误删还新的内容）。"""
    p = S.store_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    it = _item(1)
    it["published_at"] = "不是时间"
    p.write_text(json.dumps(it, ensure_ascii=False) + "\n", encoding="utf-8")
    assert S.prune(root=tmp_path) == 0
    assert list(S.load(root=tmp_path, force=True)) == ["h1"]


def test_prune_enforces_max_rows_by_oldest_first(tmp_path: Path) -> None:
    """超上限时**丢最旧的**，不是丢最新的。"""
    for i in range(6):
        S.persist([_item(i, days_ago=i * 0.1)], root=tmp_path)
    removed = S.prune(root=tmp_path, max_rows=3)
    assert removed == 3
    left = set(S.load(root=tmp_path, force=True))
    assert left == {"h0", "h1", "h2"}, f"丢错了方向：留下 {left}"


def test_prune_is_atomic(tmp_path: Path) -> None:
    """重写走 tmp + 原子替换：不留半份文件、不留 .tmp 残骸。"""
    S.persist([_item(1), _item(2, days_ago=9)], root=tmp_path)
    S.prune(root=tmp_path)
    d = S.store_path(tmp_path).parent
    assert not list(d.glob("*.tmp")), "留下了 .tmp 残骸"
    # 文件仍然逐行可解析
    for line in S.store_path(tmp_path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            json.loads(line)


def test_prune_noop_when_nothing_expired(tmp_path: Path) -> None:
    S.persist([_item(1)], root=tmp_path)
    assert S.prune(root=tmp_path) == 0


# ======================================================================
# ④ 版本/兼容：老行缺键时不能炸
# ======================================================================

def test_load_tolerates_rows_missing_new_keys(tmp_path: Path) -> None:
    """第一轮之前的行没有 `credibility` —— 是**正常状态**，不是损坏。"""
    p = S.store_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"content_hash": "h9", "title": "老行",
                             "published_at": _iso(0.1)},
                            ensure_ascii=False) + "\n", encoding="utf-8")
    rows = S.load(root=tmp_path, force=True)
    assert rows["h9"]["title"] == "老行"
    assert "credibility" not in rows["h9"]
