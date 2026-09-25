"""原文倾向抽取（P1 第二步）—— 对象是**第三方原文的语气**，不是平台判断。

对应设计：`docs/INTEL_CENTER_REDESIGN.md` §0.3（约束 3）、§5.1。

## ⚠️ 这个模块的输出**不是**我们的观点

字段名 `tone` 的含义严格限定为：**这条第三方原文自己是什么语气**。
界面上必须显示为「原文倾向」，且**永远**与一个可核对的依据（原文词组）
一起出现 —— 用户能自己判断这个归类对不对。

## 用户口径（2026-09-25）

> "原则上有幻觉风险的可以不显示数值，可信度低的也不做倾向分析。"

两条都落在这里：

  1. **可信度低的（< `MIN_CREDIBILITY_FOR_TONE`）不做倾向分析** ——
     抽错了也没人会发现，而用户会把它当成平台的判断
  2. **有幻觉风险的项不显示数值** —— `confidence` 只在
     **规则层与模型层一致**时给出；不一致时 `tone="未定"`，
     界面上不显示倾向标签

## 实测的幻觉（所以才有拦截）

本地 `qwen2.5:1.5b` 在 5 条样本上出了 4 类错，全部实测：

| 幻觉 | 实例 |
|---|---|
| 把 JSON 模板当答案抄回 | `codes: ["6位代码"]` |
| 空串占位 | `codes: [""]` |
| 标点被改写（不再逐字） | `phrases: ["据传,未经证实"]`（原文是全角逗号） |
| **判定相反** | 「中标12.5亿元订单，机构上调盈利预测」被判 `中性`（规则层 4 个偏多词） |

最后一条最危险：**不是抽取失败，是判断错了**。所以判据不能只看模型 ——
必须与规则层交叉验证。

## 拦截策略

    tone 层  取模型与规则的**交集**；不一致 → "未定"（不显示倾向）
    phrases  逐字子串校验，**丢弃所有**非逐字的（不做模糊匹配 ——
             模糊匹配会把"改写"也放进来，那就失去了"可核对"的意义）
    codes    必须是**原文中出现的 6 位数字**。这是合规要求（设计稿"抽取的
             股票代码必须能在原文找到依据"），顺带挡掉 `["6位代码"]`
    confidence  只在两层一致时给值，且取两者较小值（保守）
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final

from src.domain.intel.credibility import MIN_CREDIBILITY_FOR_TONE

logger = logging.getLogger(__name__)

#: 倾向取值。`未定` 是**一等公民**而不是"失败" ——
#: 两层不一致时它就是正确答案：我们确实不知道原文什么语气。
TONE_BULL: Final = "偏多"
TONE_BEAR: Final = "偏空"
TONE_NEUTRAL: Final = "中性"
TONE_UNKNOWN: Final = "未定"

VALID_TONES: Final[frozenset[str]] = frozenset(
    {TONE_BULL, TONE_BEAR, TONE_NEUTRAL})

#: 依据词组的长度下限（太短的词组在任何文本里都能找到，没有核对价值）
MIN_PHRASE_CHARS: Final = 3

#: 单条文本送模型的最大字符数（防 prompt 膨胀；摘要已在契约层截断过）
MAX_TEXT_CHARS: Final = 600

#: **A 股**股票代码（6 位，且以真实存在的板块前缀开头）
#:
#: ⚠️ 不能只匹配 `\d{6}`。实测踩到：别国事件里的金额串会被当股票代码 ——
#: "金砖国家新开发银行与南非签署**2亿美元**项目贷款" 里抠出了 `222500`、
#: `256500` 这种根本不存在的代码，还带着 `codes` 上了界面。
#: 放开匹配等于给用户看了假的标的。
#:
#: 现行有效前缀（2026）：
#:   沪市 600/601/603/605（主板）、688（科创板）
#:   深市 000/001/002/003（主板+中小）、300/301（创业板）
#: ⚠️ 各分支的**位数必须各自配平到 6 位** —— 第一版把 `8[3-9]`（2 位）
#: 和 `60[0135]`（4 位）塞进同一个非捕获组再补 `\d{3}`，
#: 于是 2 位前缀的只匹配到 5 位、永远配不上北交所代码（实测 `830799` 漏掉）。
_CODE_RE: Final = re.compile(
    r"(?<!\d)("
    r"60[0135]\d{3}"      # 沪主板
    r"|688\d{3}"          # 科创板
    r"|00[0123]\d{3}"     # 深主板/中小
    r"|30[01]\d{3}"       # 创业板
    r"|43\d{4}"           # 北交所（430xxx）
    r"|8[3-9]\d{4}"       # 北交所（83/87/88 开头）
    r")(?!\d)"
)

#: 情绪/方向词表 —— 与 `llm_policy._BULL/_BEAR` 同源，
#: 但这里**不 import 它**：那个模块管"该走哪一层"，本模块管"语气是什么"，
#: 复用会让改一处的词表意外影响另一处的路由决策。
_BULL_WORDS: Final[tuple[str, ...]] = (
    "上调", "超预期", "增长", "突破", "放量", "中标", "订单", "扩产",
    "扭亏", "新高", "提价", "获批", "量产", "回购", "增持", "涨停",
    "改善", "回暖", "提升", "受益",
)
_BEAR_WORDS: Final[tuple[str, ...]] = (
    "下调", "低于预期", "下滑", "亏损", "减持", "质押", "违规", "处罚",
    "退市", "停产", "降价", "解禁", "商誉减值", "跌停", "承压", "恶化",
)


# ======================================================================
# 规则层
# ======================================================================

@dataclass
class RuleTone:
    tone: str = TONE_UNKNOWN
    bull_hits: list[str] = field(default_factory=list)
    bear_hits: list[str] = field(default_factory=list)
    confidence: float = 0.0


def rule_tone(text: str) -> RuleTone:
    """纯词表计数。**可复算、零成本、零幻觉** —— 所以它是交叉验证的基准。"""
    s = text or ""
    bull = [w for w in _BULL_WORDS if w in s]
    bear = [w for w in _BEAR_WORDS if w in s]
    if not bull and not bear:
        return RuleTone()
    if len(bull) > len(bear):
        tone = TONE_BULL
    elif len(bear) > len(bull):
        tone = TONE_BEAR
    else:
        tone = TONE_NEUTRAL
    # 差距越大越有把握；上限 0.9（词表法不该自称完全确定）
    gap = abs(len(bull) - len(bear))
    conf = 0.5 + 0.1 * gap if gap else 0.4
    return RuleTone(tone=tone, bull_hits=bull, bear_hits=bear,
                    confidence=min(round(conf, 2), 0.9))


# ======================================================================
# 抽取结果
# ======================================================================

@dataclass
class ToneResult:
    """一条情报的原文倾向 + **可核对的依据**。"""

    tone: str = TONE_UNKNOWN
    #: 原文词组（**逐字来自原文**，已过子串校验）
    phrases: list[str] = field(default_factory=list)
    #: 原文中出现的 6 位代码（已过"必须在原文里"校验）
    codes: list[str] = field(default_factory=list)
    #: 0~1。**只在两层一致时给值**；不一致时为 `None`（不显示数值）
    confidence: float | None = None
    #: 判定来源：`rules` | `rules+llm` | `skipped`
    source: str = "rules"
    #: 面向用户的一句话（**不含来源标识**）
    explain: str = ""
    #: 被拦截掉的内容（仅供日志与审计，**不出接口**）
    rejected: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tone(self) -> bool:
        return self.tone in VALID_TONES

    def to_public(self) -> dict[str, Any]:
        """**白名单构造** —— 新字段默认不出接口。"""
        out: dict[str, Any] = {
            "tone": self.tone,
            # 界面靠它决定"显示标签还是显示未定"
            "has_tone": self.has_tone,
            "phrases": list(self.phrases),
            "codes": list(self.codes),
            "confidence": self.confidence,
            "source": self.source,
            "explain": self.explain,
        }
        return out


def _parse_json(raw: str) -> dict[str, Any] | None:
    """从模型输出里取 JSON。**只认 JSON，不做"猜意图"式修补。**

    实测 `json_mode=True` 时本地模型基本会给纯 JSON；但偶尔会带
    ```json 围栏。这里只剥围栏，不做任何字段补全 ——
    补全等于替模型编造它没说过的内容。
    """
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def validate_extraction(obj: dict[str, Any], text: str) -> tuple[
        str, list[str], list[str], dict[str, Any]]:
    """把模型输出过一遍拦截，返回 `(tone, phrases, codes, rejected)`。

    ## 三条拦截，每一条都对应实测出现过的一次幻觉

      · `tone` 必须是合法枚举 —— 实测模型会把模板文字抄回来
      · `phrases` 必须**逐字**出现在原文 —— 实测被改写过标点
      · `codes` 必须是原文里真有的 6 位数字 —— 实测出现过
        `["6位代码"]`（模板文字）与 `[""]`（空串）
    """
    rejected: dict[str, Any] = {}

    raw_tone = str(obj.get("tone") or "").strip()
    tone = raw_tone if raw_tone in VALID_TONES else TONE_UNKNOWN
    if raw_tone and tone == TONE_UNKNOWN:
        rejected["tone"] = raw_tone

    phrases: list[str] = []
    bad_phrases: list[str] = []
    for p in (obj.get("phrases") or []):
        s = str(p or "").strip()
        if not s:
            continue
        if len(s) < MIN_PHRASE_CHARS or s not in text:
            bad_phrases.append(s)
            continue
        if s not in phrases:
            phrases.append(s)
    if bad_phrases:
        rejected["phrases"] = bad_phrases

    src_codes = set(_CODE_RE.findall(text))
    codes: list[str] = []
    bad_codes: list[str] = []
    for c in (obj.get("codes") or []):
        s = str(c or "").strip()
        if not s:
            continue
        if s in src_codes and s not in codes:
            codes.append(s)
        else:
            bad_codes.append(s)
    if bad_codes:
        rejected["codes"] = bad_codes
    # 兜底：模型没给代码但原文里有 —— 用规则抓（原文里真有，不算幻觉）
    if not codes and src_codes:
        codes = sorted(src_codes)[:3]

    return tone, phrases, codes, rejected


# ======================================================================
# 编排
# ======================================================================

def extract_tone(*, text: str, credibility_score: int,
                 llm_obj: dict[str, Any] | None = None) -> ToneResult:
    """合成一条最终倾向。

    `llm_obj` 为 `None` 表示**没有调模型**（低可信 / 模型不可用），
    此时只用规则层。这样"模型挂了"不会让功能消失，只是精度下降 ——
    但界面能看出来（`source` 字段）。

    ## 交叉验证：不一致就不给倾向

    实测最危险的一类错不是"抽取失败"，而是**判断相反**：
    「中标12.5亿元订单，机构上调盈利预测」被模型判成 `中性`，
    而规则层命中 4 个偏多词。这时：
      · 信模型 → 一条明显的偏多原文被标成中性（漏报）
      · 信规则 → 词表法也会被"利空出尽"这类反讽骗到
    所以**两个都不信**，给 `未定`。用户看到的就是"这条我们不做倾向归类"，
    那比一个可能错的标签诚实得多。
    """
    rule = rule_tone(text)

    # 低可信：不做倾向分析（用户口径）
    if credibility_score < MIN_CREDIBILITY_FOR_TONE:
        return ToneResult(
            tone=TONE_UNKNOWN,
            phrases=[],
            codes=sorted(set(_CODE_RE.findall(text or "")))[:3],
            confidence=None,
            source="skipped",
            explain=(f"可信度 {credibility_score} 低于 {MIN_CREDIBILITY_FOR_TONE}，"
                     f"不做原文倾向分析"))

    # 没调模型：只用规则
    if llm_obj is None:
        if rule.tone == TONE_UNKNOWN:
            return ToneResult(
                tone=TONE_UNKNOWN, source="rules",
                codes=sorted(set(_CODE_RE.findall(text or "")))[:3],
                explain="规则层无倾向词，且未做语义抽取")
        return ToneResult(
            tone=rule.tone,
            phrases=(rule.bull_hits if rule.tone == TONE_BULL
                     else rule.bear_hits if rule.tone == TONE_BEAR else []),
            codes=sorted(set(_CODE_RE.findall(text or "")))[:3],
            confidence=rule.confidence,
            source="rules",
            explain=f"词表计数（多{len(rule.bull_hits)}/空{len(rule.bear_hits)}）")

    tone, phrases, codes, rejected = validate_extraction(llm_obj, text or "")

    # ── 交叉验证 ──
    if rule.tone == TONE_UNKNOWN:
        # 规则层没意见：模型说了算，但**不给数值置信度** ——
        # 只有一个来源的判断，没资格自称有把握
        final = tone
        conf = None
        why = "规则层无倾向词，采用语义抽取" if tone != TONE_UNKNOWN else "无依据"
    elif tone == TONE_UNKNOWN:
        final = rule.tone
        conf = rule.confidence
        why = f"语义抽取无结论，采用词表计数（多{len(rule.bull_hits)}/空{len(rule.bear_hits)}）"
    elif tone == rule.tone:
        final = tone
        conf = min(rule.confidence, 0.85)   # 一致但仍保守
        why = "词表计数与语义抽取一致"
    else:
        # ★ 不一致 → 不猜
        final = TONE_UNKNOWN
        conf = None
        why = (f"词表计数（{rule.tone}）与语义抽取（{tone}）不一致，"
               f"不给出倾向")

    # ★ 依据词组的**兜底**：模型给的词组可能全被拦掉（实测：标点被改写
    # 导致"逐字"校验失败），此时 final 有倾向但 phrases 为空 ——
    # 界面上就会显示"偏多"却**没有依据**。对一个必须能被核对的字段，
    # 那比不显示更糟（用户无法判断归类对不对）。
    #
    # 兜底用**规则层命中的词**：它们按定义就是原文子串，逐字可核。
    if final != TONE_UNKNOWN and not phrases:
        phrases = list(rule.bull_hits if final == TONE_BULL
                       else rule.bear_hits if final == TONE_BEAR else [])

    # 标 `未定` 时把依据也清掉 —— 否则界面上会出现
    # "未定" + 一串看起来像证据的词组，等于变相给了倾向
    if final == TONE_UNKNOWN:
        phrases = []
        codes = codes or sorted(set(_CODE_RE.findall(text or "")))[:3]

    res = ToneResult(
        tone=final, phrases=phrases, codes=codes, confidence=conf,
        source="rules+llm", explain=why, rejected=rejected)
    if rejected:
        # 只记数量，不记内容 —— 内容可能夹带上游文本
        logger.info("倾向抽取拦截：%s", {k: len(v) for k, v in rejected.items()})
    return res


#: 送模型的系统提示。措辞很讲究：
#:   · **"只输出 JSON"** —— 实测不加这句模型会写一段解释
#:   · **"逐字"** —— 实测不加这句 phrases 会被改写（标点被换掉）
#:   · **"不得补充"** —— 防止模型把常识补进来
#:   · 明确"只判原文语气、不做预测" —— 合规边界写进提示词
SYSTEM_PROMPT: Final = (
    "你是信息抽取器，只做一件事：判断**这段第三方原文自己**的语气。"
    "不是你的观点，不是预测，不要给任何投资建议。"
    "只输出 JSON，不要解释。"
    "phrases 必须**逐字**来自输入文本，不得改写标点、不得补充常识。"
    "codes 只填文本中**真实出现**的 6 位股票代码，没有就填空数组。"
)


def build_prompt(text: str) -> str:
    """单条抽取的 prompt。"""
    return (
        '返回 JSON：{"tone":"偏多|偏空|中性",'
        '"phrases":["原文逐字词组"],"codes":["6位代码"]}'
        "\n文本：" + (text or "")[:MAX_TEXT_CHARS]
    )


__all__ = [
    "MAX_TEXT_CHARS",
    "MIN_PHRASE_CHARS",
    "SYSTEM_PROMPT",
    "TONE_BEAR",
    "TONE_BULL",
    "TONE_NEUTRAL",
    "TONE_UNKNOWN",
    "VALID_TONES",
    "RuleTone",
    "ToneResult",
    "build_prompt",
    "extract_tone",
    "rule_tone",
    "validate_extraction",
]
