"""LLM输出幻觉防护（Hallucination Guard）。

三层校验（互不短路，完整审计）：
1. 数字溯源：输出中的具体数字（百分比/金额/比率）必须可在输入数据中找到
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
_PERCENT_RE = re.compile(r"-?\d+(?:\.\d+)?\s*%")
_AMOUNT_RE = re.compile(r"-?\d+(?:\.\d+)?\s*(?:亿|万|千万|百万|万亿|亿元|万元)")
_RATIO_RE = re.compile(r"-?\d+(?:\.\d+)?\s*(?:倍|PE|PB|ROE|EPS)")

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
        if check_numbers and input_norm:
            output_numbers: list[str] = []
            for regex in (_PERCENT_RE, _AMOUNT_RE, _RATIO_RE):
                output_numbers.extend(regex.findall(agent_output))
            seen: set[str] = set()
            for num in output_numbers:
                norm = re.sub(r"\s+", "", num)
                if norm in seen:
                    continue
                seen.add(norm)
                if norm not in input_norm:
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
