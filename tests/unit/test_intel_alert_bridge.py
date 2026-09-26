"""情报 → 告警桥的闸门与字段映射。

用户口径（2026-09-25）：
    "为什么不把情报流、舆情热度 有明显利空或利多的信息，都通过事件告警弹出。"
    "明确方向 + 信度达标 + 仅 high 级；跨源同文合并成一条；
     非交易时段只进列表不弹。"

用户改口径（2026-10-01，**只针对知识星球**）：
    "知识星球是不是弹窗和置信度无关，取决于抓取的信息内容"
    "券商名不一定要告警，但是一定要前端输出信息。"

这个文件把那些话拆成逐条可执行的断言。为什么值得单测：
**弹窗是最打扰用户的功能**，闸门松一格的表现是"半夜一直响"，
而那种问题在开发机上看不出来（开发时很少有人在非交易时段测）。

知识星球特有的那几条（内容规则、信度旁路、强制抬档）在这里测；
规则本身（名单、券商形态、OR 语义）在
`tests/unit/test_intel_alert_content_rule.py`。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.domain.alerts.service import in_trading_window
from src.domain.intel import alert_bridge as B


def _item(tone: str, score: int, *, codes=(), industry: str = "",
          title: str = "某公司公告重大合同",
          published_at: str = "2026-09-25 10:00:00",
          alias: str = "newswire-cls", chash: str = "h1",
          kind: str = "", summary: str = "正文" * 40) -> dict:
    it = {
        "title": title,
        "summary": summary,
        "published_at": published_at,
        "source_alias": alias,
        "content_hash": chash,
        "codes": list(codes),
        "industry": industry,
        "credibility": {"score": score},
        "tone": {"tone": tone, "has_tone": tone in ("偏多", "偏空"),
                 "neutral": tone == "中性"},
    }
    if kind:
        it["kind"] = kind
    return it


def _zsxq(tone: str, score: int, *, summary: str = "正文" * 40,
          bullish=None, bearish=None, chash: str = "z1",
          published_at: str = "2026-09-25 10:00:00") -> dict:
    """知识星球条目（真实形态：`score=58`、`kind=research_note`）。

    ⚠️ 默认 `has_tone=False`（模型没判出方向）—— 那是这个来源最常见的状态，
    而这次修改要解决的正是"它以前永远不弹"。
    """
    it = _item(tone, score, kind="research_note", chash=chash,
               alias="research-note-zsxq", summary=summary,
               published_at=published_at,
               title="【中泰证券】行业深度")
    it["tone"] = {"tone": tone,
                  "has_tone": tone in ("偏多", "偏空"),
                  "neutral": tone == "中性"}
    if bullish is not None:
        it["tone"]["bullish"] = bullish
    if bearish is not None:
        it["tone"]["bearish"] = bearish
    return it


# ======================================================================
# 闸门
# ======================================================================

@pytest.mark.parametrize("tone,score,expected", [
    ("偏多", 94, True),      # 官方披露 + 明确方向
    ("偏空", 84, True),      # 持牌机构研报
    ("偏多", 74, True),      # 权威财经媒体（门槛值，闭区间）
    ("偏多", 73, False),     # 差一分 → 不弹
    ("偏多", 54, False),     # 财经自媒体：方向判断本身不可靠
    ("未定", 94, False),     # "我们不知道" → 绝不能弹
    ("中性", 94, False),
])
def test_alert_gates(tone, score, expected) -> None:
    assert B.is_alertable(_item(tone, score)) is expected


def test_neutral_stored_row_is_not_alertable() -> None:
    """`neutral=True` 的行即使 `tone` 恰好是"偏多"也不能弹。"""
    x = _item("偏多", 94)
    x["tone"]["neutral"] = True
    assert not B.is_alertable(x)


# ======================================================================
# 方向 → 分数映射（"仅 high 级"靠它落到既有引擎的档位上）
# ======================================================================

def test_bear_maps_to_risk_bull_maps_to_opportunity() -> None:
    a_bear = B.build_assessment(_item("偏空", 84))
    a_bull = B.build_assessment(_item("偏多", 94))
    assert a_bear is not None and a_bull is not None
    assert a_bear.sentiment == "negative" and a_bear.risk_score == 84.0
    assert a_bear.opportunity_score == 0.0
    assert a_bull.sentiment == "positive" and a_bull.opportunity_score == 94.0
    assert a_bull.risk_score == 0.0


def test_confidence_is_derived_from_credibility() -> None:
    """置信度 = 可信度/100 —— "信度达标"由既有引擎统一裁决，
    桥里不另写一套阈值（两套阈值必然漂移）。"""
    a = B.build_assessment(_item("偏多", 95))
    assert a is not None and abs(a.confidence - 0.95) < 1e-9


def test_no_assessment_without_direction() -> None:
    assert B.build_assessment(_item("未定", 94)) is None
    assert B.build_assessment(_item("中性", 94)) is None


# ======================================================================
# 去重键
# ======================================================================

def test_cross_source_same_text_shares_content_key() -> None:
    """用户要的"跨源同文合并成一条"落在这里：`content_key` **不含来源**。"""
    a = _item("偏多", 94, alias="newswire-cls", chash="a")
    b = _item("偏多", 94, alias="newswire-em", chash="b")
    assert B.build_event(a).content_key == B.build_event(b).content_key
    # 但 event_key 按内容指纹走，两条不同内容仍是两个事件
    assert B.build_event(a).event_key != B.build_event(b).event_key


def test_different_day_same_title_is_a_new_alert() -> None:
    """同标题不同日期**不该**互相抑制 —— 否则同一只票的连续公告只弹第一条。"""
    a = _item("偏多", 94, published_at="2026-09-24 10:00:00", chash="a")
    b = _item("偏多", 94, published_at="2026-09-25 10:00:00", chash="b")
    assert B.build_event(a).content_key != B.build_event(b).content_key


# ======================================================================
# 事件类型映射（枚举里没有 "news"）
# ======================================================================

@pytest.mark.parametrize("kw,expected", [
    ({"codes": ["600519"]}, "stock"),
    ({"industry": "银行"}, "sector"),
    ({}, "policy"),
])
def test_event_type_falls_back(kw, expected) -> None:
    assert B.build_event(_item("偏多", 94, **kw)).event_type.value == expected


# ======================================================================
# 非交易时段只进列表不弹
# ======================================================================

@pytest.mark.parametrize("moment,expected", [
    # 2026-09-25 是周五
    (datetime(2026, 9, 25, 10, 0), True),    # 连续竞价
    (datetime(2026, 9, 25, 9, 20), True),    # 集合竞价：隔夜消息定价窗口
    (datetime(2026, 9, 25, 15, 3), True),    # 盘后 5 分钟复盘窗口
    (datetime(2026, 9, 25, 15, 30), False),  # 盘后深夜：不弹
    (datetime(2026, 9, 25, 12, 0), False),   # 午休：不弹
    (datetime(2026, 9, 25, 8, 0), False),    # 盘前太早：不弹
    (datetime(2026, 9, 25, 23, 30), False),  # 半夜：绝不弹
    (datetime(2026, 9, 26, 10, 0), False),   # 周六
    (datetime(2026, 9, 27, 10, 0), False),   # 周日
])
def test_trading_window_gate(moment, expected) -> None:
    """弹窗时段闸门。

    ⚠️ 15:03 那一条是**回归用例**：`session_state()` 在 15:00 之后直接
    返回 `closed`（没有 `post_close` 这个状态），第一版按它判，
    结果是"盘后 5 分钟永远不弹"。现在按分钟数与 `POST_CLOSE` 自行判定。
    """
    assert in_trading_window(moment) is expected


# ======================================================================
# ★ 知识星球：信度旁路 + 内容规则触发（用户 2026-09-25 改判据）
# ======================================================================

def test_zsxq_bypasses_credibility_gate() -> None:
    """实测背景：知识星球 15 条真实笔记**全部 58 分**，闸门是 74 ——
    也就是说这个来源在结构上**永远不可能**弹一次（桥写得再完整也没用）。

    用户改判据："知识星球不看置信度，看内容里有没有'人'…"。
    这条用例断言：58 分 + 明确方向 ⇒ 弹（同一条内容在其它来源仍不弹）。
    """
    z = _zsxq("偏多", 58)
    other = _item("偏多", 58)
    assert B.is_alertable(z) is True
    assert B.is_alertable(other) is False     # 其它来源的闸门**没动**


def test_zsxq_low_credibility_does_not_need_to_be_high_to_alert() -> None:
    """18 分（实测最低的一条）同样要能弹 —— 旁路是彻底的，不是"降一点门槛"。"""
    assert B.is_alertable(_zsxq("偏空", 18)) is True


def test_zsxq_analyst_name_alerts_even_without_any_model_verdict() -> None:
    """用户点名的那条路：出现分析师名 ⇒ 弹，**不看信度、不看模型方向**。

    名字必须**逐字在原文里**（这里用真实名单里的一个）。
    """
    z = _zsxq("未定", 58,
              summary="孙潇雅：看好该环节供需格局改善，建议关注龙头企业")
    assert B.is_alertable(z) is True
    reason = B.reason_of(z)
    assert reason.triggers() == ["analyst"]
    # 理由里点出是**谁**让它弹的（弹窗要能回答"为什么弹我"）
    assert "孙潇雅" in reason.describe()
    a = B.build_assessment(z, reason)
    assert a is not None
    assert "孙潇雅" in a.impact_path


def test_zsxq_broker_name_alone_is_recorded_but_does_not_alert() -> None:
    """用户 2026-10-01 改口径："券商名不一定要告警，但是一定要前端输出信息。"

    ⚠️ 成对断言（**故意合成一条**）：不弹 **且** 机构名仍被识别出来。
    拆成两条的话，将来重构可能"满足一个、破坏另一个"而两条都还是绿的。
    """
    z = _zsxq("未定", 58, summary="【中泰证券】发布行业周报，重申板块配置价值")
    assert B.is_alertable(z) is False
    assert B.reason_of(z).fires is False
    # 机构名照样识别得到（出接口那一侧由 `IntelItem.to_public()` 负责）
    assert B.reason_of(z).institutions_display() == ["中泰证券"]


def test_zsxq_forced_score_reaches_the_engine_high_tier() -> None:
    """⚠️ 旁路信度**必须同时抬分**，否则引擎两道闸门会静默吃掉告警。

        alert_confidence_min = 0.70    58/100 = 0.58 → `evaluate` 直接返回 None
        alert_opp_high       = 80      机会分 58 < 80 → 连 medium 都不是

    这条用例断言抬分**确实发生**且确实过关（跑真引擎，不只是看数字）。
    """
    from src.domain.alerts.thresholds import AlertEngine

    z = _zsxq("偏多", 58)
    a = B.build_assessment(z, B.reason_of(z))
    assert a is not None
    assert a.confidence >= 0.70
    alert = AlertEngine().evaluate(B.build_event(z), a)
    assert alert is not None
    assert alert.alert_level.value == "high"
    assert alert.alert_type.value == "opportunity"


def test_zsxq_bear_verdict_becomes_a_high_risk_alert() -> None:
    """偏空（= 利空）走风险侧；80 ≥ alert_risk_high(75) ⇒ 仍是 high 档。"""
    from src.domain.alerts.thresholds import AlertEngine

    z = _zsxq("偏空", 58)
    a = B.build_assessment(z, B.reason_of(z))
    assert a is not None
    assert a.sentiment == "negative" and a.risk_score > 0
    alert = AlertEngine().evaluate(B.build_event(z), a)
    assert alert is not None
    assert alert.alert_level.value == "high"
    assert alert.alert_type.value == "risk"


def test_zsxq_without_stocks_is_marked_weaker_in_the_alert_text() -> None:
    """**无标的必须如实标注**（用户口径："目的就是找到那些股被唱多，唱空"）。

    一条说"利多"却不点名个股的告警，与一条点名 `天岳先进 688234` 的告警
    不是一回事。都显示成同一个样子，用户会以为系统找到了票。
    """
    z = _zsxq("偏多", 58, summary="行业景气度上行，看好板块配置价值" * 3)
    a = B.build_assessment(z, B.reason_of(z))
    assert a is not None
    assert a.affected_stocks == []
    assert B.NO_STOCK_FLAG in a.summary


def test_stock_bearing_alert_names_the_stock_first() -> None:
    """**个股优先**：方向桶里的个股（名字 + 词表代码）是告警的主语。"""
    z = _zsxq("偏多", 58,
              summary="天岳先进（688234）碳化硅衬底放量",
              bullish={"industries": ["第三代半导体"],
                       "stocks": [{"name": "天岳先进", "code": "688234",
                                   "count": 2}]})
    a = B.build_assessment(z, B.reason_of(z))
    assert a is not None
    assert a.affected_stocks and a.affected_stocks[0].name == "天岳先进"
    assert a.affected_stocks[0].code == "688234"
    # 有标的就不该再打"信号偏弱"的标
    assert B.NO_STOCK_FLAG not in a.summary
    # 事件类型跟着标的走（列表标签与内容一致）
    assert B.build_event(z).event_type.value == "stock"
    # 行业信息**不丢**，只是不当标题（用户口径："不要让它成为标题"）
    assert a.affected_industries == ["第三代半导体"]


def test_other_sources_still_gated_by_credibility() -> None:
    """其它来源**一个字都没改**：方向明确但信度不足 ⇒ 不弹。"""
    assert B.is_alertable(_item("偏多", 73)) is False
    assert B.is_alertable(_item("偏多", 74)) is True
    # 分析师名对其它来源**不是**放行理由（那条规则只属于知识星球）
    assert B.is_alertable(
        _item("未定", 94, summary="孙潇雅：看好算力")) is False


def test_bypass_source_detection_accepts_kind_and_legacy_alias() -> None:
    """识别旁路来源的两条判据：`kind` 优先，明文 alias 兜底。

    ⚠️ 假名化后的 alias（`src-…`）**不认** —— 它无法反查也不该反查。
    """
    assert B.is_bypass_source({"kind": "research_note"}) is True
    assert B.is_bypass_source({"source_alias": "research-note-zsxq"}) is True
    assert B.is_bypass_source({"source_alias": "src-1a2b3c4d"}) is False
    # `kind` 明确给了其它类型时**不**再看 alias（否则所有来源都会放行）
    assert B.is_bypass_source(
        {"kind": "newswire", "source_alias": "research-note-zsxq"}) is False


def test_zsxq_alerts_share_the_cross_source_content_key() -> None:
    """跨源同文合并**仍然生效**（用户明确要保留这条闸门）。

    同一件事被别处转发时 `content_key` 相同 ⇒ 告警引擎按它做 24h 冷却抑制，
    不会因为"知识星球也能弹了"就一天弹五遍。
    """
    a = _zsxq("偏多", 58, chash="za")
    b = _item("偏多", 94, alias="newswire-cls", chash="zb")
    b["title"] = a["title"]
    assert B.build_event(a).content_key == B.build_event(b).content_key
    # 但 event_key 按各自内容指纹走，两条不同内容仍是两个事件
    assert B.build_event(a).event_key != B.build_event(b).event_key


def test_zsxq_model_failure_degrades_to_the_rule_layer() -> None:
    """模型/规则失败**绝不抛异常**：退化成"没有理由"，由另一条触发条件兜底。

    这里模拟"tone 字段整个坏掉"（模型失败时情报流给的就是这种形状）——
    分析师名那条路仍必须弹。
    """
    z = _zsxq("偏多", 58, summary="孙潇雅：看好算力产业链")
    z["tone"] = None
    assert B.is_alertable(z) is True
    assert B.build_assessment(z, B.reason_of(z)) is not None
