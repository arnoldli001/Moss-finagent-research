"""跨侧同名：同一个板块/个股**不许同时挂在利好与利空两侧**。

> 用户口径（2026-09-26）：
>
>   "同一个板块会同时出现在「利好」和「利空」两侧，同名两侧都不放，
>    在触发依据里不用写任何内容。"

## 为什么这条必须单独钉

实测来源：一条券商作文（瑞银——特斯拉）的多空分析里出现

    利好  板块 储能 / 个股 特斯拉
    利空  板块 储能

它**看起来像功能坏了**，而每一段单独看都是合法的 —— 模型在段 1 把"储能"
写进利好、在段 3 写进利空，合并时两侧都留下了。

两个纪律：
  1. **两侧都不放**（不是"留证据多的那一侧"）：我们没有带方向的计数，
     `count` 只是"提到几次"，拿它选一侧等于替用户编方向；
  2. **不写进触发依据**：用户明确说"不用写任何内容"——那一栏的职责是
     "方向靠哪几个词判的"（逐字可核对），不塞我们的解释。
"""

from __future__ import annotations

import pytest

from src.domain.intel import tone as T


def _side(inds: list[str], stocks: list[tuple[str, str]]) -> dict:
    return {
        "industries": list(inds),
        "stocks": [{"name": n, "code": c, "count": 1} for n, c in stocks],
        "count": len(inds) + len(stocks),
        "boards": [{"name": n, "code": f"88{i:04d}.TI"} for i, n in enumerate(inds)],
    }


# ======================================================================
# ① 纯函数：两侧都不放
# ======================================================================

def test_industry_on_both_sides_is_dropped_from_both() -> None:
    """★ 行业同名 → 两侧都删（这就是"储能"那个 case）。"""
    bull = _side(["储能", "光伏"], [])
    bear = _side(["储能"], [])
    b, s = T.drop_cross_side(bull, bear)
    assert b["industries"] == ["光伏"]
    assert s["industries"] == []
    assert b["count"] == 1 and s["count"] == 0


def test_stock_on_both_sides_is_dropped_from_both() -> None:
    bull = _side([], [("特斯拉", "TSLA"), ("中芯国际", "688981")])
    bear = _side([], [("特斯拉", "TSLA")])
    b, s = T.drop_cross_side(bull, bear)
    assert [x["name"] for x in b["stocks"]] == ["中芯国际"]
    assert s["stocks"] == []


def test_stock_compared_by_name_not_by_pair() -> None:
    """★ 同名不同码也要删：用户看到的是**名字**，不是 (名字, 代码)。

    一条里带代码、另一条里只给名字是常态（模型有时只写名字）。
    按二元组判会让它两侧都留着，界面照样显示同一句话两次。
    """
    bull = _side([], [("中芯国际", "688981")])
    bear = _side([], [("中芯国际", "")])
    b, s = T.drop_cross_side(bull, bear)
    assert b["stocks"] == [] and s["stocks"] == []


def test_code_only_entries_compared_by_code() -> None:
    """没有名字、只有代码时按代码判 —— 否则所有"无名字"的票会互相撞成同名。"""
    bull = _side([], [("", "600000"), ("", "600001")])
    bear = _side([], [("", "600000")])
    b, s = T.drop_cross_side(bull, bear)
    assert [x["code"] for x in b["stocks"]] == ["600001"]
    assert s["stocks"] == []


def test_boards_follow_the_dropped_industries() -> None:
    """`boards` 与 `industries` 是同一批：删了行业必须一起删，否则留悬空板块。"""
    bull = _side(["储能", "光伏"], [])
    bear = _side(["储能"], [])
    b, _ = T.drop_cross_side(bull, bear)
    assert [x["name"] for x in b["boards"]] == ["光伏"]


def test_no_overlap_returns_the_same_objects() -> None:
    """没有交集时**原样返回**（这条在抽取热路径上，不该白重建 dict）。"""
    bull = _side(["储能"], [("特斯拉", "TSLA")])
    bear = _side(["面板"], [("京东方A", "000725")])
    b, s = T.drop_cross_side(bull, bear)
    assert b is bull and s is bear


def test_explain_is_not_touched() -> None:
    """★ 用户口径："在触发依据里不用写任何内容" —— 不许加说明。"""
    res = T.ToneResult(
        tone=T.TONE_UNKNOWN,
        explain="各段均未判出语气",
        bullish=_side(["储能"], []),
        bearish=_side(["储能"], []),
    )
    b, s = T.drop_cross_side(res.bullish, res.bearish)
    res.bullish, res.bearish = b, s
    assert res.explain == "各段均未判出语气", "触发依据被改写了（用户要求不写任何内容）"
    assert "分歧" not in res.to_public()["explain"]


# ======================================================================
# ② 端到端：合并时会**制造**跨侧同名
# ======================================================================

def test_merge_then_drop() -> None:
    """★ 单段各自合法，合起来才是矛盾 —— 所以合并之后必须再去一次。

    模拟实测那条：段 1 说"储能"利好，段 3 说"储能"利空。
    """
    part_bull = T.ToneResult(tone=T.TONE_BULL,
                             bullish=_side(["储能"], []), bearish={})
    part_bear = T.ToneResult(tone=T.TONE_BEAR,
                             bullish={}, bearish=_side(["储能"], []))
    merged = T.merge_tone_results([part_bull, part_bear], segments=2, calls=2)
    assert merged.bullish["industries"] == [], "利好侧还留着同名板块"
    assert merged.bearish["industries"] == [], "利空侧还留着同名板块"
    assert merged.bullish["count"] == 0 and merged.bearish["count"] == 0
    assert merged.bullish["boards"] == []


def test_merge_keeps_non_overlapping_names() -> None:
    """只有交集被删，两侧各自独有的照常保留（别把功能删没了）。"""
    part_bull = T.ToneResult(tone=T.TONE_BULL,
                             bullish=_side(["储能", "固态电池"],
                                           [("宁德时代", "300750")]),
                             bearish={})
    part_bear = T.ToneResult(tone=T.TONE_BEAR,
                             bullish={},
                             bearish=_side(["储能"], [("宁德时代", "300750"),
                                                      ("京东方A", "000725")]))
    merged = T.merge_tone_results([part_bull, part_bear], segments=2, calls=2)
    assert merged.bullish["industries"] == ["固态电池"]
    assert merged.bearish["industries"] == []
    assert [x["name"] for x in merged.bullish["stocks"]] == []
    assert [x["name"] for x in merged.bearish["stocks"]] == ["京东方A"]


def test_public_payload_never_shows_the_same_name_on_both_sides() -> None:
    """对外契约层也要干净：`to_public()` 出来的两侧没有交集。"""
    part_bull = T.ToneResult(tone=T.TONE_BULL,
                             bullish=_side(["储能"], [("特斯拉", "TSLA")]),
                             bearish={})
    part_bear = T.ToneResult(tone=T.TONE_BEAR,
                             bullish={},
                             bearish=_side(["储能"], [("特斯拉", "TSLA")]))
    pub = T.merge_tone_results([part_bull, part_bear],
                               segments=2, calls=2).to_public()
    b = {x["name"] for x in pub["bullish"]["industries"]} \
        & {x["name"] for x in pub["bearish"]["industries"]}
    assert not b, f"板块两侧都有：{b}"
    bst = {x["name"] for x in pub["bullish"]["stocks"]} \
        & {x["name"] for x in pub["bearish"]["stocks"]}
    assert not bst, f"个股两侧都有：{bst}"


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
