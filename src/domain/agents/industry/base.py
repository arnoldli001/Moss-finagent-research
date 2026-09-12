"""行业层Agent共享基类（A13科技/A14消费/A15周期/A16医药）。

与分析层一致的防幻觉策略：行业景气信号（关注指标的最近两期方向、估值旗标）
由本地代码从数据点计算，LLM只在行业分析框架内做定性研判。
子类用类属性声明行业差异，不重写execute。
"""

from __future__ import annotations

from typing import Any, ClassVar

from src.domain.agents.analysis.base import AnalysisAgentBase, AnalysisPayload


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
        return (
            f"你正在按「{self.framework}」框架分析{self.industry_name}行业。\n"
            "请输出JSON对象，字段：\n"
            '- "conclusion": 行业景气研判（150字内，必须引用本地信号中的具体数值）\n'
            '- "confidence": "high"|"medium"|"low"\n'
            '- "outlook": "向好"|"平稳"|"走弱"|"不明确"\n'
            '- "cycle_position": 当前行业周期位置（30字内，须符合上述框架术语）\n'
            '- "drivers": 核心驱动因素2-4条\n'
            '- "risks": 行业主要风险1-3条'
        )

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
