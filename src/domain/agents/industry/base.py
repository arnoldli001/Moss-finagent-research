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
            f"按「{self.framework}」框架分析{focus}。\n输出JSON：\n"
            f'- "conclusion": 首句直接答问并点名"{focus}"，200字内，引用本地信号/事件的'
            "具体事实，不复述无关数据。有申万估值截面须引具体PE/PB判高低"
            "（PE分位<20%关注、>80%高估，可判「估值洼地」）；有渗透率须判生命周期"
            "（预研/导入/成长/成熟/饱和）\n"
            '- "confidence": high|medium|low\n'
            '- "outlook": 向好|平稳|走弱|不明确\n'
            '- "cycle_position": 当前行业周期位置（30字内，须用上述框架术语，'
            "并解释本地信号对应哪个阶段）\n"
            '- "drivers": 核心驱动因素2-4条（须落到本行业，如库存/价格/政策/需求）\n'
            '- "risks": 行业主要风险1-3条'
        )

    def _build_context(self, payload: AnalysisPayload) -> str:
        """注入关注指标、PE估值点、申万行业估值截面、渗透率数据的最近若干期。

        每行带 DataFreshnessEvaluator 评估的新鲜度图标/权重/标注；
        expired (conf<0.1) 完全过滤；stale/lagging 展示但标注。
        """
        from datetime import date

        from src.core.data_freshness import DataFreshnessEvaluator

        evaluator = DataFreshnessEvaluator()
        # self.industry_name 直接当行业 hint（行业 Agent 自己知道分析哪个行业）
        industry_hint = self.industry_name or None
        today = date.today()

        watched = self._watched_points(payload)
        pe_points = [
            p for p in payload.data_points
            if "PE" in str(p.get("indicator", "")).upper() and p not in watched
        ]
        lines: list[str] = []
        expired_count = 0

        def _format_with_fresh(p: dict[str, Any]) -> str | None:
            """单行格式化，返回 None 表示 expired 被过滤。"""
            nonlocal expired_count
            indicator = str(p.get("indicator", "?"))
            fe = evaluator.evaluate(
                indicator, p.get("period_date"), industry_hint, today,
            )
            if not fe.should_display:
                expired_count += 1
                return None
            raw_conf = p.get("confidence")
            display_conf = (
                min(float(raw_conf), fe.confidence)
                if raw_conf is not None else fe.confidence
            )
            parts = [
                f"- {fe.status_icon}{indicator}",
                f"{p.get('period_date', '?')}={p.get('value', '缺失')}",
                f"c{display_conf:.2f}",
                str(p.get('source_name', '?')),
            ]
            if fe.weight_multiplier < 1.0:
                parts.append(f"w×{fe.weight_multiplier}")
            if fe.note and fe.status != "expired":
                parts.append(fe.note)
            return " ".join(parts)

        # 常规行业指标（关注指标+PE时序点）
        for points in (watched, pe_points):
            by_indicator: dict[str, list[dict[str, Any]]] = {}
            for p in points:
                by_indicator.setdefault(str(p.get("indicator", "?")), []).append(p)
            for _indicator, series in by_indicator.items():
                series = sorted(series, key=lambda p: str(p.get("period_date", "")),
                                reverse=True)[:_CONTEXT_PERIODS]
                for p in sorted(series, key=lambda p: str(p.get("period_date", ""))):
                    row = _format_with_fresh(p)
                    if row:
                        lines.append(row)
        # 申万行业估值截面（PE/PB/股息率，按行业名分组展示）
        sw_points = [
            p for p in payload.data_points
            if str(p.get("indicator", "")).startswith("ind:sw_")
        ]
        if sw_points:
            lines.append("\n【申万行业估值截面】")
            for p in sorted(sw_points, key=lambda x: float(x.get("value", 0))):
                fe = evaluator.evaluate(
                    str(p.get("indicator", "?")),
                    p.get("period_date"), industry_hint, today,
                )
                if not fe.should_display:
                    expired_count += 1
                    continue
                extra = p.get("extra") or {}
                ind_name = extra.get("industry_name", "?")
                ind_level = extra.get("industry_level", "?")
                parent = extra.get("parent_industry", "")
                metric = extra.get("metric", "?")
                val = p.get("value", "?")
                parent_str = f"（所属：{parent}）" if parent else ""
                icon = fe.status_icon if fe.status != "fresh" else ""
                weight_note = f" 权重×{fe.weight_multiplier}" if fe.weight_multiplier < 1.0 else ""
                lines.append(
                    f"- 申万{ind_level} {icon}| {ind_name}{parent_str} | "
                    f"{metric}={val} | 成份{extra.get('constituent_count', '?')}个"
                    f"{weight_note}"
                )
        # 渗透率数据
        pen_points = [
            p for p in payload.data_points
            if str(p.get("indicator", "")).startswith("ind:penetration:")
        ]
        if pen_points:
            lines.append("\n【渗透率数据（生命周期判断）】")
            for p in pen_points:
                fe = evaluator.evaluate(
                    str(p.get("indicator", "?")),
                    p.get("period_date"), industry_hint, today,
                )
                if not fe.should_display:
                    expired_count += 1
                    continue
                extra = p.get("extra") or {}
                track = extra.get("track", "?")
                val = p.get("value", "?")
                lifecycle = extra.get("lifecycle", "?")
                source = extra.get("source", "?")
                note = extra.get("note", "")
                icon = fe.status_icon if fe.status != "fresh" else ""
                line = (
                    f"- {track}：渗透率{val}%（{lifecycle}）{icon}| 来源：{source}"
                )
                if note:
                    line += f" | 备注：{note}"
                lines.append(line)

        # 头部时效概览（紧凑）
        header = (
            f"[日期{today.isoformat()}；{self.industry_name}行业"
            + (f"；{expired_count}点过期过滤" if expired_count else "")
            + "]\n"
        )

        return header + "\n".join(lines) if lines else header + "（无行业关注指标数据点）"

    def _skip_reason(self, payload: AnalysisPayload) -> str | None:
        """关注指标零命中且无可信事件且无申万估值/渗透率时跳过LLM。

        信息层提取到事件（如产业链新闻）时允许基于事件做定性研判，避免"有问题无回答"。
        申万行业估值截面和渗透率数据本身也可支撑行业研判，不应跳过。
        """
        signal = payload.hint.get("industry_signal") or {}
        has_sw_valuation = any(
            str(p.get("indicator", "")).startswith("ind:sw_")
            for p in payload.data_points
        )
        has_penetration = any(
            str(p.get("indicator", "")).startswith("ind:penetration:")
            for p in payload.data_points
        )
        if (
            signal.get("watched_indicator_count", 0) == 0
            and not payload.events
            and not payload.verified_texts
            and not has_sw_valuation
            and not has_penetration
        ):
            return (
                f"采集数据中无{self.industry_name}行业关注指标"
                f"（关注：{'/'.join(self.watch_keywords[:6])}…）且无相关可信事件，跳过LLM定性"
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
    def _pair_direction(pv: float, cv: float) -> tuple[str, str]:
        """返回(细标签含幅度, 粗方向)；粗方向用于多指标投票汇总。"""
        if pv == 0:
            return f"基数为零，最新值{cv:g}", "持平"
        delta = (cv - pv) / abs(pv) * 100
        if abs(delta) < 1.0:
            return "基本持平", "持平"
        if delta > 0:
            return f"环比上行{delta:.1f}%", "上行"
        return f"环比下行{abs(delta):.1f}%", "下行"

    @staticmethod
    def _trend_signal(points: list[dict[str, Any]]) -> dict[str, Any]:
        """按指标分组，各取最近两期比较方向；多指标时投票汇总。

        不跨指标比较：多个指标同属最新一期时，旧实现会把A指标与B指标当成
        前后两期，产生无意义的方向信号。
        """
        groups: dict[str, list[dict[str, Any]]] = {}
        for p in points:
            if isinstance(p.get("value"), (int, float)):
                groups.setdefault(str(p.get("indicator", "?")), []).append(p)

        details: list[str] = []
        votes: dict[str, int] = {"上行": 0, "下行": 0, "持平": 0}
        per_indicator: dict[str, str] = {}
        for indicator, series in groups.items():
            ordered = sorted(series, key=lambda p: str(p.get("period_date", "")))
            if len(ordered) < 2:
                continue
            prev, curr = ordered[-2], ordered[-1]
            pv, cv = float(prev["value"]), float(curr["value"])
            label, coarse = IndustryAgentBase._pair_direction(pv, cv)
            votes[coarse] += 1
            per_indicator[indicator] = label
            details.append(
                f"{indicator}：{prev.get('period_date', '?')}期{pv:g} → "
                f"{curr.get('period_date', '?')}期{cv:g}（{label}）"
            )

        if not details:
            return {"trend": "数据不足", "detail": "关注指标少于两期，无法判断方向",
                    "per_indicator": {}}

        decided = {k: v for k, v in votes.items() if v > 0}
        if len(decided) == 1:
            coarse = next(iter(decided))
            trend = {"上行": "关注指标普遍环比上行",
                     "下行": "关注指标普遍环比下行",
                     "持平": "关注指标环比基本持平"}[coarse]
        else:
            trend = (
                f"信号分化（上行{votes['上行']}/持平{votes['持平']}/下行{votes['下行']}）"
            )
        return {
            "trend": trend,
            "detail": "；".join(details),
            "per_indicator": per_indicator,
        }

    def _valuation_flag(self, payload: AnalysisPayload) -> str:
        pe_points = [
            p for p in payload.data_points
            if "PE" in str(p.get("indicator", "")).upper()
            and isinstance(p.get("value"), (int, float))
        ]
        if not pe_points:
            return "未提供PE数据，估值维度不评价"
        # PE是时序点：必须取最新期，旧实现取序列首个点（24个月前）导致估值旗标失真
        latest = max(pe_points, key=lambda p: str(p.get("period_date", "")))
        ind, pe = str(latest.get("indicator", "")), float(latest["value"])
        period = latest.get("period_date", "")
        when = f"{period}期" if period else ""
        if pe > self.pe_high_watermark:
            return (f"{ind}{when}{pe:g}高于{self.industry_name}行业警戒线"
                    f"{self.pe_high_watermark:g}，估值偏高")
        return f"{ind}{when}{pe:g}处于{self.industry_name}行业常规区间"

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
