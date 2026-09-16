"""分析层Agent共享基类。

统一职责：payload解析 → 数据点上下文化 → LLM(reasoning层,JSON模式) →
输出解析与置信度融合。子类只需提供system_prompt与_requirements钩子。
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from pydantic import BaseModel, Field, ValidationError

from src.core.base_agent import BaseAgent
from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, TraceStep, coerce_confidence
from src.domain.skills.library import SkillLibrary
from src.infrastructure.llm import LLMGateway, TaskTier
from src.infrastructure.llm.hallucination_guard import HallucinationGuard


class AnalysisPayload(BaseModel):
    """分析层统一输入契约（由Supervisor从ResearchState组装）。"""

    data_points: list[dict[str, Any]] = Field(default_factory=list)
    focus: str = ""
    """分析焦点：行业名 / 股票代码 / 宏观主题"""
    user_query: str = ""
    """用户原始提问：conclusion必须直接回答此问题，禁止只给泛化模板点评"""
    hint: dict[str, Any] = Field(default_factory=dict)
    """本地计算参考值（如估值对比、风险比率），LLM只解读不计算"""
    events: list[dict[str, Any]] = Field(default_factory=list)
    """信息层A06提取的事件表（诉讼/监管/管理层等），合规与风险Agent用作信号"""
    verified_texts: list[str] = Field(default_factory=list)
    """A05已核验可信但A06未结构化为事件的原文摘要（兜底事实素材，带溯源来源行）"""


def parse_llm_json(agent_id: str, content: str) -> dict[str, Any]:
    """解析LLM输出JSON（容忍```json围栏、<think>块与尾随文本）。

    依次尝试：围栏提取 → 整体loads → raw_decode取首个JSON对象。
    """
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        raise AgentExecutionError(f"{agent_id} LLM输出JSON非对象: {type(data)}")
    except json.JSONDecodeError:
        pass
    try:
        data, _ = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as exc:
        raise AgentExecutionError(f"{agent_id} LLM输出非合法JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentExecutionError(f"{agent_id} LLM输出JSON非对象: {type(data)}")
    return data


class AnalysisAgentBase(BaseAgent):
    """分析层四Agent（A08/A09/A10/A11）的公共骨架。"""

    task_tier: ClassVar[TaskTier] = "reasoning"
    system_prompt: ClassVar[str] = ""
    context_max_periods: ClassVar[int | None] = 60
    """每个indicator注入prompt的最近期数：全量历史（个股数千条日线/宏观数百月值）
    会淹没用户问题并浪费token；本地hint计算仍用全量数据，不受此限。None表示不截断。"""

    def __init__(
        self, agent_id: str, gateway: LLMGateway,
        skill_library: SkillLibrary | None = None,
    ) -> None:
        super().__init__(agent_id)
        self._gateway = gateway
        self._skill_library = skill_library

    def _requirements(self, payload: AnalysisPayload) -> str:
        """子类覆盖：任务要求与输出JSON schema说明。"""
        raise NotImplementedError

    def _parse_payload(self, payload: dict[str, Any]) -> AnalysisPayload:
        try:
            return AnalysisPayload.model_validate(payload)
        except ValidationError as exc:
            raise AgentExecutionError(f"{self.agent_id}输入payload不合法: {exc}") from exc

    def _build_context(self, payload: AnalysisPayload) -> str:
        """数据点 → LLM可读上下文（带溯源 + **指数衰减置信度** + 五级分级）。

        基于 src/core/data_freshness.py 的 DataFreshnessEvaluator：
        - 指标前缀 → 发布周期（日频/周频/月频/季频/年频/长周期）
        - 行业周期倍数（短 1.0 / 中 1.5 / 长 2.0 / 超长 2.5）
        - confidence = exp(-0.5 × 距今天数 / (周期 × 倍数))
        - expired (conf<0.1) 完全过滤；stale/lagging 展示但带权重和标注

        不再用硬编码的 12/24 月或 6/12 月阈值——日频 PE 断档 7 天就 lagging，
        月频 CPI 3 个月才 lagging，长周期船舶数据 24 个月仍 normal。
        """
        from datetime import date

        from src.core.data_freshness import DataFreshnessEvaluator

        evaluator = DataFreshnessEvaluator()
        today = date.today()
        groups: dict[str, list[dict[str, Any]]] = {}
        for p in payload.data_points:
            groups.setdefault(str(p.get("indicator", "?")), []).append(p)

        # 尝试从 payload.focus / payload.hint 提取行业关键词（影响周期倍数）
        industry_hint = None
        if hasattr(payload, "focus") and payload.focus:
            industry_hint = payload.focus
        elif payload.user_query:
            industry_hint = payload.user_query

        lines: list[str] = []
        expired_count = 0
        stale_indicators: list[str] = []  # lagging + stale 的指标列表（概览行用）

        for indicator, series in groups.items():
            series = sorted(series, key=lambda p: str(p.get("period_date", "")),
                            reverse=True)
            # 先判断这个指标整体最新点的状态（概览用）
            newest_eval = evaluator.evaluate(
                indicator, series[0].get("period_date") if series else None,
                industry_hint, today,
            )
            if newest_eval.status in ("lagging", "stale", "expired"):
                stale_indicators.append(
                    f"{indicator}({newest_eval.status_icon} {newest_eval.status})"
                )
            # 截断到 context_max_periods
            if self.context_max_periods is not None:
                series = series[:self.context_max_periods]
            for p in series:
                # 每个数据点独立算新鲜度（同一指标不同 period_date 新鲜度不同）
                fe = evaluator.evaluate(
                    indicator, p.get("period_date"), industry_hint, today,
                )
                if not fe.should_display:
                    expired_count += 1
                    continue  # 完全过滤（conf < 0.1）
                # 原始 confidence 取 DataPoint 自带的，如果新鲜度更低则取 min
                raw_conf = p.get("confidence")
                display_conf = (
                    min(float(raw_conf), fe.confidence)
                    if raw_conf is not None else fe.confidence
                )
                value = p.get("value", "缺失")
                parts = [
                    f"- {indicator} {fe.status_icon}",
                    f"期间 {p.get('period_date', '?')}",
                    f"值 {value}",
                    f"来源 {p.get('source_name', '?')}",
                    f"置信度 {display_conf:.2f}",
                ]
                if fe.weight_multiplier < 1.0:
                    parts.append(f"权重×{fe.weight_multiplier}")
                if fe.note and fe.status != "expired":
                    parts.append(f"[{fe.note}]")
                lines.append(" | ".join(parts))

        # 时效概览
        total = len(payload.data_points)
        freshness_header = [
            f"### 当前日期锚定：{today.isoformat()}",
            f"### 输入数据：{len(lines)} 条展示（{total} 总 / {expired_count} 过期已过滤）",
            "### 新鲜度机制：指数衰减 exp(-0.5×d/(周期×行业倍数))",
        ]
        if stale_indicators:
            freshness_header.append(
                f"### ⚠️ 以下指标新鲜度下降：{', '.join(stale_indicators)}"
            )
        header = "\n".join(freshness_header) + "\n"

        return header + "\n".join(lines) if lines else header + "（无可用数据点）"

    def _parse_llm_json(self, content: str) -> dict[str, Any]:
        return parse_llm_json(self.agent_id, content)

    def _enrich_result(self, payload: AnalysisPayload, data: dict[str, Any]) -> dict[str, Any]:
        """子类可覆盖：向result追加本地计算字段。"""
        return data

    def _prepare(self, payload: AnalysisPayload) -> None:
        """子类可覆盖：prompt构建前的本地计算（可回填payload.hint）。"""

    def _match_skills(self, payload: AnalysisPayload) -> list[dict[str, str]]:
        """确定性技能匹配：user_query/focus 命中frontmatter触发短语则取技能正文。"""
        if self._skill_library is None:
            return []
        text = f"{payload.user_query} {payload.focus}".strip()
        try:
            return self._skill_library.match_skills(self.agent_id, text)
        except Exception:  # noqa: BLE001 技能加载失败不阻断主分析
            return []

    def _skill_block(self, skill_hits: list[dict[str, str]]) -> str:
        if not skill_hits:
            return "无"
        return "\n\n".join(
            f"### 技能：{s['name']}\n{s['content'][:4000]}" for s in skill_hits
        )


    async def execute(self, input: AgentInput) -> AgentOutput:
        payload = self._parse_payload(input.payload)
        self._prepare(payload)
        if not payload.data_points and not payload.events and not payload.verified_texts:
            question = payload.user_query or payload.focus or "相关主题"
            return AgentOutput(
                task_id=input.task_id, agent_id=self.agent_id,
                conclusion=(
                    f"未获取到回答「{question[:60]}」所需的数据或可信信息，"
                    "无法形成有依据的分析结论（拒绝无数据臆测）"
                ),
                confidence=Confidence.LOW, trace_id=input.task_id,
                reasoning_steps=[TraceStep(step=1, step_type="data_retrieval",
                                           description="输入数据点与事件均为空，跳过LLM调用")],
            )

        hint = json.dumps(payload.hint, ensure_ascii=False) if payload.hint else "无"
        has_liq = "market_liquidity" in (payload.hint or {})
        from datetime import date as _date
        today_str = _date.today().isoformat()
        event_lines = "\n".join(
            f"- [{e.get('direction', 'neutral')}] {e.get('event_type', 'other')} "
            f"{e.get('subject', '')}: {e.get('evidence_quote', '')}"
            for e in payload.events[:15]
        ) or "无"
        verified_lines = "\n".join(f"- {t}" for t in payload.verified_texts[:5]) or "无"
        query_block = (
            f"### 用户提问（conclusion必须直接回答此问题，不得答非所问）\n{payload.user_query}\n"
            if payload.user_query else
            f"### 分析焦点\n{payload.focus or '综合分析'}\n"
        )
        rule1 = (
            f"1. conclusion首句必须直接回答「{payload.user_query[:80]}」，"
            "禁止绕开问题给模板点评；\n"
            if payload.user_query else
            "1. 结论必须直接回应用户提问；\n"
        )
        liq_rule = (
            "5. 大盘/买卖/仓位类问题：按「总量阶段→三市分项占比→双创宽度"
            "与PE分位→结论」顺序，缩量阶段(<2.5万亿)低吸不追高；"
            "data_gaps缺口须如实声明；\n"
            if has_liq else ""
        )
        skill_hits = self._match_skills(payload)
        prompt = (
            f"{query_block}\n"
            "### 回答规则\n"
            f"{rule1}"
            "2. 只用给定数据与事件，禁止编造数值；\n"
            "3. 无关数据不作依据，说明数据缺口并降置信度；\n"
            "4. 缺量化数据源时说明缺口，给方向性判断不杜撰数字；\n"
            f"{liq_rule}"
            "6. 若含技能指引须遵循其Phase步骤与输出格式。\n\n"
            f"### 🔴 时效红线（最高优先级）\n"
            f"当前日期是 {today_str}。你只能使用上方「输入数据」中展示的数据点。\n"
            "严禁引用你训练数据中任何年份、任何时间点的外部信息或历史数值，"
            "哪怕你记得准确也要当作不存在。所有数据引用必须能在上方「输入数据」"
            "段落找到对应 period_date 和数值。如果上方没有某数据，就说「未获取到」"
            "或「数据缺口」，然后只依据有的数据给结论。引用数据时必须带 period_date。\n\n"
            f"### 分析焦点\n{payload.focus or '综合分析'}\n\n"
            f"## 输入数据（含溯源）\n{self._build_context(payload)}\n\n"
            f"## 信息层事件\n{event_lines}\n\n"
            f"## 已核验原文\n{verified_lines}\n\n"
            f"## 本地计算参考\n{hint}\n\n"
            f"## 专业技能指引\n{self._skill_block(skill_hits)}\n\n"
            f"## 任务要求\n{self._requirements(payload)}\n\n"
            "仅供研究参考，不构成投资建议。"
        )
        response = await self._gateway.complete(
            self.task_tier, self.system_prompt, prompt,
            agent_id=self.agent_id, trace_id=input.task_id, json_mode=True,
        )
        try:
            data = self._parse_llm_json(response.content)
        except AgentExecutionError as parse_exc:
            # 小模型/flash偶发未转义引号导致非法JSON：带错误反馈修复重试一次（不走缓存）
            repair_prompt = (
                f"{prompt}\n\n你上一次的输出不是合法JSON（{parse_exc}）。"
                "请严格只输出一个JSON对象，不要输出推理过程、注释或代码围栏，"
                "所有字符串内的双引号必须转义。"
            )
            response = await self._gateway.complete(
                self.task_tier, self.system_prompt, repair_prompt,
                agent_id=self.agent_id, trace_id=input.task_id,
                json_mode=True, use_cache=False,
            )
            data = self._parse_llm_json(response.content)

        # 幻觉防护：校验输出数字/股票代码是否grounded于输入数据
        conclusion_text = str(data.get("conclusion", ""))
        hg_report = HallucinationGuard.verify(
            conclusion_text, prompt, check_citations=False,
        )
        if not hg_report.passed:
            data["conclusion"] = conclusion_text + " " + hg_report.render_warning()
            if hg_report.confidence < 0.7:
                data["confidence"] = "low"

        result = self._enrich_result(payload, data)

        return AgentOutput(
            task_id=input.task_id, agent_id=self.agent_id,
            conclusion=str(data.get("conclusion", "")),
            confidence=coerce_confidence(data.get("confidence", "medium")),
            data_refs=self._collect_refs(payload),
            trace_id=input.task_id,
            reasoning_steps=[
                TraceStep(step=1, step_type="data_retrieval",
                          description=f"上下文化 {len(payload.data_points)} 个数据点"),
                TraceStep(step=2, step_type="llm_inference",
                          description=(
                              f"model={response.model_used} cache={response.cache_kind} "
                              f"fallback={response.fallback_used}"
                          )),
                *([TraceStep(step=3, step_type="skill_injection",
                             description=f"命中技能：{[s['name'] for s in skill_hits]}")]
                  if skill_hits else []),
                TraceStep(
                    step=4, step_type="cross_validation",
                    description=(
                        f"幻觉防护: passed={hg_report.passed} "
                        f"confidence={hg_report.confidence:.2f}"
                    ),
                ),
            ],
            result={**result, "model_used": response.model_used,
                    "tokens_in": response.tokens_in, "tokens_out": response.tokens_out},
        )

    @staticmethod
    def _collect_refs(payload: AnalysisPayload) -> list[str]:
        refs = []
        for p in payload.data_points:
            refs.append(
                p.get("data_id") or p.get("raw_content_hash")
                or f"{p.get('indicator', '?')}:{p.get('period_date', '?')}"
            )
        return refs
