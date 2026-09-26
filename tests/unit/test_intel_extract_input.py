"""抽取入参：**先清洗、再（由 `tone_job`）分段**，而且 `extract_text` 永远不出接口。

## Step ② 为什么顺序不能反（实测的缺陷）

原来 `extract_text` 是直接对**原始文本**做压缩的（当时是"超过 600 字取两头
各 300 字"）。一条真实笔记的"尾部 300 字"整段是：

    …%E7%89%87%E4%BF%A1%E6%81%AF%23" />

—— URL 编码后的 `<e …>` 标签残片。原因很直白：标签的**头半截**落在被切掉的
那 300 字之外，等切完再想清洗，`<e` 都已经不在串里了，谁也救不回来。
所以顺序必须是：**剥富文本标签 → 再压缩**（第四轮起压缩 = `tone.segment_text`
切段，仍在清洗之后 —— 顺序不变，只是压缩那一步换了实现）。

清洗复用的是 `intel_sources.strip_rich_tags`（脱敏链路已经在用的那一份）——
本文件同时锁住"抽取这条路拿到的确实是**清洗后**的正文"。

## `extract_text` 为什么绝不能被浏览器看到

它是**内部键**：清洗后的抽取入参，专供 `tone_job` 分段抽取用。
`IntelFeed.to_public()` 必须把它剥掉（顶层**与**收容组的 `group_items` 里都要剥）
—— 不剥就等于把 `summary` 的 260 字展示截断整个抵消掉，移动端一条占满十屏。

⚠️ 收容组里那层是**另一个 dict**（`_group_row` 自己生成的投影），
只剥顶层等于没剥：曾经 21 条研究笔记全部落在组内，实测"带 extract_text 的
条数"就是 0（既不生效，也照样泄漏）。两处都要有用例。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from src.domain.intel import service as S
from src.domain.intel.tone import (
    EXTRACT_HALF_CHARS,
    EXTRACT_MAX_CHARS,
    extraction_text,
)

#: 标签的"尾部残片"形态（实测截图里的那一段）。
#: `%E7%89%87%E4%BF%A1%E6%81%AF%23` = "#文字图片信息#"（知识星球的 hashtag 标签值）
_TAIL_FRAGMENT = '%E7%89%87%E4%BF%A1%E6%81%AF%23" />'

#: 一条**以富文本标签结尾**的研究笔记（真实形态）：
#: `title="%E6%96%87%E5%AD%97..."` 是 hashtag 标签，`href` 是分享链接标签
_NOTE_WITH_TAGS = (
    "半导体设备板块景气上行，存储芯片价格连续上涨，中芯国际受益于扩产，"
    "机构上调盈利预测；光伏组件价格下滑，隆基绿能承压。"
    "综上，推荐关注设备与材料环节的龙头公司，并提示关注产能释放节奏。"
    '<e type="hashtag" hid="51122445582824" '
    f'title="{_TAIL_FRAGMENT}'
)
_NOTE_WITH_SHARE_LINK = (
    "英伟达 CoWoS-L 扩产展望 <e type=\"web\" "
    "href=\"https%3A%2F%2Fwx.zsxq.com%2Fmweb%2Fexternal_link.html%3Fk%3D1\">"
    "网页链接</e> 后文还在"
)


# ======================================================================
# Step ②：清洗 → 压缩
# ======================================================================

def test_extraction_input_is_prose_not_markup() -> None:
    """★ 以 `<e …>` 结尾的笔记，抽取入参里必须是**正文**，不是标签残片。"""
    out = S.extraction_input(_NOTE_WITH_TAGS)
    for bad in ("<e", "href", "title=", "%E", "%23", '" />', "hashtag"):
        assert bad not in out, f"标签残片进了抽取入参：{bad!r} → {out[-80:]!r}"
    assert "中芯国际受益于扩产" in out, "清洗把正文一起吃掉了"


def test_share_link_tag_is_stripped_before_compression() -> None:
    """分享链接标签（带百分号编码地址）同样要剥掉 —— 它是**平台域名**。"""
    out = S.extraction_input(_NOTE_WITH_SHARE_LINK)
    for bad in ("zsxq", "%2F", "<e", "http"):
        assert bad not in out, f"平台地址进了抽取入参：{bad!r}"
    assert out.endswith("后文还在"), "标签之外的正文被删掉了"


def test_cleaning_happens_before_the_head_tail_cut() -> None:
    """★★ 这条锁的就是那个实测缺陷：**清洗必须在压缩之前**。

    构造一条 > 600 字的原文，让**结尾**是一大段标签。若先压缩再清洗，
    尾部窗口里就只剩 `%E7%89%87…` 这种残片（头半截被切掉了）；
    先清洗则尾部窗口里是干净的正文 —— 两件事用同一个函数、两种顺序，
    结果必须不同（否则这条用例证明不了顺序）。
    """
    body = "半导体设备景气上行，订单饱满。" * 30          # 远超过 600 字
    raw = body + '<e type="hashtag" title="' + ("%E5%A4%A7" * 80) + '" />'
    assert len(raw) > EXTRACT_MAX_CHARS

    cleaned_first = S.extraction_input(raw)
    # 反事实：先压缩再清洗（这是修复前的顺序）
    wrong_order = extraction_text(raw)

    assert "%E5%A4%A7" not in cleaned_first, "先清洗之后尾部窗口里不该有编码残片"
    assert "。" in cleaned_first[-EXTRACT_HALF_CHARS:], "尾部窗口里没有正文"
    # 顺序反了会把残片留在尾部窗口里 —— 这正是当初的线上表现
    assert "%E5%A4%A7" in wrong_order, "反事实没复现出来，用例失去意义"


def test_short_text_is_not_compressed() -> None:
    """≤600 字原样返回（只是清洗过）—— 不引入省略标记。"""
    out = S.extraction_input("存储芯片涨价，中芯国际扩产。")
    assert out == "存储芯片涨价，中芯国际扩产。"
    assert "省略" not in out


def test_long_text_keeps_everything_for_segmentation() -> None:
    """★★ 第四轮：超过 600 字的原文**原样进入抽取入参**（不再取两头）。

    第一~三轮这里是"取两头各 300 字 + 省略标记"。那个做法有一个**实测的硬
    天花板**：一条 3452 字的《碳化硅材料专题会议》取两头得到 591 字，
    而 `天岳先进`（688234）与 `第三代半导体` 落在被切掉的中间 ~2500 字里 ——
    模型给出的实体字段**全空**。它没失败，它**没看见**。

    压缩改由 `tone.segment_text` 完成：切成多段、**每段都送进模型**。
    所以这个函数的契约变成"只清洗、不压缩"，本用例锁住这一点 ——
    若有人在读取侧"顺手"加一次截断，中间那段原文就再也到不了模型，
    而表现只是"实体又变少了"，没有任何报错。
    """
    head = "结论：半导体设备景气上行。"
    middle = "碳化硅衬底环节由天岳先进主导。"
    tail = "综上，推荐关注存储芯片与设备环节。"
    out = S.extraction_input(head + "填充内容。" * 200 + middle + tail)
    assert out.startswith(head), "开头被切掉了"
    assert out.endswith(tail), "结尾被切掉了（研报的标的与结论都在结尾）"
    assert middle in out, "中间那段被切掉了 —— 实体恰好常在那里"
    assert "省略" not in out, "全文入参里不该有省略标记（没有省略任何内容）"


# ======================================================================
# `extract_text` 不出接口（顶层 + 收容组内）
# ======================================================================

def test_extract_text_never_reaches_the_browser() -> None:
    """★★ 内部键 `extract_text` 不许出现在 `to_public()` 的任何一层。

    顶层与 `group_items` 都要剥：收容组的子条目是 `_group_row` 生成的
    **另一个 dict**，只剥顶层等于没剥（实测 21 条笔记全在组内）。
    """
    feed = S.IntelFeed(items=[
        {"title": "普通条目", "summary": "正文",
         "extract_text": "内部全文：这是不该出接口的全文内容"},
        {"title": "收容组", "is_group": True, "group_count": 2,
         "group_items": [
             {"title": "组内条目", "summary": "正文",
              "extract_text": "内部全文：组内那条也不该出接口"},
         ]},
    ])
    pub = feed.to_public()
    blob = json.dumps(pub, ensure_ascii=False)
    assert "extract_text" not in blob, "内部键名泄漏到接口"
    assert "不该出接口" not in blob, "内部全文内容泄漏到接口"
    # 正常字段照旧（别为了剥一个键把菜单整份删了）
    assert pub["items"][0]["title"] == "普通条目"
    assert pub["items"][1]["group_items"][0]["title"] == "组内条目"


class _StubItem:
    def __init__(self, payload: dict) -> None:
        self._p = payload

    def to_public(self) -> dict:
        return dict(self._p)


class _StubTopic:
    def __init__(self, title: str, text: str, content_hash: str) -> None:
        self.title = title
        self.text = text
        self.created_at = datetime.now(timezone.utc).astimezone().strftime(
            "%Y-%m-%dT%H:%M:%S+0800")
        self.content_hash = content_hash


class _StubIncremental:
    def __init__(self, topics: list[_StubTopic]) -> None:
        self.topics = topics
        self.watermark = "2026-09-25T00:00:00+0800"
        self.new_count = len(topics)
        self.truncated = False
        self.newest = "2026-09-25T23:59:59+0800"


def test_build_feed_sets_cleaned_extract_text_and_still_hides_it(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 端到端：知识星球那条旁路产出的 `extract_text` 是**清洗过**的正文，
    而且从 `to_public()` 里一个字都出不去。

    这条同时守住两个真实事故：
      · 标签残片进了抽取入参（正文一个字没喂进去）
      · 全文随接口发给浏览器（正好抵消 `summary` 的展示截断）
    """
    note = _StubTopic("【天风电子】半导体设备",
                      "半导体设备景气上行，存储芯片涨价，中芯国际受益于扩产。"
                      "机构上调盈利预测，隆基绿能承压。"
                      '<e type="web" href="https%3A%2F%2Fwx.zsxq.com%2Fx">'
                      "网页链接</e>",
                      "hash-note-1")

    async def _fake_fetch_all(**_kw):
        return [], {}

    monkeypatch.setattr(
        "src.infrastructure.connectors.intel_sources.fetch_all", _fake_fetch_all)
    monkeypatch.setattr(
        "src.infrastructure.connectors.zsxq_incremental.fetch_incremental",
        lambda: _StubIncremental([note]))
    monkeypatch.setattr(
        "src.infrastructure.connectors.zsxq_incremental.save_watermark",
        lambda *a, **k: None)

    feed = asyncio.run(S.build_feed(limit=50, group_undetermined=False))
    rows = [it for it in feed.items if it.get("content_hash") == "hash-note-1"]
    assert rows, f"笔记没进流水线：{[i.get('title') for i in feed.items]}"
    internal = rows[0].get("extract_text") or ""
    assert internal, "抽取入参没被挂上去（tone_job 会退回 260 字展示摘要）"
    for bad in ("zsxq", "%2F", "<e", "href"):
        assert bad not in internal, f"标签残片进了抽取入参：{bad!r}"

    blob = json.dumps(feed.to_public(), ensure_ascii=False)
    assert "extract_text" not in blob
    assert "zsxq" not in blob and "%2F" not in blob, "平台地址泄漏到接口"


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
