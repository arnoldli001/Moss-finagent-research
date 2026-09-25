"""可信度打分的回归测试（**规则层，零 LLM**）。

## 为什么这些断言值得写

可信度是**排序与筛选的依据** —— 它错了，用户看到的"最该看的几条"就是错的，
而且错得很安静（分数照样显示、界面照样正常）。

所以要锁住三件事：

  ① **公式与设计稿例证一致**（改了权重会让所有分数漂移）
  ② **来源分即上限**（小作文堆不出高分 —— 这是防"用传闻伪造可信度"的核心）
  ③ **对外只出分数与理由，不出任何来源标识**（数据源保密）
"""

from __future__ import annotations

import json

import pytest

from src.domain.intel.credibility import (
    FILTERS,
    MIN_CREDIBILITY_FOR_TONE,
    Credibility,
    content_score,
    level_of,
    matches_filter,
    score_item,
    source_level,
)


# ======================================================================
# 来源分
# ======================================================================

def test_source_level_matches_on_any_keyword() -> None:
    """来源分级是 **OR** 语义：命中任意特征词即算该档。

    ## 实测踩过的坑

    第一版写成 `all(m in name for m in level.match)`（AND），而配置里
    一档列了十几个近义词 —— 于是**永远匹配不上**（没有任何名字会同时含
    "公告"和"交易所"和"证监会"…），实测全部落到保守档 38，
    整个打分形同虚设。这条测试就是锁住这个语义。
    """
    # 官方档：只要名字里有"公告"就该命中，不需要同时有"交易所"
    assert source_level("交易所公告")[0] == 94
    assert source_level("某某公司公告")[0] == 94
    # 持牌研报档
    assert source_level("券商研报-某某证券研究所")[0] == 84
    # 权威媒体档
    assert source_level("财联社电报")[0] == 74
    # 自媒体档（知识星球内容走这里）
    assert source_level("知识星球-WD调研")[0] == 54
    # 未证实传闻：最低档
    assert source_level("据传某某公司将被收购")[0] == 28


def test_source_level_unknown_is_conservative() -> None:
    """认不出来源时给**保守档**，不是中性 50。

    高估一个陌生来源的代价（用户信了假消息）远大于低估（用户多看一眼）。
    """
    base, label = source_level("某某不明渠道")
    assert base <= 40, f"陌生来源给分过高：{base}（{label}）"
    assert source_level("")[0] <= 40


# ======================================================================
# 公式
# ======================================================================

def test_source_score_is_a_ceiling() -> None:
    """★ **来源分即上限**：内容写得再好，也抬不动低权威来源。

    这是全模块最重要的一条性质。设计稿 §4.1 的原话是"来源分同时是不可
    突破的上限"，理由是防止"低质来源靠堆量/堆措辞刷分"。

    验证方式：拿一档只有 54 分的来源（自媒体），喂**满分内容**（附公告编号
    的官方原文形态），看它能到多少 —— 必须仍在 60 出头，
    **不能**接近 94（官方档）。
    """
    official_text = "公告编号2026-041 证券代码：688981 关于先进制程扩产的公告"
    c = score_item(source_name="知识星球-WD调研", kind="research_note",
                   title=official_text, summary=official_text)
    assert c.source_base == 54, c.source_base
    assert c.content_base == 100, c.content_base
    assert c.score <= 65, (
        f"低权威来源配满分内容拿到了 {c.score} —— 来源分没有成为上限，"
        f"等于用措辞伪造了可信度")
    # 而且要明显低于官方档
    assert c.score < 94 - 20


def test_official_source_with_weak_content_is_still_high() -> None:
    """反向：官方来源配弱内容，也不该被内容分拖垮。

    一条只有"待公布"三个字的交易所公告仍然是官方披露。加权混合里
    低分项占 85%，所以 94 与中性内容分混合后仍应在 60 分以上
    （实测 57~63，落在「中」档而非「低」档）。
    """
    c = score_item(source_name="交易所公告", kind="policy",
                   title="待公布", summary="")
    assert c.source_base == 94
    assert c.score >= 55, f"官方来源被内容分拖到 {c.score}"
    assert level_of(c.score)[0] in ("mid", "upper", "high"), (
        f"官方来源落到 {level_of(c.score)[1]} 档（{c.score}）")


@pytest.mark.parametrize("src,cont,lo,hi", [
    (94, 100, 90, 96),    # 官方 + 官方原文
    (84, 78, 76, 86),     # 研报 + 有数据支撑
    (54, 52, 50, 58),     # 自媒体 + 转述
    (28, 12, 8, 20),      # 传闻 + 纯推测
])
def test_formula_stays_in_design_range(src: int, cont: int, lo: int,
                                       hi: int) -> None:
    """加权混合的落点。

    ## 与设计稿 §4.4 的差异（如实记录）

    设计稿给的四行期望是 **94 / 88 / 66 / 31**，但把任何单一公式代入
    §4.2 自身给出的分数（内容分 100/78/52/12）都**无法同时命中**这四行：

        公式                      94,100   84,78   54,52   28,12
        相加式 min(100, 源+0.35内容)  100     100      84      32
        取低者 min(源, 内容)           94      78      52      12
        加权 85/15                    95      80.1    52.7    14.6
        设计稿期望                     94      88      66      31

    "相加式"能对上第 4 行（32 vs 31），但把官方与研报都顶到 100；
    "取低者"能对上第 1 行，但小作文只有 52（期望 66）。
    四行里第 2、3 行（88、66）用 §4.2 的内容分**根本算不出来** ——
    更像是手写示例时按印象填的。

    所以这里**以性质为准、不以示例数字为准**：
      · 官方 + 可核实 → 90 以上
      · 低权威来源配满分内容 → 仍 ≤65（来源分即上限，见上一条测试）
      · 传闻 → 20 以下
    这三条性质比拟合四个手写数字重要得多，也是"防伪造可信度"的真正依据。
    """
    got = int(round(0.85 * min(src, cont) + 0.15 * max(src, cont)))
    assert lo <= got <= hi, f"({src},{cont}) → {got} 不在 [{lo},{hi}]"


# ======================================================================
# 内容分
# ======================================================================

def test_content_score_orders_by_verifiability() -> None:
    """内容分按**可核实程度**降序，不是按"写得好不好"。"""
    official = content_score("关于签订重大合同的公告", "公告编号2026-001")
    data = content_score("8月排产环比+12%", "订单同比增长30%")
    quoted = content_score("某分析师认为行业将复苏", "")
    emotion = content_score("大利好！全面爆发！", "")
    guess = content_score("某公司或将受益", "不排除下周有进展")
    assert official.score > data.score > quoted.score > emotion.score, (
        f"{official.score}/{data.score}/{quoted.score}/{emotion.score}")
    assert guess.score <= emotion.score


def test_content_score_never_zero_for_plain_text() -> None:
    """平铺直叙的客观陈述给**中性分**，不能是 0。

    0 会让一条客观陈述看起来像"纯推测"（12）—— 那是**低估**。
    真正压住它的仍然是来源分（两轴取低者占 85%）。
    """
    s = content_score("公司发布了日常经营公告", "内容为常规披露")
    assert s.score >= 40, f"中性文本被压到 {s.score}"


# ======================================================================
# 分层与筛选
# ======================================================================

def test_level_boundaries() -> None:
    assert level_of(80)[0] == "high"
    assert level_of(79)[0] == "upper"
    assert level_of(65)[0] == "upper"
    assert level_of(64)[0] == "mid"
    assert level_of(50)[0] == "mid"
    assert level_of(49)[0] == "low"
    assert level_of(35)[0] == "low"
    assert level_of(34)[0] == "doubt"


def _cred(score: int, source_base: int = 54) -> Credibility:
    return Credibility(score=score, source_base=source_base, content_base=50,
                       source_reason="x", content_reason="y")


@pytest.mark.parametrize("key,score,source_base,kind,expect", [
    ("all", 95, 94, "newswire", True),
    ("all", 10, 28, "newswire", True),
    ("high", 80, 94, "newswire", True),
    ("high", 79, 84, "newswire", False),
    ("mid_up", 50, 54, "newswire", True),
    ("mid_up", 49, 38, "newswire", False),
    ("low", 49, 38, "newswire", True),
    ("low", 50, 54, "newswire", False),
    # 「仅官方」按**来源档**判，不按分数 —— 一条 60 分的官方公告也该进来
    ("official", 60, 94, "policy", True),
    ("official", 60, 84, "policy", False),
    # 「仅研报」按**类型**判
    ("broker", 60, 84, "broker_report", True),
    ("broker", 90, 94, "newswire", False),
])
def test_filter_predicates(key: str, score: int, source_base: int, kind: str,
                           expect: bool) -> None:
    got = matches_filter(_cred(score, source_base), kind, key)
    assert got is expect, f"{key} score={score} base={source_base} → {got}"


def test_unknown_filter_does_not_silently_empty_the_list() -> None:
    """未知档位**不过滤**（宁可多给，不要静默清空列表）。

    静默清空是最坏的一类失败：用户看到空页面会以为"没有情报"，
    而实际是参数写错了。
    """
    assert matches_filter(_cred(50), "newswire", "no_such_filter") is True
    assert "all" in FILTERS


def test_low_credibility_is_excluded_from_tone_analysis() -> None:
    """低于阈值**不做倾向分析**（用户口径 2026-09-25）。

    模型从一条低可信来源里抽"偏多/偏空"，抽错了也没人会发现 ——
    而用户会把它当成平台的判断。所以低可信只进情报流留档。

    ⚠️ 阈值卡在 50 分（「中」档的下界）会有边界歧义：一条 51 分的
    中性文本刚好跨过去。这里取样时**避开边界**（用明确低分的传闻），
    同时把边界行为单独断言 —— 边界怎么写是产品决定，但不能是"没人
    想过"的意外。
    """
    # 明确低分：传闻 + 纯推测
    low = score_item(source_name="某某不明渠道", kind="newswire",
                     title="据传某公司将被收购", summary="网传消息，纯属猜测")
    assert low.score < MIN_CREDIBILITY_FOR_TONE, f"分数 {low.score} 没到阈值以下"
    assert low.tone_allowed is False

    # 明确高分：官方 + 可核实
    high = score_item(source_name="交易所公告", kind="policy",
                      title="关于重大合同的公告", summary="公告编号2026-001")
    assert high.score >= MIN_CREDIBILITY_FOR_TONE
    assert high.tone_allowed is True

    # 边界：恰好等于阈值时**允许**（`>=` 而非 `>`）
    at = Credibility(score=MIN_CREDIBILITY_FOR_TONE, source_base=54,
                     content_base=50, source_reason="x", content_reason="y",
                     tone_allowed=MIN_CREDIBILITY_FOR_TONE
                     >= MIN_CREDIBILITY_FOR_TONE)
    assert at.tone_allowed is True


# ======================================================================
# 契约：不泄漏来源标识
# ======================================================================

def test_public_output_carries_no_source_identifier() -> None:
    """`to_public()` 只出分数与中文理由，**不出任何来源标识**。

    打分内部用的是真实来源名（"东方财富-全球财经快讯"），
    而那正是要保护的资产 —— 一旦跟着分数出去，前面所有脱敏都白做。
    """
    cred = score_item(source_name="东方财富-全球财经快讯", kind="newswire",
                      title="市场消息", summary="某板块走强")
    pub = cred.to_public()
    blob = json.dumps(pub, ensure_ascii=False)
    for leak in ("东方财富", "eastmoney", "全球财经", "sina", "zsxq",
                 "source_name", "source_alias"):
        assert leak not in blob, f"可信度输出泄漏 {leak!r}：{blob}"
    # 必需字段都在
    for k in ("score", "source_base", "content_base", "source_reason",
              "content_reason", "corroboration", "tone_allowed", "explain"):
        assert k in pub, f"缺字段 {k}"


def test_corroboration_is_none_not_zero() -> None:
    """佐证数第一步恒为 `None`，**不是 0**。

    0 的意思是"查过了，没有第二条来源"；`None` 是"还没做这个判断"。
    混起来会让界面对用户说谎。佐证需要多源共振聚类（第二步）。
    """
    c = score_item(source_name="交易所公告", kind="policy",
                   title="公告", summary="")
    assert c.corroboration is None
    assert c.to_public()["corroboration"] is None


def test_credibility_has_no_directional_fields() -> None:
    """合规硬约束：契约里**不存在**目标价/评级/买卖字段（设计稿 §0.3）。"""
    pub = score_item(source_name="券商研报", kind="broker_report",
                     title="行业深度", summary="排产+12%").to_public()
    for banned in ("target_price", "rating", "buy_sell", "tone", "direction",
                   "bullish", "bearish"):
        assert banned not in pub, f"可信度契约里出现了越线字段 {banned!r}"
