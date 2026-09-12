"""行业层Agent共享基类（A13科技/A14消费/A15周期/A16医药）。

与分析层一致的防幻觉策略：行业景气信号（关注指标的最近两期方向、估值旗标）
由本地代码从数据点计算，LLM只在行业分析框架内做定性研判。
子类用类属性声明行业差异，不重写execute。
"""

from __future__ import annotations

from typing import Any, ClassVar

from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, TraceStep
from src.domain.agents.analysis.base import AnalysisAgentBase, AnalysisPayload

# 每个关注指标注入LLM上下文的最近期数（全量历史数百点会冲淡焦点且浪费token）
_CONTEXT_PERIODS = 6


class IndustryAgentBase(AnalysisAgentBase):
    """行业分析Agent骨架：子类声明行业元数据，基类统一本地信号计算与prompt。"""

    industry_name: ClassVar[str] = ""
    """行业名（如 科技/消费/周期/医药）"""
    framework: ClassVar[str] = ""
    """行业分析框架一句话描述（注入prompt）"""
    watch_keywords: ClassVar[tuple[str, ...]] = ()
    """关注指标关键字（命中数据点indicator才纳入本地趋势信号）"""
    pe_high_watermark: ClassVar[float] = 40.0
    """PE高于该值给估值偏高旗标（行业子类可覆盖）"""
    capabilities_names: ClassVar[tuple[str, ...]] = ()

    def _requirements(self, payload: AnalysisPayload) -> str:
        focus = payload.focus or f"{self.industry_name}行业"
        return (
            f"你正在按「{self.framework}」框架分析{focus}所在的{self.industry_name}行业。\n"
            "请输出JSON对象，字段：\n"
            f'- "conclusion": {focus}行业景气研判（150字内，必须点名"{focus}"，'
            "必须引用本地信号中的具体数值，禁止复述与本行业无关的宏观数据）\n"
            '- "confidence": "high"|"medium"|"low"\n'
            '- "outlook": "向好"|"平稳"|"走弱"|"不明确"\n'
            '- "cycle_position": 当前行业周期位置（30字内，须用上述框架术语，'
            "并解释本地信号对应哪个阶段）\n"
            '- "drivers": 核心驱动因素2-4条（须落到本行业，如库存/价格/政策/需求）\n'
            '- "risks": 行业主要风险1-3条'
        )

    def _build_context(self, payload: AnalysisPayload) -> str:
        """只注入关注指标（含PE估值点）的最近若干期，避免无关指标冲淡行业焦点。"""
        watched = self._watched_points(payload)
        pe_points = [
            p for p in payload.data_points
            if "PE" in str(p.get("indicator", "")).upper() and p not in watched
        ]
        lines: list[str] = []
        for points in (watched, pe_points):
            by_indicator: dict[str, list[dict[str, Any]]] = {}
            for p in points:
                by_indicator.setdefault(str(p.get("indicator", "?")), []).append(p)
            for indicator, series in by_indicator.items():
                series = sorted(series, key=lambda p: str(p.get("period_date", "")),
                                reverse=True)[:_CONTEXT_PERIODS]
                for p in sorted(series, key=lambda p: str(p.get("period_date", ""))):
                    lines.append(
                        f"- {indicator} | 期间 {p.get('period_date', '?')} "
                        f"| 值 {p.get('value', '缺失')} | 来源 {p.get('source_name', '?')}"
                    )
        return "\n".join(lines) if lines else "（无行业关注指标数据点）"

    def _skip_reason(self, payload: AnalysisPayload) -> str | None:
        """关注指标零命中时跳过LLM：通用CPI/PPI不足以支撑专业行业研判，防硬聊。"""
        signal = payload.hint.get("industry_signal") or {}
        if signal.get("watched_indicator_count", 0) == 0:
            return (
                f"采集数据中无{self.industry_name}行业关注指标"
                f"（关注：{'/'.join(self.watch_keywords[:6])}…），跳过LLM定性"
            )
        return None

    async def execute(self, input: AgentInput) -> AgentOutput:  # type: ignore[override]
        payload = self._parse_payload(input.payload)
        self._prepare(payload)
        reason = self._skip_reason(payload)
        if reason:
            return AgentOutput(
                task_id=input.task_id, agent_id=self.agent_id,
                conclusion=reason, confidence=Confidence.LOW, trace_id=input.task_id,
                result={"industry_signal_calc": payload.hint.get("industry_signal"),
                        "skipped": True},
                reasoning_steps=[TraceStep(
                    step=1, step_type="data_retrieval",
                    description="关注指标零命中，跳过LLM调用")],
            )
        return await super().execute(input)

    def _watched_points(self, payload: AnalysisPayload) -> list[dict[str, Any]]:
        if not self.watch_keywords:
            return list(payload.data_points)
        return [
            p for p in payload.data_points
            if any(k in str(p.get("indicator", "")) for k in self.watch_keywords)
        ]

    @staticmethod
    def _trend_signal(points: list[dict[str, Any]]) -> dict[str, Any]:
        """取同指标最近两期（按period_date）比较方向。"""
        valued = [p for p in points if isinstance(p.get("value"), (int, float))]
        if len(valued) < 2:
            return {"trend": "数据不足", "detail": "关注指标少于两期，无法判断方向"}
        ordered = sorted(valued, key=lambda p: str(p.get("period_date", "")))
        prev, curr = ordered[-2], ordered[-1]
        pv, cv = float(prev["value"]), float(curr["value"])
        if pv == 0:
            direction = "基数为零无法比较"
            delta = cv
        else:
            delta = (cv - pv) / abs(pv) * 100
            if abs(delta) < 1.0:
                direction = "基本持平"
            elif delta > 0:
                direction = f"环比上行{delta:.1f}%"
            else:
                direction = f"环比下行{abs(delta):.1f}%"
        return {
            "trend": direction,
            "detail": (
                f"{curr.get('indicator', '?')}：{prev.get('period_date', '?')}期"
                f"{pv:g} → {curr.get('period_date', '?')}期{cv:g}"
            ),
        }

    def _valuation_flag(self, payload: AnalysisPayload) -> str:
        for p in payload.data_points:
            ind = str(p.get("indicator", ""))
            if "PE" in ind.upper() and isinstance(p.get("value"), (int, float)):
                pe = float(p["value"])
                if pe > self.pe_high_watermark:
                    return f"{ind}{pe:g}高于{self.industry_name}行业警戒线" \
                           f"{self.pe_high_watermark:g}，估值偏高"
                return f"{ind}{pe:g}处于{self.industry_name}行业常规区间"
        return "未提供PE数据，估值维度不评价"

    def _prepare(self, payload: AnalysisPayload) -> None:
        watched = self._watched_points(payload)
        payload.hint["industry_signal"] = {
            "industry": self.industry_name,
            "framework": self.framework,
            "watched_indicator_count": len(watched),
            **self._trend_signal(watched),
            "valuation": self._valuation_flag(payload),
        }

    def _enrich_result(self, payload: AnalysisPayload, data: dict[str, Any]) -> dict[str, Any]:
        data["industry_signal_calc"] = payload.hint.get("industry_signal")
        return data

    def get_capabilities(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "capabilities": list(self.capabilities_names) or ["industry_analysis"],
            "task_tier": self.task_tier,
        }

    def health_check(self) -> bool:
        return True
