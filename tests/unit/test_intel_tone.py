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

import asyncio
import json
from pathlib import Path

import pytest

from src.domain.intel import vocab
from src.domain.intel.credibility import MIN_CREDIBILITY_FOR_TONE
from src.domain.intel.tone import (
    TONE_BEAR,
    TONE_BULL,
    TONE_NEUTRAL,
    TONE_UNKNOWN,
    build_prompt,
    extract_tone,
    extraction_schema,
    parse_json,
    rule_tone,
    validate_entities,
    validate_events,
    validate_extraction,
    validate_summary,
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


def test_placeholder_values_count_as_no_data() -> None:
    """★★ 占位值 = "没有数据"，**逐个列表字段**都要吃掉。

    实测两批（同一类错的两种穿法）：

        第一批  `"bull_industries": ["无"]`（写了个汉字"无"）
        第二批  `"codes":[""], "bull_industries":[""], "bull_stocks":[""]`
                （一个**装着空串的列表**，而不是空数组）

    为什么危险：空串 `strip()` 之后仍是一个"存在的条目"（会在 rejected 里
    刷噪音）；而 `"无"` 只要原文里刚好有"无"字，逐字校验就会放行 ——
    界面上于是出现一条叫"无"的行业/个股，看起来像我们的系统坏了。
    """
    text = "公司公告：无重大事项，生产经营正常，订单饱满，机构上调盈利预测"
    # 行业：四种占位写法一个都不能留
    bull, bear, _ = validate_entities(
        {"bull_industries": ["", "  ", "无", "N/A"],
         "bear_industries": ["无"],
         "bull_stocks": ["", "  ", "无", "N/A"],
         "bear_stocks": ["无"]},
        text)
    assert bull["industries"] == [] and bear["industries"] == []
    assert bull["stocks"] == [] and bear["stocks"] == []
    assert bull["boards"] == [] and bear["boards"] == []
    # 代码 / 词组
    _, phrases, codes, rejected = validate_extraction(
        {"tone": TONE_BULL, "phrases": ["", "  ", "无", "N/A"],
         "codes": ["", "  ", "无", "N/A"]}, text)
    assert phrases == [] and codes == []
    # 事件
    events, _ = validate_events(
        {"events": ["", "  ", "无", "N/A"]}, text)
    assert events == []
    # 摘要
    assert validate_summary({"summary": "无"}, text) == ("", "")


def test_placeholder_values_do_not_reach_the_store(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★ 占位值也不许进**存储行**（那才是用户能看到的持久层）。

    模型回的就是"一串空串"时，落库的行必须长得像"这次没抽到"，
    而不是"抽到了一个空字符串"。
    """
    from src.domain.intel import tone_job, tone_store

    _fresh_store(monkeypatch, tmp_path)
    reply = json.dumps({
        "summary": "无", "events": ["", "无", "N/A"], "tone": "偏多",
        "phrases": [""], "codes": [""],
        "bull_industries": [""], "bull_stocks": [""],
        "bear_industries": ["无"], "bear_stocks": ["N/A"],
    }, ensure_ascii=False)
    asyncio.run(tone_job.run_once(
        gateway=_FakeGateway(reply), items=[_item(long_text=True)],
        root=tmp_path))
    hit = tone_store.get("h1", root=tmp_path)
    assert hit is not None
    # 列表字段里不许有"空条目"（空串 / 纯空白 / 占位值）
    for key in ("phrases", "codes", "events"):
        assert all(str(x).strip() for x in hit[key]), (key, hit[key])
    for side in ("bullish", "bearish"):
        assert all(str(x).strip() for x in hit[side]["industries"]), hit[side]
        assert all(str(s["name"]).strip() or str(s["code"]).strip()
                   for s in hit[side]["stocks"]), hit[side]
    # 摘要也不能是占位值（"无"不是摘要）
    assert hit["summary"] != "无"
    assert "N/A" not in json.dumps(hit, ensure_ascii=False)


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


# ======================================================================
# 第二轮：摘要 / 利好利空行业与个股 / 关键事件
# ======================================================================
#
# 这一节的每一条都对应一个具体的失败模式：
#   · 模型编一个原文里没有的股票名（"光模块龙头"）→ 用户拿去核对，核不到
#   · 模型把**券商团队名**当股票（`【天风电子】`）→ 界面上出现一只不存在的票
#   · 输出被 token 上限截断 → 整条退回规则层，标的全丢
#   · 模型挂了 → 功能整个消失（而不是"精度下降"）
#   · 老存储行没有新键 → 读取侧 KeyError，情报流整页挂掉

#: 一条**真实形态**的知识星球调研笔记（含券商团队名 + 个股 + 行业 + 空头标的）。
#:
#: ⚠️ 第三轮起行业要过**概念板块词表**（`ml_board` 的 138 个），所以这条笔记里
#: 同时放了三种行业，供用例分清"为什么被丢"：
#:
#:   `存储芯片` / `军工`   主线挖掘跟踪的板块（逐字在原文里 → 留）
#:   `半导体设备` / `光伏` 原文里逐字有、但**不是**跟踪的板块（→ 丢）
_ZSXQ_NOTE = (
    "【天风电子】半导体设备板块景气上行，存储芯片涨价，"
    "中芯国际（688981）受益于扩产，机构上调盈利预测；"
    "光伏组件价格下滑，隆基绿能601012承压，军工订单下调。"
)

#: 用例用的**受控词表**（名字与代码都取自真实来源：`ml_board` / 行情仓）。
#:
#: 为什么不用真实词表：那要读 950MB 的 `mainline_cache.db`，而且真库明天
#: 多一个板块就会让这里的期望值漂移。真表由
#: `tests/unit/test_intel_vocab.py` 里那两条 `test_real_*` 单独验。
_FAKE_ENTRIES = [
    vocab.VocabEntry("存储芯片", vocab.KIND_BOARD, board_code="886042.TI"),
    vocab.VocabEntry("军工", vocab.KIND_BOARD, board_code="885847.TI"),
    vocab.VocabEntry("中芯国际", vocab.KIND_STOCK, code="688981"),
    vocab.VocabEntry("隆基绿能", vocab.KIND_STOCK, code="601012"),
]


@pytest.fixture(autouse=True)
def _fake_vocabulary(monkeypatch: pytest.MonkeyPatch) -> None:
    """把实体词表换成受控小表（**不读**数据仓/行情仓，断言与真库解耦）。"""
    monkeypatch.setattr(vocab, "_TABLE", vocab.build_table(_FAKE_ENTRIES))


def test_entity_not_in_source_is_dropped() -> None:
    """★ 原文逐字：原文里没有的名字/行业**必须丢**，不做模糊匹配。

    这是本项目比参考实现更严的一条。参考实现只做"在整段研报里 find 到
    就算"，那在**单条笔记**上是不够的 —— 模型会补出"光模块龙头"
    "新能源车"这种"听起来就是这段在讲的东西"。用户拿去原文核对
    一个字都找不到，那比不显示更糟。

    ★ 第三轮又加了一道：行业还必须是**主线挖掘跟踪的概念板块**
    （`存储芯片`/`军工`），"半导体设备"这种原文里逐字有、但我们没有
    跟踪的板块同样要丢 —— 用户口径："情报流的概念板块要和主线挖掘对齐"。
    """
    bull, bear, rejected = validate_entities(
        {"bull_industries": ["存储芯片", "半导体设备", "新能源车"],
         "bull_stocks": ["中芯国际", "光模块龙头"],
         "bear_industries": ["军工", "光伏"],
         "bear_stocks": ["隆基绿能"]},
        _ZSXQ_NOTE)
    names = [s["name"] for s in bull["stocks"]]
    assert names == ["中芯国际"], f"编的股票名被放行：{names}"
    # 跟踪的板块留下；原文里逐字有但没跟踪的（半导体设备）也丢
    assert bull["industries"] == ["存储芯片"]
    assert "半导体设备" in rejected["bull_industries"], "没跟踪的板块被放行了"
    assert "新能源车" in rejected["bull_industries"]
    assert "光模块龙头" in rejected["bull_stocks"]
    # 原文里真有的照常保留（别误杀）
    assert [s["name"] for s in bear["stocks"]] == ["隆基绿能"]
    assert bear["industries"] == ["军工"]
    assert "光伏" in rejected["bear_industries"]
    # 同一批板块的**主线挖掘代码**也要带上（情报流与主线挖掘靠它对齐）
    assert bull["boards"] == [{"name": "存储芯片", "code": "886042.TI"}]
    assert bear["boards"] == [{"name": "军工", "code": "885847.TI"}]


def test_broker_name_is_not_extracted_as_stock() -> None:
    """★ 券商/团队名不是股票 —— 提示词里列了名单，这里必须**硬拦住**。

    实测最高频的一类错：`【天风电子】` 这种"券商+行业"的写法在模型眼里
    完全像一只票。参考实现的提示词名单（天风电子、华福电新、中信电子、
    国金AI金属、中泰汽车、东吴计算机、东北商业航天、招商机械、信达消费）
    这里原样移植，并且**同时**写成拦截 —— 提示词是"请求"，拦截才是"保证"。
    """
    bull, _, rejected = validate_entities(
        {"bull_industries": ["天风电子"], "bull_stocks": ["天风电子"]},
        _ZSXQ_NOTE + " 天风电子 团队观点")
    assert bull["stocks"] == [], "券商名被当成股票了"
    assert bull["industries"] == [], "券商名被当成行业了"
    assert "天风电子" in rejected["bull_stocks"]
    # 名单只有一份：提示词里必须真的写着它（否则模型仍会照抄券商名）
    assert "天风电子" in build_prompt(_ZSXQ_NOTE)


def test_truncated_json_is_repaired() -> None:
    """★ 输出被 token 上限截断时要能救回来。

    截断后 `json.loads` 必然失败，整条退回规则层 —— 而模型其实已经把
    大部分答案说了。修复只补 `]`/`}`（**不补任何字段值**），
    丢掉末尾那个不完整的条目，前面的原样保留。

    ⚠️ 用**扁平的字符串数组**（而不是 `[{"name": ...}]`）是因为
    本地模型在"只列股票名"这种输出上截断得更早、更常见；
    而修复的边界在这里最清楚：`"隆基` 这个**残缺的字符串**必须被丢掉
    （补成 `"隆基"` 就是替模型编一个名字）。
    """
    raw = ('{"tone":"偏多","phrases":[],"codes":[],'
           '"bull_stocks":["中芯国际"],"bear_stocks":["隆基绿能","隆基')
    obj = parse_json(raw)
    assert obj is not None, "截断的 JSON 没有被修复"
    assert obj["tone"] == "偏多"
    assert obj["bull_stocks"] == ["中芯国际"]
    # 残缺的末尾条目不能出现在结果里
    assert json.dumps(obj["bear_stocks"], ensure_ascii=False) == '["隆基绿能"]'


def test_json_with_chatter_prefix_is_parsed() -> None:
    """模型在 JSON 前写了一句"好的" —— 抓第一个 `{...}`，不猜内容。"""
    obj = parse_json('好的，结果是：{"tone":"偏空","codes":[]} 以上。')
    assert obj == {"tone": "偏空", "codes": []}


def test_unparseable_output_returns_none() -> None:
    """完全不是 JSON → `None`（调用方走规则层）。**绝不猜模型想说什么**。"""
    assert parse_json("这段文本没有任何 JSON") is None
    assert parse_json("") is None


def test_count_is_recomputed_from_source() -> None:
    """提及次数取 `max(模型给的, 原文实际出现次数)`。

    实测模型**系统性少报**（原文里出现 3 次，它写 1）。原文计数可复算，
    所以两者取大。⚠️ 只在名字长度 ≥2 时才算 —— 单字名（"中"）
    在原文里出现几十次，计数会变成没有意义的数。
    """
    text = "中芯国际扩产，中芯国际获上调，中芯国际订单饱满。"
    bull, _, _ = validate_entities(
        {"bull_stocks": [{"name": "中芯国际", "count": 1}]}, text)
    assert bull["stocks"][0]["count"] == 3, "少报的计数没有被原文计数补上"


def test_single_char_name_is_not_counted() -> None:
    """单字名不计原文字数（否则计数是噪音），但仍按逐字规则判有效性。"""
    bull, _, _ = validate_entities({"bull_stocks": ["中"]}, "中芯国际扩产")
    assert bull["stocks"] == [], "单字名没有核对价值，应当丢弃"


def test_hallucinated_event_is_dropped() -> None:
    """关键事件允许概括，但**必须与原文有公共子串**。

    实测模型会补一句"公司基本面良好" —— 那种话在原文里连 4 个字都对不上。
    """
    events, rejected = validate_events(
        {"events": ["机构上调盈利预测", "公司基本面良好"]}, _ZSXQ_NOTE)
    assert events == ["机构上调盈利预测"]
    assert rejected == ["公司基本面良好"]


def test_summary_without_root_in_source_is_dropped() -> None:
    """摘要同样要有根：与原文的最长公共子串 ≥ 4 字。"""
    text = "半导体设备板块景气上行，中芯国际受益于扩产"
    assert validate_summary({"summary": "半导体设备景气上行"}, text)[0] == \
        "半导体设备景气上行"
    got, reason = validate_summary({"summary": "公司基本面持续向好"}, text)
    assert got == "" and reason == "no_root_in_source"


def test_rule_only_when_llm_unavailable_keeps_new_fields_empty() -> None:
    """★★ 模型不可用时**绝不抛异常**，旧字段照常工作。

    「模型挂了」是一个必然会发生的事件（本地 Ollama 会被关机、会被
    别的任务占满）。正确行为是"精度下降"而不是"功能消失"：
    倾向退回词表，摘要/标的/事件给空值（**不编**）。

    ⚠️ 用**长文本**（> `MIN_CHARS_FOR_EXTRACTION`）：短文本那条路
    `tone_job` 会传 `rule_entities=True`（词表扫描补实体，见
    `tests/unit/test_intel_vocab.py`）；而这里要锁的是
    **长文本 + 模型挂了** —— 那条路上不许用整条语气给每个标的派方向。
    """
    text = _ZSXQ_NOTE * 2
    res = extract_tone(text=text, credibility_score=70, llm_obj=None)
    assert res.tone == rule_tone(text).tone
    assert res.summary == ""
    assert res.events == []
    assert res.bullish == {} and res.bearish == {}
    pub = res.to_public()
    # 旧消费方读的字段一个都不能少（向后兼容）
    for k in ("tone", "has_tone", "phrases", "codes", "confidence",
              "source", "explain"):
        assert k in pub, f"旧字段 {k} 丢了"
    # 新字段也必须**在契约里**（白名单漏了就等于功能不存在）
    for k in ("summary", "events", "bullish", "bearish"):
        assert k in pub, f"新字段 {k} 没进 to_public 白名单"


def test_events_and_summary_survive_tone_unknown() -> None:
    """`未定` 只清 `phrases`（它是倾向的依据），**不清**标的与事件。

    理由：标的与事件是"原文里明写的事实"，不是判断 ——
    "模型与词表对语气意见不一致"推不出"这段原文没提到中芯国际"。
    清掉它们等于因为一个分歧丢掉全部可核对的信息。
    """
    res = extract_tone(text=_ZSXQ_NOTE, credibility_score=70,
                       llm_obj={"tone": TONE_NEUTRAL,
                                "phrases": ["机构上调盈利预测"],
                                "codes": ["688981"],
                                "bull_stocks": ["中芯国际"],
                                "bull_industries": ["存储芯片"],
                                "events": ["机构上调盈利预测"],
                                "summary": "存储芯片涨价"})
    assert res.tone == TONE_UNKNOWN, "冲突时应当给未定"
    assert res.phrases == [], "未定时不该留依据"
    assert [s["name"] for s in res.bullish["stocks"]] == ["中芯国际"]
    assert res.bullish["industries"] == ["存储芯片"]
    assert res.bullish["boards"] == [{"name": "存储芯片", "code": "886042.TI"}]
    assert res.events == ["机构上调盈利预测"]
    assert res.summary == "存储芯片涨价"


def test_hallucinated_code_bucket_is_dropped() -> None:
    """★ 模型给的代码**一个字都不采用**（它会给一个"配得上名字"的号）。

    实测：原文里连一个数字都没有，模型照样能编出 6 位代码。
    旧实现是"代码不在原文里 → 整条丢"（连名字一起没了）；
    现在改成**名字保留、代码从词表取** —— 名字逐字在原文里可核对，
    代码来自本地名录也可核对，而模型那个值直接不看（进 `rejected` 供排障）。
    """
    bull, _, rejected = validate_entities(
        {"bull_stocks": [{"name": "中芯国际", "code": "600519"}]}, _ZSXQ_NOTE)
    assert [s["name"] for s in bull["stocks"]] == ["中芯国际"]
    assert bull["stocks"][0]["code"] == "688981", "代码没有从词表取"
    assert "600519" not in json.dumps(bull, ensure_ascii=False), "模型编的代码上了结果"
    assert rejected["bull_stocks_model_code"] == ["中芯国际:600519"]


def test_name_code_pairing_from_source() -> None:
    """原文写 `中芯国际（688981）` 时要补全**配对关系**。

    补的不是新事实：代码在原文里、名字在原文里，缺的只是"这两个是一对"。
    反过来（原文只写了代码）用本地名录查名字，查不到就留空 —— **不编名字**。
    """
    bull, _, _ = validate_entities({"bull_stocks": ["中芯国际"]}, _ZSXQ_NOTE)
    assert bull["stocks"][0]["code"] == "688981"


def test_extraction_schema_is_a_copy() -> None:
    """schema 必须返回拷贝：调用方就地改它会污染所有后续调用的约束。"""
    s = extraction_schema()
    s["properties"]["tone"]["enum"] = ["x"]
    assert extraction_schema()["properties"]["tone"]["enum"] == [
        "偏多", "偏空", "中性"]
    # 九个字段都要在 schema 里（少了哪个，受约束解码就不会输出那个键）
    for k in ("summary", "events", "tone", "phrases", "codes",
              "bull_industries", "bear_industries", "bull_stocks",
              "bear_stocks"):
        assert k in extraction_schema()["required"]


def test_job_prompt_delegates_to_tone() -> None:
    """★★ `tone_job.build_prompt(title, body)` 必须仍然**委托**给 `tone`。

    提示词与校验判据是同一套（券商名单、逐字约束、九个字段的结构）。
    一旦这里改成"任务里自己拼一份提示"，模型被要求输出的东西与
    `tone` 校验的东西就会**悄悄分叉** —— 表现是"模型总是抽不到"，
    而日志里看不出任何异常。所以这里直接锁"两份提示文本同源"。
    """
    from src.domain.intel import tone, tone_job

    assert tone_job.build_prompt("标题", "正文") == tone.build_prompt("标题\n正文")
    # 系统提示也是同一个对象（不是抄了一份）
    assert tone_job._SYSTEM == tone.SYSTEM_PROMPT


# ======================================================================
# 编排层（tone_job）：模型失败、老行兼容、落库白名单
# ======================================================================

class _FakeGateway:
    """假网关。`reply` 为 `None` 时**抛异常**（模拟模型不可用）。"""

    def __init__(self, reply: str | None) -> None:
        self.reply = reply
        self.calls = 0
        self.kwargs: dict[str, object] = {}

    async def complete(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        self.kwargs = kwargs
        if self.reply is None:
            raise RuntimeError("ollama 不可用")
        return type("R", (), {"content": self.reply})()


def _fresh_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """把 `tone_store` 换到临时目录（**不碰真实存储**）。

    `tone_store` 是进程内缓存 + 追加写，测试必须显式清掉缓存，
    否则会把真库里已有的 hash 当成"抽过了"。
    """
    from src.domain.intel import tone_store

    monkeypatch.setattr(tone_store, "store_path",
                        lambda root=None: tmp_path / "tone_results.jsonl")
    monkeypatch.setattr(tone_store, "_CACHE", {})
    monkeypatch.setattr(tone_store, "_LOADED", True)


def _item(content_hash: str = "h1", *, long_text: bool = False) -> dict:
    """一条送抽取的条目。

    ⚠️ `long_text=True` 用**长文本**（> `MIN_CHARS_FOR_EXTRACTION`）：
    短文本按用户口径**根本不调模型**（`tone_job` 判长度），
    所以"要验模型这条路"的用例必须给足长度，否则断言会静默落空
    （实测踩过：短文本下 `gateway.calls == 0`，用例还以为模型被调过）。
    """
    return {
        "content_hash": content_hash,
        "title": "【天风电子】半导体设备",
        "summary": _ZSXQ_NOTE * 2 if long_text else _ZSXQ_NOTE,
        "credibility": {"score": 70},
    }


def test_tone_job_model_failure_falls_back_without_raising(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★★ **模型不可用时 `run_once` 不许抛** —— 旧字段照常落库。

    这条是"模型必须有兜底"的守门测试：把 `gateway.complete` 换成
    抛异常的假网关，任务仍要写出倾向结果（`source="rules"`）。

    ⚠️ 用长文本：短文本那条路压根不调模型（用户口径），
    拿短文本测"模型挂了"等于什么也没测。
    """
    from src.domain.intel import tone_job, tone_store

    _fresh_store(monkeypatch, tmp_path)
    stats = asyncio.run(tone_job.run_once(
        gateway=_FakeGateway(None), items=[_item(long_text=True)],
        root=tmp_path))
    assert stats["written"] == 1
    hit = tone_store.get("h1", root=tmp_path)
    assert hit is not None
    assert hit["source"] == "rules", "模型挂了应当退回规则层"
    assert hit["tone"], "旧字段必须照常落库"
    # 没抽到就是空值，**不许编**
    assert hit["events"] == []
    assert hit["bullish"]["stocks"] == []


def test_tone_job_persists_new_fields(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """一次调用抽到的摘要/事件/标的要**落进存储**（接口才读得到）。"""
    from src.domain.intel import tone_job, tone_store

    _fresh_store(monkeypatch, tmp_path)
    reply = json.dumps({
        "summary": "存储芯片景气上行，光伏承压",
        "events": ["机构上调盈利预测"],
        "tone": "偏多", "phrases": ["机构上调盈利预测"],
        "codes": ["688981"],
        "bull_industries": ["存储芯片"], "bull_stocks": ["中芯国际"],
        "bear_industries": [], "bear_stocks": ["隆基绿能"],
    }, ensure_ascii=False)
    gateway = _FakeGateway(reply)
    stats = asyncio.run(tone_job.run_once(
        gateway=gateway, items=[_item(long_text=True)], root=tmp_path))
    assert stats["written"] == 1
    hit = tone_store.get("h1", root=tmp_path)
    assert hit is not None
    assert [s["name"] for s in hit["bullish"]["stocks"]] == ["中芯国际"]
    assert hit["bullish"]["boards"] == [{"name": "存储芯片", "code": "886042.TI"}]
    # 模型给的空行业列表照旧（不是被词表拦掉的，是真的没有）
    assert hit["bearish"]["industries"] == []
    assert hit["events"] == ["机构上调盈利预测"]
    assert hit["summary"], "摘要没落库"
    # 拒绝明细**不许**进持久层（那是幻觉原文，会被当成真实抽取结果）
    assert "rejected" not in hit
    # ★ 必须走**语法级约束**（Ollama 受约束解码）：只给 json_mode 时
    # 小模型会返回"是 JSON 但不是这个 JSON"的东西，字段名自创，
    # 整条静默退回规则层 —— 看起来像"模型没抽到"。
    assert gateway.kwargs.get("json_schema"), "抽取没有下发布局约束"


def test_old_store_row_without_new_keys_still_reads(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★★ 第二轮之前落库的行（**没有** events/bullish/bearish）必须照常可读。

    存储里现在同时存在两种格式：老行只有 tone/phrases/codes/…。
    缺键是**正常状态**，不是损坏 —— 读取侧直接下标会在
    "用户翻到一条几天前的老数据"时 KeyError，把整页情报流打挂。
    真库里实测已有 176 行这种格式。
    """
    from src.domain.intel import tone_store

    _fresh_store(monkeypatch, tmp_path)
    legacy = {
        "content_hash": "old1", "at": "2026-09-25T16:27:11+08:00",
        "tone": "偏多", "has_tone": True, "neutral": False,
        "phrases": ["增长"], "codes": [], "confidence": 0.6,
        "source": "rules+llm", "explain": "词表计数",
        "summary": "中秋假期跨区域人员流动量增长",
    }
    p = tone_store.store_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(legacy, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    tone_store.load(root=tmp_path, force=True)

    t = tone_store.view(tone_store.get("old1", root=tmp_path))
    assert t["tone"] == "偏多" and t["has_tone"] is True
    assert t["summary"]
    # 老行缺的新字段给**空值**，不抛
    assert t["events"] == []
    # ⚠️ `boards`（第三轮）同样要为老行补空列表：读取侧 `_side_view` 是投影，
    # 漏一个键的表现是"抽到了但接口永远看不到"，不会报错。
    assert t["bullish"] == {"industries": [], "stocks": [], "boards": [],
                            "count": 0}
    assert t["bearish"]["stocks"] == []


def test_view_of_missing_row_is_safe() -> None:
    """没抽过的条目（`get` 返回 `None`）也要能安全摊平。"""
    from src.domain.intel import tone_store

    t = tone_store.view(None)
    assert t["has_tone"] is False and t["tone"] == "未定"
    assert t["events"] == [] and t["bullish"]["count"] == 0


def test_store_row_whitelist_excludes_rejected() -> None:
    """落库白名单：`rejected`（被拦掉的幻觉原文）**不许**进存储文件。"""
    from src.domain.intel import tone_store

    row = tone_store.build_row({
        "tone": "偏多", "rejected": {"phrases": ["机构看好"]},
        "phrases": ["上调盈利预测"], "events": [], "bullish": {},
        "bearish": {}, "一些将来的字段": 1,
    })
    assert "rejected" not in row
    assert "一些将来的字段" not in row, "白名单外的字段默认不该落库"
    assert row["tone"] == "偏多" and row["phrases"] == ["上调盈利预测"]

