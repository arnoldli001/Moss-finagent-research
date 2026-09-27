"""A17 投研建议Agent（决策层）。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from src.core.base_agent import BaseAgent
from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, TraceStep, coerce_confidence
from src.domain.agents.analysis.base import parse_llm_json
from src.infrastructure.llm import LLMGateway
from src.infrastructure.llm.hallucination_guard import HallucinationGuard


class RecommendationPayload(BaseModel):
    """A17输入：上游分析Agent输出的聚合。"""

    analyses: list[dict[str, Any]] = Field(default_factory=list)
    """各分析Agent的AgentOutput摘要（agent_id/conclusion/confidence/result）"""
    focus: str = ""
    user_query: str = ""
    hint: dict[str, Any] = Field(default_factory=dict)
    """本地计算参考（market_liquidity：流动性周期skill研判），LLM只解读不计算"""


class RecommendationAgent(BaseAgent):
    """综合宏观/中观/微观/财务风险四维分析，仲裁冲突，输出投研建议（PRD A17，P0）。"""

    system_prompt = (
        "投研委员会主席，综合四维分析生成投研建议。\n"
        "- 冲突时显式仲裁并给取舍依据，每条逻辑可回溯到分析维度；\n"
        "- 立场与证据强度一致，高风险禁看多；\n"
        "- 有market_liquidity按「总量→三市分项→双创宽度PE分位→结论」引用，"
        "缩量(<2.5万亿)回踩不追高，缺口如实声明不杜撰数字；\n"
        "- 问收益预期给乐观/中性/悲观三档情景+核心假设+证伪信号。"
    )

    def __init__(self, gateway: LLMGateway, agent_id: str = "A17_recommend") -> None:
        super().__init__(agent_id)
        self._gateway = gateway

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "capabilities": ["synthesize", "conflict_arbitration", "stance_decision"],
            "task_tier": "decision",
        }

    def health_check(self) -> bool:
        return True

    def _parse_payload(self, payload: dict[str, Any]) -> RecommendationPayload:
        try:
            return RecommendationPayload.model_validate(payload)
        except ValidationError as exc:
            raise AgentExecutionError(f"{self.agent_id}输入不合法: {exc}") from exc

    def _build_context(self, payload: RecommendationPayload) -> str:
        """完整上下文（向后兼容，单次 execute() 用）：含 result 全字段。"""
        blocks = []
        for a in payload.analyses:
            result = {
                k: v for k, v in (a.get("result") or {}).items()
                if k not in ("tokens_in", "tokens_out", "model_used")
            }
            blocks.append(
                f"### {a.get('agent_id', '?')}（置信度: {a.get('confidence', '?')}）\n"
                f"结论: {a.get('conclusion', '')}\n"
                f"结构化: {json.dumps(result, ensure_ascii=False)}"
            )
        return "\n\n".join(blocks) if blocks else "（无上游分析）"

    def _build_compact_context(self, payload: RecommendationPayload,
                               max_chars_per_block: int = 400) -> str:
        """压缩上下文（ReAct 第 1 步用，2026-09-27 第八轮优化）。

        **问题（审计实证）**：完整 context = 7582 tokens，3 步 ReAct
        各发一遍 = 22,746 tokens_in（占 A17 全程 55%）。

        **做法**：每个上游分析只保留 `{agent_id, confidence, conclusion摘要≤200字,
        2-3 个关键数值}`，单块 ≤400 字符；**5 维 × 400 = 2000 字符 ≈ ~700 tokens**，
        而完整版 7582 tokens → **−91%**。

        ReAct 的关键洞察：**LLM 在第 1 步其实只需要'浓缩结论 + 关键数字'做综合**；
        完整 result 是给单次 execute() 的审计用的（has hallucination guard）。
        ReAct 第 2+ 步用的是 `_summarize_lm_output`，不再重发上游分析。
        """
        import re as _re

        blocks: list[str] = []
        for a in payload.analyses:
            agent_id = str(a.get("agent_id", "?"))
            confidence = str(a.get("confidence", "?"))
            conclusion = str(a.get("conclusion", "")).strip()
            # 截断 conclusion（中文按字符算，英文按词算，统一按字符）
            if len(conclusion) > 200:
                conclusion = conclusion[:197] + "..."
            # 从 result 里抽 2-3 个关键数值（保留数值型字段，跳过元数据/审计字段）
            result = a.get("result") or {}
            SKIP = {"tokens_in", "tokens_out", "model_used", "disclaimer",
                    "stance", "evidence", "trace_id"}
            metrics: list[str] = []
            for k, v in result.items():
                if k in SKIP:
                    continue
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    metrics.append(f"{k}={v}")
                elif isinstance(v, str) and len(v) <= 30 and _re.match(r"^[+\-]?[\d.,%/]+$", v):
                    metrics.append(f"{k}={v}")
                if len(metrics) >= 3:
                    break
            block = (
                f"### {agent_id}（置信度:{confidence}）\n"
                f"结论: {conclusion}"
            )
            if metrics:
                block += f"\n关键数值: {', '.join(metrics)}"
            # 严格截断：避免单块膨胀
            if len(block) > max_chars_per_block:
                block = block[:max_chars_per_block - 3] + "..."
            blocks.append(block)
        return "\n\n".join(blocks) if blocks else "（无上游分析）"

    @staticmethod
    def _render_hint(payload: RecommendationPayload) -> str:
        """流动性周期skill研判 → 紧凑中文文本；无hint时返回占位说明。"""
        liq = (payload.hint or {}).get("market_liquidity") or {}
        text = liq.get("summary_text")
        if text:
            return text + (
                "\n（以上为系统本地计算的量化参考，直接引用其中数字即可，"
                "禁止自行重算或改写；[数据缺口]条目必须在结论中如实声明。）"
            )
        return "（本次无大盘流动性量化参考；如问题涉及买卖时点/仓位，需说明数据缺口）"

    def build_prompt(self, payload: RecommendationPayload, *,
                     react_mode: bool = False,
                     compact: bool = False) -> str:
        """构建A17任务prompt。

        react_mode=True 时输出格式为 {"action": {...}} 或 {"final_answer": {...}}，
        供ReAct执行器解析工具调用；否则直接输出conclusion等字段（execute单次调用用）。

        compact=True（ReAct 第 1 步推荐）：
            使用 `_build_compact_context` —— 每个上游分析压缩到 ≤400 字符，
            只保留结论摘要与 2-3 个关键数值。配合 `ReActExecutor(incremental=True)`
            实现 A17 tokens_in −91%（审计基线 7582 → ~700）。
        """
        if react_mode:
            output_spec = (
                '输出JSON二选一：\n'
                '1. {"action": {"name": "ask_agent"|"query_data", "args": {...}}}\n'
                '   ask_agent: {"receiver": "A10_micro", "question": "..."}\n'
                '   query_data: {"indicator": "PE(TTM)", "limit": 10}\n'
                '   上游冲突/数据缺失时调用，同问题不重问，每Agent最多2次。\n'
                '2. {"final_answer": {"conclusion": "...", "confidence": "...", '
                '"stance": "...", "key_logic": [...], "catalysts": [...], '
                '"risks": [...], "monitoring_points": [...], '
                '"conflicts_resolved": [...], '
                '"position_advice": "仓位区间+节奏", '
                '"expected_return_3_6m": {"bull":"...", "base":"...", '
                '"bear":"...", "assumptions":[...], "invalid_signals":[...]}}}\n'
                '   买卖/仓位问题position_advice必填；收益预期问题expected_return_3_6m必填；'
                '其余给null。\n'
            )
        else:
            output_spec = (
                "输出JSON：conclusion, confidence, stance, key_logic, catalysts, "
                "risks, monitoring_points, conflicts_resolved(冲突仲裁说明), "
                "position_advice, expected_return_3_6m{bull,base,bear,assumptions,"
                "invalid_signals}。买卖问题position_advice必填；收益问题"
                "expected_return_3_6m必填(三档情景+假设+证伪信号)；其余给null。"
                "可选action调工具追问(最多2轮)。\n"
            )
        # ★ ReAct 模式下用压缩上下文（−91% tokens_in，配合 incremental=True）
        context_fn = self._build_compact_context if compact else self._build_context
        return (
            f"## 投研标的/主题\n{payload.focus or '综合'}\n"
            f"## 用户问题（conclusion必须直接回答，禁止绕开问题写模板点评）\n"
            f"{payload.user_query or '无'}\n\n"
            f"## 上游分析结论\n{context_fn(payload)}\n\n"
            f"## 本地量化参考（流动性周期skill）\n{self._render_hint(payload)}\n\n"
            f"## 任务要求\n{output_spec}\n"
            "注意：本分析仅供研究参考，不构成投资建议。"
        )

    async def execute(self, input: AgentInput) -> AgentOutput:
        payload = self._parse_payload(input.payload)
        if not payload.analyses:
            return AgentOutput(
                task_id=input.task_id, agent_id=self.agent_id,
                conclusion="无上游分析结论可综合，跳过建议生成",
                confidence=Confidence.LOW, trace_id=input.task_id,
            )

        prompt = self.build_prompt(payload)
        response = await self._gateway.complete(
            "decision", self.system_prompt, prompt,
            agent_id=self.agent_id, trace_id=input.task_id, json_mode=True,
        )
        try:
            data = parse_llm_json(self.agent_id, response.content)
        except AgentExecutionError as parse_exc:
            repair_prompt = (
                f"{prompt}\n\n你上一次的输出不是合法JSON（{parse_exc}）。"
                "请严格只输出一个JSON对象，不要输出推理过程、注释或代码围栏，"
                "所有字符串内的双引号必须转义。"
            )
            response = await self._gateway.complete(
                "decision", self.system_prompt, repair_prompt,
                agent_id=self.agent_id, trace_id=input.task_id,
                json_mode=True, use_cache=False,
            )
            data = parse_llm_json(self.agent_id, response.content)

        # 幻觉防护：校验A17综合结论中的数字/代码是否grounded于上游分析+prompt
        conclusion_text = str(data.get("conclusion", ""))
        hg_report = HallucinationGuard.verify(
            conclusion_text, prompt, check_citations=False,
        )
        if not hg_report.passed:
            data["conclusion"] = conclusion_text + " " + hg_report.render_warning()
            if hg_report.confidence < 0.7:
                data["confidence"] = "low"

        return AgentOutput(
            task_id=input.task_id, agent_id=self.agent_id,
            conclusion=str(data.get("conclusion", "")),
            confidence=coerce_confidence(data.get("confidence", "medium")),
            data_refs=[a.get("agent_id", "?") for a in payload.analyses],
            trace_id=input.task_id,
            reasoning_steps=[
                TraceStep(step=1, step_type="llm_inference",
                          description=(
                              f"综合{len(payload.analyses)}维分析 "
                              f"model={response.model_used}"
                          )),
                TraceStep(
                    step=2, step_type="cross_validation",
                    description=(
                        f"幻觉防护: passed={hg_report.passed} "
                        f"confidence={hg_report.confidence:.2f}"
                    ),
                ),
            ],
            result={**data, "stance": data.get("stance", "中性"),
                    "model_used": response.model_used,
                    "disclaimer": (
                        "以上信息仅供研究参考，不构成投资建议。"
                        "投资有风险，入市需谨慎，盈亏自负。"
                    )},
        )
