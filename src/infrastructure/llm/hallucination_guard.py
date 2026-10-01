"""LLM输出幻觉防护（Hallucination Guard）。

三层校验（互不短路，完整审计）：
1. 数字溯源：输出中的具体数字（百分比/金额/比率）必须可在输入数据中找到
   —— 判据是**数值等价**（单位换算 + 末位精度），不是字符串相等；
   详见 `_grounded_by_value`（`万亿`/`亿` 的互换与 `90.18`/`90.183` 的精度差
   都是同一个读数，判成幻觉会让护栏自己失去可信度）
2. 股票代码grounding：输出中的6位股票代码必须来自输入数据
3. 来源标注：涉及新闻/财报/数据的陈述必须标注来源（system prompt已强制但需校验）

设计原则：
- 失败默认保守：无法判定时标为"疑似幻觉"，附加警示而非直接拒绝
- 轻量纯规则：不依赖额外LLM调用（省token），LLM-as-Judge作为可选扩展点
- 与现有JSON修复循环协同：先parse JSON→再校验内容→有问题则修复重试

参考：moss-finance-assistant governance/guardrails/hallucination_guard.py
适配：Moss-finagent-research的输入是结构化prompt（含数据溯源标签），
校验对象是LLM输出的JSON（conclusion字段含自然语言陈述）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 数字提取正则
#
# ⚠️⚠️ **金额单位的分支顺序就是语义**：正则的选择分支**先匹配先赢**，
#    所以必须"长的在前"。原先把 `亿` 写在 `万亿` 前面 —— `1.70万亿` 被抠成
#    `1.70万`（把万亿读成万，差 1 亿倍），而这个 token 要拿去和输入做**字符串**
#    比对 ⇒ 必然判"未在输入数据中找到"。
#    现场（2026-09-29，`docs/PRD.md` §19.16.5 第 2 条）：结论写 `1.70万亿`、
#    输入是 `17028亿` → 被扣上"幻觉防护提示"。
_AMOUNT_UNITS: tuple[str, ...] = ("万亿", "亿元", "万元", "千万", "百万", "亿", "万")
_AMOUNT_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*(" + "|".join(_AMOUNT_UNITS) + r")")
#: 金额单位 → 「亿」的换算因子（数值等价判定用）
_AMOUNT_UNITS_TO_YI: dict[str, float] = {
    "万亿": 10_000.0, "亿": 1.0, "亿元": 1.0,
    "千万": 0.1, "百万": 0.01, "万": 1e-4, "万元": 1e-4,
}
_PERCENT_RE = re.compile(r"-?\d+(?:\.\d+)?\s*%")
_RATIO_RE = re.compile(r"-?\d+(?:\.\d+)?\s*(?:倍|PE|PB|ROE|EPS)")
#: 任意数值（不带单位）—— 数值等价判定的候选池
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
#: 把一个输出 token 拆成「数值 + 单位」（单位可为空串）
_TOKEN_RE = re.compile(r"\s*(-?\d+(?:\.\d+)?)\s*([^\d\s]*)\s*$")

# 股票代码：6位数字（前后非数字）
_STOCK_CODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")

# 需要来源标注的触发词
_NEEDS_SOURCE_RE = re.compile(
    r"新闻|消息|报道|公告|数据显示|财报|业绩|研报|"
    r"PE|PB|ROE|营收|净利|涨|跌|成交",
    re.IGNORECASE,
)

# 来源标注模式
_SOURCE_MARK_PATTERNS = [
    re.compile(r"来源[：:]\s*\S", re.IGNORECASE),
    re.compile(r"引自[：:]\s*\S", re.IGNORECASE),
    re.compile(r"根据.{0,20}(?:公告|报道|数据|财报|研报)", re.IGNORECASE),
    re.compile(r"据.{0,10}(?:报道|数据|公告)", re.IGNORECASE),
    re.compile(r"溯源", re.IGNORECASE),
    re.compile(r"数据点", re.IGNORECASE),
]


# ============================================================
# 数值等价判定（第 1 层校验的判据，不是"模糊匹配"）
# ============================================================
#
# ## 现场（`docs/PRD.md` §19.16.5 第 2 条，用户截图报障）
#
# 结论写 `1.70万亿` 而输入是 `17028亿`、写 `90.18%` 而输入是 `90.183`，
# 两个都被标成"未在输入数据中找到的数字"。
# **这是同一个读数的两种写法**，判成幻觉会让这条护栏自己失去可信度
# （狼来了）—— 真出幻觉时没人再看它。
#
# ## 判据（两条，各自有明确的语义，不是"差不多就算过"）
#
# ① **末位精度容差**：输出的**最后一位小数**决定容差。`90.18` 的容差是
#    `±0.005`，即"输入里存在一个四舍五入到两位就等于 90.18 的读数"。
#    `90.183` 满足（|90.183-90.18|=0.003），`91.7` 不满足。
# ② **金额单位换算**：`万亿 / 亿 / 千万 / 百万 / 万` 先换算到同一单位再比。
#    `1.70万亿` → 17000 亿（容差 ±50 亿）→ 命中输入里的 `17028亿`。
#
# ## 为什么**整数百分比/倍数不放宽**
#
# 输入上下文里全是日期（`2026-09-18`）、置信度（`c0.95`）—— 日期自带一堆
# 被边界分隔的整数。若整数也走"裸数字命中"，编一个"净利率 18%"会**碰巧**
# 命中 `2026-09-18` 里的 18 ⇒ 把真幻觉洗成通过。
# 所以整数只在**金额单位**这一维放宽（候选池只含输入里的金额 token，
# 日期整数进不来），百分号/倍数整数保持"字面命中"的原判据。


def _decimals(number_text: str) -> int:
    """小数位数（`6.76` → 2，`6` → 0）。"""
    return len(number_text.partition(".")[2])


def _rounding_tolerance(number_text: str) -> float:
    """末位精度容差：输入四舍五入到该精度就等于输出值。"""
    return 0.5 * (10.0 ** -_decimals(number_text))


def _input_numbers(text: str) -> tuple[list[float], list[float], list[float]]:
    """输入上下文里的 `(全部数值, 金额数值[换算成亿], 无单位数值[按"元"再换算成亿])`。

    ⚠️ 第三项是 2026-09-29 真实端到端补的：库里**金额字段口径是元**
    （`板块资金流:银行` 的 `value=-896960544.0`），而结论里很自然地写成
    **`-8.97亿元`** —— 源给元、结论用亿元，是**同一个数**，不是幻觉。
    只按"带金额单位的 token"建候选池会让这种**正确的改写**被标成
    「未在输入数据中找到的数字」（实测现场：A11/A20 的结论末尾都挂了这句警告）。
    """
    amounts = [
        float(m.group(1)) * _AMOUNT_UNITS_TO_YI[m.group(2)]
        for m in _AMOUNT_RE.finditer(text)
    ]
    plain = [float(m.group(0)) for m in _NUMBER_RE.finditer(text)]
    #: 无单位数值按「元」理解后再折算成亿（`-896960544.0` → -8.9696 亿）
    plain_as_yi = [v / 1e8 for v in plain]
    return plain, amounts, plain_as_yi


def _extract_output_numbers(text: str) -> list[str]:
    """输出文本里的数字 token（**保留单位**，供等价判定识别量纲）。"""
    tokens = [m.group(0) for m in _AMOUNT_RE.finditer(text)]
    tokens.extend(_PERCENT_RE.findall(text))
    tokens.extend(_RATIO_RE.findall(text))
    return tokens


def _grounded_by_value(
    token: str, plain: list[float], amounts_yi: list[float],
    plain_as_yi: list[float],
) -> bool:
    """该 token 是否与输入里的某个读数**数值等价**（见上文两条判据）。"""
    m = _TOKEN_RE.match(token)
    if m is None:
        return False
    number_text, unit = m.group(1), m.group(2)
    value = float(number_text)
    tolerance = _rounding_tolerance(number_text)
    factor = _AMOUNT_UNITS_TO_YI.get(unit)
    if factor is not None:
        # ⚠️ **金额这一维按绝对值比**（2026-09-29 实测）：中文财经散文里
        #   方向由「净流入/净流出」承载而不是符号 —— 库里 `板块资金流:银行`
        #   是 `-896960544.0`（元），A11/A20 写「主力**净流出** 8.97亿元」，
        #   按符号比会判成"数字没找到"，于是**每一句正确的金额改写**都被挂上警告。
        #   而这条护栏**从不校验方向词**（它只查数字），所以按符号比只买到
        #   系统性假 alarms，买不到真防护。数量级/数值本身仍然严格。
        scaled = value * factor
        tol = tolerance * factor
        if any(abs(abs(c) - abs(scaled)) <= tol for c in amounts_yi):
            return True
        # 源给"元"（无单位）、结论写成"亿元" → 同一个数（见 `_input_numbers`）
        return any(abs(abs(c) - abs(value)) <= tolerance for c in plain_as_yi)
    if _decimals(number_text) == 0:
        return False   # 整数百分比/倍数：只认字面命中（理由见上文）
    return any(abs(c - value) <= tolerance for c in plain)


@dataclass
class HallucinationReport:
    """幻觉防护报告。"""

    passed: bool
    confidence: float = 1.0
    unverified_numbers: list[str] = field(default_factory=list)
    unverified_stock_codes: list[str] = field(default_factory=list)
    citation_gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "confidence": round(self.confidence, 3),
            "unverified_numbers": self.unverified_numbers[:5],
            "unverified_stock_codes": self.unverified_stock_codes[:5],
            "citation_gaps": self.citation_gaps[:3],
        }

    def render_warning(self) -> str:
        """未通过时渲染警示文本，附加到结论末尾。"""
        if self.passed:
            return ""
        lines = ["[幻觉防护提示]以下陈述未经输入数据完全验证，请谨慎参考："]
        if self.unverified_numbers:
            lines.append(
                "未在输入数据中找到的数字：" + "、".join(self.unverified_numbers[:3])
            )
        if self.unverified_stock_codes:
            lines.append(
                "未在输入数据中找到的股票代码：" + "、".join(self.unverified_stock_codes[:3])
            )
        if self.citation_gaps:
            lines.append("缺少来源标注的陈述：" + "；".join(self.citation_gaps[:2]))
        return " ".join(lines)


class HallucinationGuard:
    """幻觉防护校验器（纯规则，零额外LLM调用）。"""

    @staticmethod
    def verify(
        agent_output: str,
        input_context: str,
        *,
        check_numbers: bool = True,
        check_stock_codes: bool = True,
        check_citations: bool = False,
    ) -> HallucinationReport:
        """校验LLM输出是否grounded于输入数据。

        Args:
            agent_output: LLM输出的结论文本（JSON中conclusion字段的值）
            input_context: 输入给LLM的prompt全文（含数据溯源标签）
            check_numbers: 是否校验数字溯源
            check_stock_codes: 是否校验股票代码grounding
            check_citations: 是否校验来源标注（默认关闭，system prompt已强制）
        """
        report = HallucinationReport(passed=True, confidence=1.0)
        if not agent_output:
            return report

        input_norm = re.sub(r"\s+", "", input_context) if input_context else ""

        # 1. 数字溯源：输出中的数字必须可在输入中找到
        #
        # 两级判据：① 字面命中（原判据，最快且零误判）；② 字面不中时按
        # **数值等价**复核（单位换算 + 末位精度），避免把"同一个数的另一种写法"
        # 判成幻觉 —— 详见 `_grounded_by_value` 的现场说明。
        # ②的候选池**惰性计算**：只有真的出现字面不中时才去扫输入上下文。
        if check_numbers and input_norm:
            plain_numbers: list[float] | None = None
            amount_values: list[float] | None = None
            plain_as_yi: list[float] | None = None
            seen: set[str] = set()
            for num in _extract_output_numbers(agent_output):
                norm = re.sub(r"\s+", "", num)
                if norm in seen:
                    continue
                seen.add(norm)
                if norm in input_norm:
                    continue
                if plain_numbers is None:
                    plain_numbers, amount_values, plain_as_yi = _input_numbers(
                        input_context)
                if _grounded_by_value(num, plain_numbers, amount_values,
                                      plain_as_yi):
                    continue
                report.unverified_numbers.append(num)

        # 2. 股票代码grounding
        if check_stock_codes and input_norm:
            input_codes = set(_STOCK_CODE_RE.findall(input_context or ""))
            output_codes = set(_STOCK_CODE_RE.findall(agent_output))
            if input_codes:
                unsupported = output_codes - input_codes
                report.unverified_stock_codes.extend(sorted(unsupported))

        # 3. 来源标注检查
        if check_citations and _NEEDS_SOURCE_RE.search(agent_output):
            has_source = any(p.search(agent_output) for p in _SOURCE_MARK_PATTERNS)
            if not has_source:
                sentences = re.split(r"[。！？\n]", agent_output)
                for s in sentences:
                    s = s.strip()
                    if s and re.search(r"\d", s) and _NEEDS_SOURCE_RE.search(s):
                        report.citation_gaps.append(s[:80])
                        if len(report.citation_gaps) >= 3:
                            break

        # 汇总判定
        total_issues = (
            len(report.unverified_numbers)
            + len(report.unverified_stock_codes)
            + len(report.citation_gaps)
        )
        report.passed = total_issues == 0
        report.confidence = max(0.0, 1.0 - 0.15 * total_issues)
        return report
