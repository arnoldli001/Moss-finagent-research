"""摘要生成：把原文压缩成**一句话事实**（本地模型 + 硬校验）。

> 用户口径（2026-09-25）："所有情报信息的内容，都可以考虑使用本地大模型
> 快速的摘要、总结输出，而不是原文输出+被截断。"

## 为什么现在是"原文 + 截断"，为什么它不好

`IntelItem.to_public()` 按类型把摘要截到 160~300 字。那有两个问题：

1. **截断点往往落在关键信息之前**。一条研报 3000 字，前面是背景铺垫，
   结论、数字、标的在后面 —— 截前 260 字等于把最有用的部分丢掉。
   实测用户看到的 `【华创地产链…】2026年月统计局房地产数据速览` 只有 33 字，
   而原文更长、信息更多。
2. **原文有大量冗余**（"我们认为"、"综上所述"、平台表情标记、日期签名）。
   用户要的是"这条讲了什么"，不是逐字原文。

模型压缩能同时解决两者：保留**数字与标的**，去掉冗余与铺垫。

## ⚠️ 摘要的核心风险：模型会**编**

倾向那一步的实测已经证明本地小模型会出错（把 JSON 模板当答案抄回、
判定相反）。摘要的风险更直接 —— 一个编造的数字（"营收增长 30%"）
看起来完全像真的，而且**没有任何东西能提示它是编的**。

所以这里用**抽取式约束**而不是放任生成：

    · 保留原文里的**数字**（百分比/金额/数量）—— 校验摘要中的数字
      必须能在原文找到，找不到就**丢弃整个摘要**
    · 保留原文里的**A 股代码**
    · 不许出现"建议/目标价/评级/看多"等越线表述
    · 长度硬上限；太短（没压出东西）或太长（没起到压缩作用）都丢弃

**校验不过就退回原文截断** —— 宁可给一段没压过的原文，也不要给一句
编造的事实。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 摘要长度上限（字符）。
#:
#: 手机上正文一行约 18 字，两行 36 字、三行 54 字。
#: 90 字约五行 —— 卡片列表里还能接受；再长就退回"原文截断"的体验了。
MAX_SUMMARY_CHARS: Final = 90

#: 摘要长度下限。低于它说明模型没压出东西（或原文本来就短）。
MIN_SUMMARY_CHARS: Final = 12

#: 送模型的文本上限（原文可能 3000+ 字，全送会拖慢并稀释注意力）
MAX_INPUT_CHARS: Final = 1500

#: 数字形态：百分比 / 金额 / 数量 / 倍数
_NUM: Final = re.compile(
    r"-?\d+(?:\.\d+)?\s*(?:%|个百分点|亿元|万元|亿|万|吨|台|辆|片|GW|MW|倍|家|只)?")

#: A 股代码（与 `tone._CODE_RE` 同口径）
_CODE: Final = re.compile(
    r"(?<!\d)(?:60[0135]\d{3}|688\d{3}|00[0123]\d{3}|30[01]\d{3}"
    r"|43\d{4}|8[3-9]\d{4})(?!\d)")

#: 越线表述：摘要里**绝对不能**出现（合规硬约束）
_BANNED: Final = re.compile(
    r"建议(?:买入|卖出|关注|增持|减持)|目标价|评级|买入|卖出|看多|看空|"
    r"追高|抄底|止损|止盈|加仓|减仓|建仓|推荐买|值得买")

#: 摘要里允许的"压缩痕迹"提示词（模型常以此为开头，无害但可剥）
_LEAD: Final = re.compile(r"^(?:摘要|总结|一句话|要点)[：:]\s*")


@dataclass
class SummaryResult:
    ok: bool
    text: str = ""
    reason: str = ""
    #: 被拦掉的原因（供日志与排障，**不出接口**）
    rejected: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rejected is None:
            self.rejected = {}


def parse_json(raw: str) -> dict[str, Any] | None:
    """从模型输出取 JSON。只剥 ``` 围栏，**不做字段补全**（补全=替模型编造）。"""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _numbers(text: str) -> set[str]:
    """抽出文本里的数字（去单位、去空白后比较）。

    为什么按"数字本体"比而不是整串：原文写 `同比+30%`、摘要写 `同比增30%`
    都是同一件事，带单位比会把合法的压缩判成幻觉。
    """
    out: set[str] = set()
    for m in _NUM.finditer(text or ""):
        for d in re.findall(r"-?\d+(?:\.\d+)?", m.group(0)):
            # 归一：去尾随 0（"30.0" 与 "30" 是同一个数）
            out.add(d.rstrip("0").rstrip(".") if "." in d else d)
    return out


def validate_summary(summary: str, source: str) -> SummaryResult:
    """校验模型摘要。**任何一条不过就丢弃**（调用方退回原文截断）。

    校验项与理由：

      `empty`      空/太短（< `MIN_SUMMARY_CHARS`）→ 没压出东西
      `too_long`   超过上限 → 没起到压缩作用
      `banned`     出现买卖建议/目标价/评级 → **合规红线**
      `hallucinated_number`  摘要里的数字原文找不到 → 编造
      `no_number`  原文有数字而摘要一个都没有 → 把关键信息压没了
                   （对快讯/研报这类以数字为核心的内容，这等于失真）
    """
    s = _LEAD.sub("", (summary or "").strip())
    s = re.sub(r"\s+", " ", s)
    if len(s) < MIN_SUMMARY_CHARS:
        return SummaryResult(False, s, "empty")
    if len(s) > MAX_SUMMARY_CHARS * 2:
        # 放宽到 2 倍才判"太长"：中文压缩比难精确控，卡死会大量误杀
        return SummaryResult(False, s, "too_long")
    if _BANNED.search(s):
        return SummaryResult(False, s, "banned")

    src_nums = _numbers(source)
    sum_nums = _numbers(s)
    if sum_nums:
        bad = sorted(n for n in sum_nums if n not in src_nums)
        if bad:
            return SummaryResult(False, s, "hallucinated_number",
                                 {"bad_numbers": bad[:5]})
    elif src_nums:
        # 原文有数字（"营收+30%"），摘要里一个都没有 → 关键信息被压没了
        return SummaryResult(False, s, "no_number")

    # 代码同样必须在原文里
    src_codes = set(_CODE.findall(source or ""))
    bad_codes = [c for c in _CODE.findall(s) if c not in src_codes]
    if bad_codes:
        return SummaryResult(False, s, "hallucinated_code",
                             {"bad_codes": bad_codes[:3]})

    # 截到上限（校验过了，这里只做长度收敛）
    if len(s) > MAX_SUMMARY_CHARS:
        s = s[:MAX_SUMMARY_CHARS].rstrip() + "…"
    return SummaryResult(True, s)


def build_prompt(title: str, text: str) -> str:
    """单条摘要的 prompt。

    措辞要点（每条都对应一次实测问题）：
      · "只输出 JSON" —— 不加这句模型会写一段解释
      · "不得编造数字" —— 摘要里最危险的就是看起来很像真的假数字
      · "保留原文数字" —— 否则模型会把数字概括成"大幅增长"
      · 明确"不做判断、不给建议" —— 合规边界写进提示词
    """
    body = f"{title}\n{text}".strip()[:MAX_INPUT_CHARS]
    return (
        "把下面这条财经信息压缩成**一句话**，只输出 JSON，不要解释。\n"
        '{"summary":"一句话摘要"}\n'
        "要求：保留原文中的**数字与公司名/代码**，不得编造、不得换算单位；"
        "去掉铺垫、客套与重复；只陈述原文说了什么，不做判断、不给建议、"
        f"不预测涨跌。摘要不超过 {MAX_SUMMARY_CHARS} 字。\n"
        "原文：\n" + body
    )


#: 系统提示（与 `tone.SYSTEM_PROMPT` 分开 —— 两件事的约束不同）
SYSTEM_PROMPT: Final = (
    "你是财经信息摘要器。只做压缩，不做解读。"
    "只输出 JSON，不要任何解释。"
    "摘要必须完全基于原文：不得引入原文没有的数字、公司、事件。"
    "不做投资建议，不使用'建议买入/目标价/评级'这类措辞。"
)


__all__ = [
    "MAX_INPUT_CHARS",
    "MAX_SUMMARY_CHARS",
    "MIN_SUMMARY_CHARS",
    "SYSTEM_PROMPT",
    "SummaryResult",
    "build_prompt",
    "parse_json",
    "validate_summary",
]
