"""知识星球内容触发规则：**两条触发条件是 OR**，且**机构名只展示不触发**。

> 用户口径（2026-09-25）：
>   "知识星球不看置信度，看内容里有没有'人'和'机构'，还有这条消息被本地模型
>    分析出是有利空或利多偏向的，都要告警。"
>   "三条触发条件是并列的，不是必须同时满足。"
>
> 用户改口径（2026-10-01，**本条覆盖上面那句**）：
>   "券商名不一定要告警，但是一定要前端输出信息。"

## 最终语义（本文件逐条钉住）

    触发（OR，任一成立即弹）   (a) 分析师名单命中   (b) 模型给出 偏多/偏空
    展示（不触发，但必须出接口）机构名（「XX证券」）→ 前端"机构：中泰证券"

## ⚠️ 为什么每条触发条件都要一个"只满足它"的用例

只断言"两条都真 ⇒ 弹"的测试在 **AND 语义下同样会通过** —— 也就是说
它对最可能的那个缺陷（写成 AND）**完全无感**。而 AND 的表现是
"几乎什么都不弹"，恰恰是这次要修的那个缺陷的翻版。

同理，"只有券商名"必须有一个**成对**用例（不弹 **且** 机构名在导出里）：
两个要求写在同一个用例里，将来任何一次重构都不可能"满足一个、破坏另一个"
而不被发现。
"""

from __future__ import annotations

import pytest

from src.domain.intel import alert_bridge as B
from src.domain.intel import alert_rules as R


def _item(*, title: str = "", summary: str = "", tone: str = "",
          has_tone: bool = False, extra_text: str = "",
          score: int = 58) -> dict:
    """一条最小情报（形状与 `IntelItem.to_public()` 一致）。

    ⚠️ 默认 `score=58` —— 那是**知识星球真实条目的实测分数**（15 条全是 58）。
    用例默认就拿这个分数跑：这样"信度闸门被旁路"这件事是被**每一条**用例
    顺带验证的，而不是只在一条专门的用例里。
    """
    it: dict = {
        "kind": "research_note",
        "title": title,
        "summary": summary,
        "credibility": {"score": score},
        "tone": {"tone": tone or "未定", "has_tone": has_tone,
                 "neutral": bool(has_tone) and tone == "中性"},
    }
    if extra_text:
        it["extract_text"] = extra_text
    return it


def _export(item: dict) -> dict:
    """模拟 `IntelItem.to_public()` 的**白名单导出结果**（展示侧的判据）。

    ⚠️ 为什么要在这里模拟而不是直接调真实现：真实现需要 `IntelItem` 的
    全部必填字段，而本文件关心的是"机构名会不会出现在导出里"这个**事实**。
    真正走真实现的那一半在 `tests/unit/test_intel_institutions_export.py`
    （那边断言 `IntelItem.to_public()` 与 `IntelFeed.to_public()` 的输出键）。
    两边合起来才闭环：这里钉"名字对不对"，那边钉"字段出不出得去"。
    """
    out = {k: v for k, v in item.items() if k != "extract_text"}
    out["institutions"] = R.institutions(item)
    out["analysts"] = R.analysts(item)
    return out


# ======================================================================
# 名单与形态本身（用户点名的字面口径）
# ======================================================================

def test_analyst_watchlist_is_exactly_what_the_user_named() -> None:
    """名单**逐字**是用户点名的那六个。

    ⚠️ 这条用例的价值在于"改名单必须先改测试"：名单是**手工维护**的，
    而它的全部价值就是"命中即原文逐字有这个名字"。被人顺手加一个
    模糊词（"孙"、"券商"）会让弹窗开始报出用户核对不到的东西。
    """
    assert R.ANALYST_WATCHLIST == (
        "孙潇雅", "赵宇阳", "武超则", "陈果", "刘晨明", "洪灏",
    )


@pytest.mark.parametrize("name", ["孙潇雅", "赵宇阳", "武超则", "陈果",
                                  "刘晨明", "洪灏"])
def test_every_listed_analyst_is_detected(name: str) -> None:
    hits = R.content_hits(_item(summary=f"……{name}认为行业景气度上行……"))
    assert [h.name for h in hits] == [name]
    assert hits[0].kind == R.TRIGGER_ANALYST


@pytest.mark.parametrize("broker", ["中泰证券", "天风证券", "国金证券",
                                    "国海证券", "国联民生证券"])
def test_broker_pattern_matches_real_names(broker: str) -> None:
    """用户口径"出现 XX 证券" → `[\\u4e00-\\u9fff]{2,8}证券`。"""
    hits = R.content_hits(_item(summary=f"【{broker}】维持买入评级……"))
    assert [h.name for h in hits] == [broker]
    assert hits[0].kind == R.TRIGGER_BROKER


def test_broker_pattern_verbatim_is_the_documented_one() -> None:
    """形态**逐字**就是用户给的那条口径（改它会同时改掉召回与误报）。

    ⚠️ 两侧**刻意不加**边界断言，这条用例把"别顺手加"钉住：
    加左界会让 `内 × 7 + 国金证券` 静默漏掉（见下一条用例）；
    加右界会让真实语料里的 `://国金证券研究服务` 直接归零
    （"证券"后面紧跟"研"是**最常见**的形态）。
    """
    assert R.BROKER_PATTERN.pattern == r"[\u4e00-\u9fff]{2,8}证券"


def test_broker_pattern_does_not_drift_into_a_broader_class() -> None:
    """`研究所` **不**算机构命中（用户点名的形态只有"XX证券"）。

    实测语料里"研究所/研究员"大量出现在泛指表述里，放进来会明显放大命中量。
    它与 `credibility._BROKER_SIGNATURE`（含"研究所|研究院"，服务的是**可信度
    提档**）是两件事 —— 这条用例把两者的边界钉住，防止有人"顺手统一"。
    """
    assert R.content_hits(_item(summary="某研究所认为需求回暖")) == []


# ======================================================================
# 机构名的提取质量（要拿去前端显示，所以必须逐字可核对）
# ======================================================================

@pytest.mark.parametrize("text,expected", [
    # 真实语料原样：命中停在标点处，是干净的名字
    ("【国金证券】研究服务：上调盈利预测", "国金证券"),
    # 真实语料里的另一处：跟在冒号后面
    ("某公司公告：印度国家证券存管有限公司", "印度国家证券"),
    # 无分隔符的全称：命中到"证券"为止（后面的"股份有限公司"不算名字）
    ("国联民生证券股份有限公司发布公告", "国联民生证券"),
])
def test_broker_name_is_a_verbatim_substring(text, expected) -> None:
    """机构名必须是**原文逐字子串**（前端要显示"机构：XX"，用户拿去核对）。

    ⚠️ 为什么这条是硬纪律：显示一个原文里不存在的名字，用户会认为系统在编。
    所以规整**只在命中串内部切**（不做词表、不做模糊匹配），
    切出来的必然仍是原文子串。
    """
    hits = R.content_hits(_item(summary=text))
    assert [h.name for h in hits] == [expected]
    assert expected in text


def test_broker_hit_without_any_separator_stays_verbatim_and_located() -> None:
    """无分隔符长串：名字**仍然逐字可核对**，且位置精确。

    ⚠️ 这里原先如实钉住一个**已知且可接受**的粗糙面：正文是一整串没有标点的
    汉字时，`[\\u4e00-\\u9fff]{2,8}证券` 会多带几个前缀字，而当时"按词边界切开
    需要一份券商名录 —— 用户明说不要建大名单"。

    **那条前提已经变了**（2026-10-01）：用户点名要"搜索国内大型研究机构的
    名字"，于是 `BROKER_BASES` + 研究语境后缀的**署名形态**上线，它能在长串里
    认出 `国金` + `证券` 并给出干净的名字。所以这条用例的断言保持不变
    （逐字、以"证券"结尾、offset 是真实起点），只是名字从
    "内内内内内内国金证券"变成了"国金证券" —— 粗糙面在**名录覆盖到**的地方消失了，
    名录之外的机构名照旧走粗匹配（那条路没有被削弱，见
    `test_broker_found_at_any_distance_from_a_separator`）。
    """
    text = "内内内内内内内国金证券研究服务"
    hits = R.content_hits(_item(summary=text))
    assert len(hits) == 1
    name, offset = hits[0].name, hits[0].offset
    assert name in text
    assert name.endswith("证券") and 4 <= len(name) <= 10
    # `offset` 不是"命中处随便一个位置"，而是这个名字在原文里的起点
    assert text[offset:offset + len(name)] == name
    # 名录覆盖到的机构名要给**干净**的名字（不是带前缀的那一版）
    assert name == "国金证券"


@pytest.mark.parametrize("pad", [0, 1, 6, 7, 8, 9, 16, 40])
def test_broker_found_at_any_distance_from_a_separator(pad: int) -> None:
    """★ 回归用例：左侧连续汉字**任意长度**都要命中。

    第一版加过 `(?<![\\u4e00-\\u9fff])`，实测 `pad >= 7` 命中为空 ——
    而表现不是"少带两个字"，是**这条真实笔记静默不弹、前端也没有机构名**
    （漏报没有任何线索）。所以这条边界必须被钉住。
    """
    text = "内" * pad + "国金证券研究服务"
    hits = R.content_hits(_item(summary=text))
    assert [h.kind for h in hits] == [R.TRIGGER_BROKER]
    assert hits[0].name in text and hits[0].name.endswith("证券")


def test_broker_name_returns_empty_without_the_suffix() -> None:
    """认不出"证券"时返回空串（调用方据此**丢掉**命中，不写一个假机构名）。"""
    assert R.broker_name("") == ""
    assert R.broker_name("研究所") == ""


# ======================================================================
# 扫全文（券商署名常在后段）
# ======================================================================

def test_hit_in_tail_of_full_text_is_found_and_marked_not_visible() -> None:
    """真实实测：`国金证券` 在全文第 695 字，而展示摘要只到 260 字。

    ⚠️ 两个断言缺一不可：
      · 命中**必须**被发现（否则这类带署名研报的机构信息整批丢掉）
      · 但必须标记"展示摘要未显示"（否则理由里写一个用户在卡片上
        找不到的名字，他会认为系统在编 —— 那是比漏弹更坏的信任损失）

    形态照真实语料来：机构名在句首、后面紧跟"研究服务"（实测原样）。
    """
    body = "光伏行业深度：" + "正文内容" * 160      # 约 650 字
    full = f"{body}【国金证券】研究服务：上调盈利预测"
    assert full.find("国金证券") > R.SUMMARY_CLIP
    item = _item(title="光伏行业深度", summary=body[:R.SUMMARY_CLIP],
                 extra_text=full)
    hits = R.content_hits(item)
    assert [h.name for h in hits] == ["国金证券"]
    assert hits[0].in_summary is False
    assert "展示摘要未显示" in hits[0].describe()
    # 前端那一侧照样要拿到（用户口径："一定要前端输出信息"）
    assert _export(item)["institutions"] == ["国金证券"]


def test_hit_inside_display_excerpt_is_marked_visible() -> None:
    """摘要里就能看见的命中，理由里**不加**"未显示"的提示（不制造噪音）。"""
    hits = R.content_hits(_item(summary="【中泰证券】汽车行业周报，销量超预期"))
    assert hits[0].in_summary is True
    assert hits[0].describe() == "命中机构「中泰证券」"


def test_display_only_item_still_scanned_without_extract_text() -> None:
    """没有 `extract_text`（接口来的 dict / 其它来源）时退回展示文本，不报错。"""
    item = _item(summary="孙潇雅最新观点：看好算力产业链")
    assert "extract_text" not in item
    hits = R.content_hits(item)
    assert [h.name for h in hits] == ["孙潇雅"]
    assert hits[0].source == "display"


# ======================================================================
# ★ OR 语义：两条触发条件各有一个"只满足它"的用例
# ======================================================================

def test_or_a_analyst_only_alerts_without_any_model_verdict() -> None:
    """★ 只满足 (a) 分析师 —— 模型**没有**方向（`has_tone=False`）。

    ⚠️ 信度取真实值 58（低于知识星球闸门 74），`has_tone` 为假 ——
    两者都不该影响这条判定。
    """
    item = _item(summary="孙潇雅认为该环节供需格局改善，建议关注龙头企业",
                 score=58)
    assert item["tone"]["has_tone"] is False
    assert B.is_alertable(item) is True
    assessment = B.build_assessment(item, B.reason_of(item))
    assert assessment is not None
    assert "孙潇雅" in assessment.impact_path


def test_or_b_direction_only_alerts_without_any_analyst_name() -> None:
    """★ 只满足 (b) 模型方向 —— 名单为假、机构名也为假。"""
    item = _item(summary="某公司中标大额订单，机构上调盈利预测",
                 tone="偏多", has_tone=True, score=58)
    reason = B.reason_of(item)
    assert reason.triggers() == [R.TRIGGER_DIRECTION]
    assert B.is_alertable(item) is True


def test_or_c_nothing_matched_does_not_alert() -> None:
    """★ 两条都不满足 ⇒ 不弹。**这条挡的是"OR 被写成恒真"**。

    实测背景：知识星球大量条目是宏观/海外快讯（"某指数报跌 0.15%"），
    既没有点名要盯的分析师、模型也没判出方向。它们不该弹 ——
    否则这个来源会退化成"每条都弹"，那正是 `alert_bridge` 模块存在
    要避免的事（情报流一天几百条）。
    """
    item = _item(summary="隔夜美股三大指数收跌，成交量较前一交易日小幅放大")
    assert B.is_alertable(item) is False
    assert B.reason_of(item).fires is False
    assert B.build_assessment(item) is None


# ======================================================================
# ★★ 改口径后的成对用例：只有券商名 ⇒ 不弹，但机构名必须出前端
# ======================================================================

def test_broker_only_does_not_alert_but_is_exported_for_the_frontend() -> None:
    """★★ 用户 2026-10-01 口径："券商名不一定要告警，但是**一定要**前端输出信息。"

    这一个用例同时钉住两件事（**故意写在同一个用例里**）：

        不弹        只有「中泰证券」、既无分析师名也无模型方向 → 不告警
        要显示      「中泰证券」必须出现在导出条目里（前端渲染"机构：中泰证券"）

    为什么合成一个用例：拆成两条的话，将来某次重构完全可能
    "满足一条、破坏另一条"而两条测试各自通过（比如把机构名从导出里删掉、
    同时把它加进触发条件 —— 两条测试都还是绿的）。合在一起，
    任何一侧被破坏都会让这一条红。

    ⚠️ 另外断言 `kind`/`is_chip` 层面的语义：机构名是**内容里的机构**，
    与"这条来自知识星球"那种**渠道身份**不是一回事（后者永不出现）。
    """
    item = _item(summary="【中泰证券】发布行业周报，重申板块配置价值",
                 score=58)
    # ① 不弹（机构名不是触发条件）
    assert B.is_alertable(item) is False
    assert B.reason_of(item).fires is False
    assert B.build_assessment(item) is None
    # ② 但机构名必须出得去（前端要看到"机构：中泰证券"）
    exported = _export(item)
    assert exported["institutions"] == ["中泰证券"]
    # ③ 且不能被误当成"来源平台"：导出里**没有**任何渠道标识
    assert "source_name" not in exported
    assert "platform" not in exported


def test_broker_plus_direction_alerts_and_reason_names_only_the_trigger() -> None:
    """"券商名 + 模型方向"⇒ 弹；理由只写**触发**的那一条（方向），不堆机构名。

    理由里堆一串机构名会让人以为"是这些机构让它弹的"，而实际触发的是模型方向。
    机构名走 `institutions_display()`（前端那一侧），两条信息各就各位。
    """
    item = _item(summary="【中泰证券】看好该板块，维持超配评级",
                 tone="偏空", has_tone=True, score=58)
    reason = B.reason_of(item)
    assert reason.triggers() == [R.TRIGGER_DIRECTION]
    assert reason.fires is True
    assert reason.describe() == "模型判为利空"
    assert reason.institutions_display() == ["中泰证券"]


# ======================================================================
# 展示侧的机构名 / 分析师名
# ======================================================================

def test_institutions_collects_broker_names_in_order() -> None:
    """多个机构名按**出现顺序**返回（前端直接照着渲染，不做二次排序）。"""
    item = _item(summary="【中泰证券】上午纪要，【天风证券】下午跟踪")
    assert R.institutions(item) == ["中泰证券", "天风证券"]


def test_institutions_never_contains_analyst_names() -> None:
    """机构列表里**不能**混进分析师名（前端会加上"机构："这个标签）。

    混进去的表现是界面上写着"机构：孙潇雅" —— 而孙潇雅是**人**不是机构。
    """
    item = _item(summary="孙潇雅（中泰证券）认为景气度上行")
    assert R.institutions(item) == ["中泰证券"]
    assert R.analysts(item) == ["孙潇雅"]


def test_institutions_scans_display_text_even_when_full_text_exists() -> None:
    """全文与展示文本**都扫**：展示多给一个名字没有代价，漏掉才是信息缺失。"""
    item = _item(summary="【天风证券】周报", extra_text="正文" * 100)
    # 全文里没有机构名；展示摘要里的必须仍被收到
    assert R.institutions(item) == ["天风证券"]


# ======================================================================
# 方向词映射（利多 = 偏多、利空 = 偏空）
# ======================================================================

@pytest.mark.parametrize("tone,has_tone,expected", [
    ("偏多", True, "利多"),
    ("偏空", True, "利空"),
    ("中性", True, ""),      # 判定为中性 = 没有偏向
    ("未定", False, ""),     # 未定 = 我们不知道
    ("", False, ""),
])
def test_direction_word_maps_tone_to_user_wording(tone, has_tone, expected) -> None:
    """⚠️ 映射**明确确认**：利多 = 偏多、利空 = 偏空（既有 `has_tone` 判据）。

    不额外要求 `bullish`/`bearish` 桶非空 —— 那是"原文里明写的标的"，
    与"模型的语气判断"是两件事（用户口径："被本地模型分析出是有利空或
    利多偏向的"）。
    """
    assert R.direction_word({"tone": tone, "has_tone": has_tone}) == expected


def test_legacy_tone_row_without_has_tone_key_still_works() -> None:
    """老存储行可能没有 `has_tone` 键 —— 按字面值判，不能静默变成"没方向"。"""
    assert R.direction_word({"tone": "偏多"}) == "利多"
    assert R.direction_word({"tone": "偏空"}) == "利空"
    assert R.direction_word({"tone": "未定"}) == ""


def test_neutral_flag_beats_has_tone() -> None:
    """`neutral=True` 的行即使 `tone` 恰好是"偏多"也没有方向（既有口径）。"""
    assert R.direction_word(
        {"tone": "偏多", "has_tone": True, "neutral": True}) == ""


# ======================================================================
# 健壮性：规则层绝不抛异常
# ======================================================================

@pytest.mark.parametrize("bad", [
    {}, {"title": None, "summary": None},
    {"extract_text": 123, "summary": ["not", "a", "str"]},
    {"summary": "xx", "tone": "不是 dict"},
])
def test_content_hits_never_raises(bad) -> None:
    """规则层跑在调度任务里 —— 一次异常就是"这轮不弹"且没有任何线索。"""
    assert isinstance(R.content_hits(bad), list)
    assert isinstance(R.institutions(bad), list)
    assert isinstance(R.analysts(bad), list)


def test_describe_is_empty_when_nothing_matched() -> None:
    """没有命中时理由必须是空串（不能给一句"命中机构「」"）。"""
    assert R.AlertReason().describe() == ""
    assert R.AlertReason().triggers() == []
    assert R.AlertReason().institutions_display() == []
