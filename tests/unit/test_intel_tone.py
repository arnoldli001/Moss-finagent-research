"""原文倾向抽取的回归测试（P1 第二步）。

## 为什么这些断言值得写

倾向是这一页里**唯一带模型判断**的字段，也是最容易出幻觉的地方。
实测（本地 `qwen2.5:1.5b`，5 条样本）出了 4 类错，全部锁进测试：

| 幻觉 | 实例 |
|---|---|
| 把 JSON 模板当答案抄回 | `codes: ["6位代码"]` |
| 空串占位 | `codes: [""]` |
| 标点被改写（不再逐字） | `phrases: ["据传,未经证实"]`（原文是全角逗号） |
| **判定相反** | 「中标12.5亿元订单，机构上调盈利预测」被判 `中性` |

最后一条最危险：**不是抽取失败，是判断错了** —— 所以判据不能只看模型，
必须与规则层交叉验证。不一致时给"未定"，那才是正确答案。
"""

from __future__ import annotations

import pytest

from src.domain.intel.credibility import MIN_CREDIBILITY_FOR_TONE
from src.domain.intel.tone import (
    TONE_BEAR,
    TONE_BULL,
    TONE_NEUTRAL,
    TONE_UNKNOWN,
    extract_tone,
    rule_tone,
    validate_extraction,
)


# ======================================================================
# 规则层
# ======================================================================

def test_rule_tone_counts_words() -> None:
    assert rule_tone("中标12.5亿元订单，机构上调盈利预测").tone == TONE_BULL
    assert rule_tone("业绩下滑，遭大额减持").tone == TONE_BEAR
    assert rule_tone("今天天气不错").tone == TONE_UNKNOWN


def test_rule_tone_confidence_grows_with_gap() -> None:
    """差距越大越有把握，但**上限 0.9** —— 词表法不该自称完全确定。"""
    one = rule_tone("中标")
    many = rule_tone("中标 订单 扩产 提价 获批")
    assert many.confidence > one.confidence
    assert many.confidence <= 0.9


# ======================================================================
# 幻觉拦截（逐条对应实测的错）
# ======================================================================

def test_template_text_as_code_is_rejected() -> None:
    """实测：模型把 JSON 模板里的 `"6位代码"` 当答案抄回来了。"""
    tone, phrases, codes, rejected = validate_extraction(
        {"tone": TONE_BULL, "phrases": [], "codes": ["6位代码"]},
        "市场传闻某公司可能获得大额订单")
    assert codes == [], f"模板文字被当成代码：{codes}"
    assert rejected["codes"] == ["6位代码"]


def test_empty_string_code_is_rejected() -> None:
    """实测：`codes: [""]`。"""
    _, _, codes, _ = validate_extraction(
        {"tone": TONE_NEUTRAL, "codes": ["", "600519"]},
        "贵州茅台600519发布公告")
    assert codes == ["600519"]


def test_rewritten_punctuation_phrase_is_rejected() -> None:
    """实测：模型把全角逗号改成半角，词组不再逐字 —— 必须丢弃。

    **不做模糊匹配**：模糊匹配会把"改写"也放进来，那就失去了
    "用户能拿它去原文核对"的意义。
    """
    text = "市场传闻某公司可能获得大额订单（据传，未经证实）"
    _, phrases, _, rejected = validate_extraction(
        {"tone": TONE_BULL, "phrases": ["据传,未经证实", "大额订单"]}, text)
    assert "据传,未经证实" not in phrases
    assert "大额订单" in phrases, "合法词组被误杀"
    assert rejected["phrases"] == ["据传,未经证实"]


def test_illegal_tone_value_is_rejected() -> None:
    tone, _, _, rejected = validate_extraction(
        {"tone": '偏多|偏空|中性'}, "某文本内容足够长以通过长度检查")
    assert tone == TONE_UNKNOWN
    assert rejected["tone"]


def test_short_phrase_is_rejected() -> None:
    """过短的词组在任何文本里都能找到，没有核对价值。"""
    _, phrases, _, _ = validate_extraction(
        {"tone": TONE_BULL, "phrases": ["涨", "上", "中标订单"]},
        "某公司中标订单，股价上涨")
    assert "涨" not in phrases and "上" not in phrases
    assert "中标订单" in phrases


# ======================================================================
# 股票代码：必须是**A 股**且原文里真有
# ======================================================================

@pytest.mark.parametrize("text,expect", [
    ("金砖国家新开发银行与南非签署2亿美元水项目贷款协议。", []),
    ("土耳其里拉跌至256500关口", []),
    ("金额123456789元", []),
    ("贵州茅台600519发布公告", ["600519"]),
    ("中芯国际688981公告", ["688981"]),
    ("创业板301236上市", ["301236"]),
    ("深主板000001平安银行", ["000001"]),
    ("北交所430047诺思兰德", ["430047"]),
    ("北交所新股830799上市", ["830799"]),
])
def test_only_real_a_share_codes_are_kept(text: str, expect: list[str]) -> None:
    """只保留**真实存在的 A 股代码**。

    实测踩到：`\\d{6}` 会把别国事件里的金额串当代码 ——
    "与南非签署**2亿美元**项目贷款" 抠出了 `222500`、`256500` 这种
    根本不存在的代码，还带着 `codes` 上了界面。给用户看假标的是硬伤。

    ⚠️ 各分支**位数必须各自配平到 6 位**：第一版把 `8[3-9]`（2 位）与
    `60[0135]`（4 位）塞进同一个组再补 `\\d{3}`，于是北交所代码永远匹配不上
    （实测 `830799` 漏掉）。
    """
    _, _, codes, _ = validate_extraction(
        {"tone": TONE_NEUTRAL, "phrases": [], "codes": []}, text)
    assert codes == expect, f"{text!r} → {codes}，期望 {expect}"


def test_codes_fall_back_to_source_when_model_gives_none() -> None:
    """模型没给代码但原文里有 → 用规则抓（原文里真有，不算幻觉）。"""
    _, _, codes, _ = validate_extraction(
        {"tone": TONE_BULL, "codes": []}, "科伦药业002422公告")
    assert codes == ["002422"]


# ======================================================================
# 交叉验证：不一致就不给倾向
# ======================================================================

def test_conflicting_signals_yield_unknown() -> None:
    """★ **实测最危险的错**：模型判反了。

    「中标12.5亿元订单，机构上调盈利预测」规则层命中 4 个偏多词，
    而模型判 `中性`。这时两个都不信 —— 给 `未定`。
    """
    text = "中标12.5亿元订单，同比+30%，机构上调盈利预测"
    assert rule_tone(text).tone == TONE_BULL
    res = extract_tone(text=text, credibility_score=70,
                       llm_obj={"tone": TONE_NEUTRAL, "phrases": [],
                                "codes": []})
    assert res.tone == TONE_UNKNOWN
    assert res.has_tone is False
    assert res.confidence is None, "冲突时不该给出数值置信度"
    assert "不一致" in res.explain
    # 未定时不给依据 —— 否则等于变相给了倾向
    assert res.phrases == []


def test_agreement_yields_tone_with_confidence() -> None:
    text = "某公司中标12.5亿元订单，机构上调盈利预测"
    res = extract_tone(text=text, credibility_score=70,
                       llm_obj={"tone": TONE_BULL,
                                "phrases": ["中标12.5亿元订单"],
                                "codes": []})
    assert res.tone == TONE_BULL
    assert res.has_tone is True
    assert res.confidence is not None
    assert "一致" in res.explain


def test_phrases_fall_back_to_rule_hits_when_llm_phrases_rejected() -> None:
    """★★ 模型词组全被拦掉时，要用**规则层命中的词**兜底。

    不兜底的后果：界面显示"偏多"却**没有依据** —— 对一个必须能被核对的
    字段，那比不显示更糟（用户无法判断归类对不对）。
    规则层的词按定义就是原文子串，逐字可核。
    """
    text = "公司公告：中标12.5亿元订单，机构上调盈利预测"
    res = extract_tone(text=text, credibility_score=70,
                       llm_obj={"tone": TONE_BULL,
                                # 这两个都不逐字（被改写过 / 不存在）
                                "phrases": ["中标12.5亿", "机构看好"],
                                "codes": []})
    assert res.tone == TONE_BULL
    assert res.phrases, "有倾向却没有依据 —— 界面无法核对"
    for p in res.phrases:
        assert p in text, f"兜底依据不逐字：{p!r}"


def test_rule_only_when_llm_unavailable() -> None:
    """模型不可用（`llm_obj=None`）时只用规则层，**功能不消失**。"""
    res = extract_tone(text="公司中标大额订单", credibility_score=70,
                       llm_obj=None)
    assert res.tone == TONE_BULL
    assert res.source == "rules"
    assert res.confidence is not None


# ======================================================================
# 低可信不做倾向分析（用户口径）
# ======================================================================

def test_low_credibility_is_skipped_entirely() -> None:
    """★ 用户口径："可信度低的也不做倾向分析。"

    连模型都不调 —— 省算力，也避免"抽错了没人发现"。
    """
    text = "据传某公司将获百亿订单，股价要涨"
    res = extract_tone(text=text, credibility_score=30,
                       llm_obj={"tone": TONE_BULL, "phrases": [],
                                "codes": []})
    assert res.tone == TONE_UNKNOWN
    assert res.source == "skipped"
    assert res.confidence is None
    assert str(MIN_CREDIBILITY_FOR_TONE) in res.explain


def test_threshold_boundary_allows_at_threshold() -> None:
    """恰好等于阈值时**允许**分析（`>=` 而非 `>`）。"""
    res = extract_tone(text="公司中标大额订单",
                       credibility_score=MIN_CREDIBILITY_FOR_TONE,
                       llm_obj=None)
    assert res.source == "rules", "阈值上的条目被跳过了"


# ======================================================================
# 契约
# ======================================================================

def test_public_output_has_no_source_identifier() -> None:
    """倾向输出里**不含任何来源标识**（模型也拿不到来源名）。"""
    import json

    text = "某公司中标12.5亿元订单"
    res = extract_tone(text=text, credibility_score=70,
                       llm_obj={"tone": TONE_BULL,
                                "phrases": ["中标12.5亿元订单"],
                                "codes": []})
    blob = json.dumps(res.to_public(), ensure_ascii=False)
    for leak in ("source_alias", "source_name", "eastmoney", "zsxq", "群组"):
        assert leak not in blob, f"倾向输出泄漏 {leak!r}"


def test_public_output_has_no_directional_fields() -> None:
    """合规硬约束：契约里不存在目标价/评级/买卖字段。"""
    pub = extract_tone(text="公司中标订单", credibility_score=70,
                       llm_obj=None).to_public()
    for banned in ("target_price", "rating", "buy_sell", "suggest", "advice"):
        assert banned not in pub, f"倾向契约里出现了越线字段 {banned!r}"
    # `tone` 是"原文倾向"，不是"建议" —— 字段名与解释都要能自证这一点
    assert "原文" in pub["explain"] or pub["source"] in ("rules", "rules+llm",
                                                         "skipped")
