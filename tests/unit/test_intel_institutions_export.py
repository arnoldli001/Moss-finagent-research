"""机构名（内容里的"XX证券"）必须**出得去接口**，而渠道身份必须**出不去**。

> 用户口径（2026-10-01）："券商名不一定要告警，但是**一定要前端输出信息**。"

## 为什么要单独一个文件

这条需求的失败方式是"静默少一个字段"：`IntelItem.to_public()` 与
`IntelFeed.to_public()` 都是**白名单/黑名单构造**，漏一处不会报错、
不会抛异常，只表现为"前端上没有那一块"。用户看不到任何异常，
只会觉得"这个功能好像没做"。

本项目已经踩过两次同类事故（都在 `service.py` 的注释里记着）：

    `extract_text` 忘了在收容组的 `_group_row()` 里透传 → 实测 21 条研究笔记
    的抽取入参全部退回 260 字展示摘要，"带全文的条数"是 0，而没有任何报错。

所以这里把**两条相反的方向**都钉住：

    要出得去的     institutions（内容里的机构名，公开信息）
    绝不出得去的   source_name / 渠道身份（`source_pseudonym` 假名化）
"""

from __future__ import annotations

from src.domain.intel import alert_rules as R
from src.domain.intel.service import IntelFeed
from src.infrastructure.connectors.intel_sources import (
    FORBIDDEN_PUBLIC_KEYS,
    IntelItem,
)


def _item(**kw) -> IntelItem:
    base = dict(
        kind="research_note",
        title="【中泰证券】汽车行业周报",
        summary="【中泰证券】发布行业周报，重申板块配置价值，" * 6,
        published_at="2026-10-01 10:00:00",
        source_alias="research-note-zsxq",
        source_name="知识星球-调研纪要",
        content_hash="h-institutions",
    )
    base.update(kw)
    return IntelItem(**base)


# ======================================================================
# ① 要出得去：institutions
# ======================================================================

def test_item_export_carries_institutions() -> None:
    """契约层的导出**必须**带 `institutions`（前端"机构：中泰证券"的数据源）。"""
    pub = _item().to_public()
    assert "institutions" in pub
    assert pub["institutions"] == ["中泰证券"]


def test_item_export_institutions_is_empty_list_when_none_found() -> None:
    """没有机构名时给**空列表**而不是缺键 —— 前端不必写 `?.` 兜底。"""
    pub = _item(title="隔夜美股收跌", summary="三大指数收跌，成交量小幅放大" * 8,
                source_name="新浪-7x24快讯", kind="newswire",
                source_alias="newswire-sina").to_public()
    assert pub["institutions"] == []


def test_feed_export_does_not_strip_institutions() -> None:
    """⚠️ `IntelFeed.to_public()` 是**剔除式**的：只有 `internal` 里列出的键会被摘掉。

    这条用例钉住"institutions **不在**那张剔除表里" —— 一旦有人
    （很合理地）以为它是内部字段而加进去，前端就再也看不到机构名了，
    而那种改动不会让任何东西报错。
    """
    feed = IntelFeed(items=[_item().to_public()], fetched_at="now")
    pub = feed.to_public()
    assert pub["items"][0]["institutions"] == ["中泰证券"]
    # 对照：真正的内部字段确实被剥掉了（证明这条链路的剔除是生效的）
    feed.items[0]["extract_text"] = "全文" * 100
    feed.items[0]["market_terms"] = ["中泰证券"]
    stripped = feed.to_public()["items"][0]
    assert "extract_text" not in stripped
    assert "market_terms" not in stripped
    assert stripped["institutions"] == ["中泰证券"]


def test_group_row_passes_institutions_through() -> None:
    """收容组里的子条目是**另一个 dict**（`_group_row` 白名单投影）。

    ⚠️ 不透传的表现与 `extract_text` 那次事故一模一样：组内条目展开后
    看不到机构名，而**没有任何报错**。实测大量研究笔记都落在收容组里，
    所以这条不是边角情况。
    """
    from src.domain.intel.service import _group_row

    row = _group_row({"title": "t", "summary": "s",
                      "institutions": ["天风证券"], "extract_text": "x"})
    assert row["institutions"] == ["天风证券"]


# ======================================================================
# ② 出不去：渠道身份（本条需求**不削弱**来源隐私纪律）
# ======================================================================

def test_institutions_does_not_leak_channel_identity() -> None:
    """机构名是**内容里的机构**，不是"这条来自知识星球"。

    两者必须分得很清楚：

        机构名    研报本来就公开署名 → 可以出接口
        渠道身份  我们用了哪个星球/公众号 → 一律不出（`source_pseudonym`）

    ⚠️ 这条用例防的是"顺手把来源也一起显示出来"那类改动 ——
    用户要的是"看到这条提到了哪家券商"，不是"看到这条从哪抄来的"。
    """
    pub = _item().to_public()
    # `source_alias` 必须是稳定假名，不能是明文来源
    assert pub["source_alias"] != "research-note-zsxq"
    assert pub["source_alias"].startswith("src-")
    # 知识星球不是"公开财经平台"，`platform` 给空串（白名单里没有它）
    assert pub["platform"] == ""
    # 机构名里不能出现渠道相关的字样
    for name in pub["institutions"]:
        assert "星球" not in name and "调研纪要" not in name


def test_public_export_has_no_forbidden_keys() -> None:
    """既有禁令仍然成立：导出里不能出现任何 `FORBIDDEN_PUBLIC_KEYS`。

    加 `institutions` 这个字段时**顺手复核**一遍 —— 它带的是机构名，
    与禁令里那些（author / topic_id / url / group_id…）不是一类东西。
    """
    pub = _item().to_public()
    assert not (set(pub) & FORBIDDEN_PUBLIC_KEYS)
    assert "source_name" not in pub


# ======================================================================
# ③ 与规则层的一致性
# ======================================================================

def test_contract_layer_and_alert_layer_agree_on_institutions() -> None:
    """契约层（未清洗原文）与规则层（清洗后文本）**用同一份实现**。

    两处各写一遍判据必然漂移，而漂移的表现是"前端显示了机构名、
    告警侧却没命中"（或反过来）—— 两边对不上账，且没人能一眼看出为什么。
    """
    item = _item()
    pub = item.to_public()
    layer_hits = R.institutions({"title": item.title, "summary": item.summary})
    assert pub["institutions"] == layer_hits == ["中泰证券"]
