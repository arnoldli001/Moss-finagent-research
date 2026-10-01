"""行业层Agent共享基类（A13科技/A14消费/A15周期/A16医药）。

与分析层一致的防幻觉策略：行业景气信号（关注指标的最近两期方向、估值旗标）
由本地代码从数据点计算，LLM只在行业分析框架内做定性研判。
子类用类属性声明行业差异，不重写execute。
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, TraceStep
from src.domain.agents.analysis.base import AnalysisAgentBase, AnalysisPayload
from src.domain.agents.analysis.platform_data_teaching import (
    render_platform_data_teaching,
)

logger = logging.getLogger(__name__)

# 每个关注指标注入LLM上下文的最近期数（全量历史数百点会冲淡焦点且浪费token）
_CONTEXT_PERIODS = 6

#: ★ 免责话术的判定词（**只在 `industry_scope` 存在时**才用来过滤，见
#: `IndustryAgentBase._strip_disclaimers`）。现场：真实端到端跑出来的原话
#: 「600036属银行、**不在本次科技行业数据覆盖内**…**无法给出可验证的持有结论**」，
#: prompt 里已经明写禁令**仍然输出**，所以需要一道确定性保证。
_DISCLAIMER_MARKERS: tuple[str, ...] = (
    "非本框架", "不属本框架", "不在本框架", "非本行业", "不在本行业",
    "不在本次", "不在本次行业数据覆盖", "不属本行业", "非本行业覆盖",
    "无法给出可验证的持有结论", "无法给出可验证结论", "无法给出可验证的持有",
    "无法对其半年持有给出可验证结论", "无法给出明确持有结论",
    "超出本框架", "不属于本框架",
)


def _split_sentences(text: str) -> list[str]:
    """按中英文句读切句（**保留分隔符**，拼回去与原文等长）。"""
    parts: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。！？；!?;\n":
            parts.append(buf)
            buf = ""
    if buf:
        parts.append(buf)
    return parts


def _has_disclaimer(sentence: str) -> bool:
    return any(m in sentence for m in _DISCLAIMER_MARKERS)


class IndustryAgentBase(AnalysisAgentBase):
    """行业分析Agent骨架：子类声明行业元数据，基类统一本地信号计算与prompt。"""

    industry_name: ClassVar[str] = ""
    """行业名（如 科技/消费/周期/医药）

    ⚠️ 这是**类级默认值**，A13-A16 用它写死自己的行业。
    兜底 Agent（A20）的行业名**按每次请求解析**（同一进程要服务任意行业），
    所以基类一律通过 `_industry_name_for(payload)` 取值，**不要**在基类里
    直接读 `self.industry_name` —— 那会把"每次请求一个行业"变成"进程启动时
    定死一个行业"，而且多请求并发时会互相串（本项目实测过同类：
    配置读进内存后不再重读，导致实例跑的是旧值）。
    """
    framework: ClassVar[str] = ""
    """行业分析框架一句话描述（注入prompt）"""
    watch_keywords: ClassVar[tuple[str, ...]] = ()
    """关注指标关键字（命中数据点indicator才纳入本地趋势信号）

    同上：这是**类级默认**，兜底 Agent 用 `_watch_keywords_for(payload)` 按需解析。
    """
    pe_high_watermark: ClassVar[float] = 40.0
    """PE高于该值给估值偏高旗标（行业子类可覆盖）"""
    capabilities_names: ClassVar[tuple[str, ...]] = ()

    dynamic_industry: ClassVar[bool] = False
    """是否**按每次请求**解析行业与关注指标（兜底行业 Agent 为 True）。

    A13-A16 各自服务一个固定行业，写死 `industry_name` / `watch_keywords` 是对的；
    兜底 Agent 要服务任意行业，写死任何一个都等于只覆盖它。
    本标记让**护栏能区分这两种设计**（`test_contract_consistency.py`
    的行业关注词判据据此走"动态解析"分支），而不是把"没写死"一律当成缺陷。
    """

    # ---------------- 按请求解析行业（兜底 Agent 的扩展点）----------------
    #
    # 为什么是"方法"而不是"属性"：`runtime.agents` 里的 Agent 是**单例**，
    # 一次进程要服务任意行业。把行业名写到 `self` 上会让并发请求互相覆盖，
    # 而且症状是"结论里写的是另一个行业"——比报错难查得多。

    def _industry_name_for(self, payload: AnalysisPayload) -> str:
        """本次请求的行业名。默认 = 类级 `industry_name`（A13-A16 行为不变）。"""
        return self.industry_name

    def _watch_keywords_for(self, payload: AnalysisPayload) -> tuple[str, ...]:
        """本次请求的关注指标关键字。默认 = 类级 `watch_keywords`。"""
        return self.watch_keywords

    def _framework_for(self, payload: AnalysisPayload) -> str:
        """本次请求的分析框架。默认 = 类级 `framework`。"""
        return self.framework

    # ---------------- 以下原样使用类级值 ----------------

    def _requirements(self, payload: AnalysisPayload) -> str:
        industry = self._industry_name_for(payload)
        focus = payload.focus or f"{industry}行业"
        # ★ 2026-09-29（§19.16.5 第 1 条）：标的**不在本框架内**时的措辞纪律。
        #   修前 A14（消费）拿到一只银行股会写「600036 属银行、**非本框架覆盖标的**…
        #   无法给出可验证的持有结论」—— 数据全在、兜底 Agent 也已接管，
        #   但这句话读起来就是"系统缺能力"。禁令写进 prompt，**判定不在这里做**
        #   （归属判定在编排层，见 `supervisor.industry_scope_for`）。
        scope = payload.hint.get("industry_scope") or {}
        scope_note = ""
        if scope and scope.get("self_named"):
            scope_note = (
                f"\n⚠️ 本次标的属「{scope.get('focus_industry')}」行业，**不属**你负责的"
                f"「{scope.get('agent_industry')}」框架：用户是主动点名了"
                f"「{scope.get('agent_industry')}」才把这一节挂上的。"
                f"所以这一节要回答的是**{scope.get('agent_industry')}行业本身**的问题，"
                f"**不是**对 {focus} 的行业结论（{focus} 的行业结论由"
                f"{scope.get('served_by')}负责）。\n"
                "**禁止**输出「非本框架覆盖标的」「不在本行业数据覆盖内」「无法给出"
                "可验证的持有结论」这类免责话术；只就本次数据给出可验证的观察"
                "（哪条指标、什么数值、什么方向），确实没有可用数据就**一句说清缺哪条**。\n"
            )
        # 归属判定在时，结论的首句对象要跟着改 —— 否则 prompt 一边说"别把标的当你的"
        # 一边又要求"首句点名{标的}"，模型只会挑一个执行（实测它挑了前者之后的
        # 免责话术，正是 §19.16.5 报障的那句）。
        first_line = (
            f'首句直接回答「{scope.get("agent_industry")}」行业本身的问题，'
            f"**不要**把 {focus} 当成本行业的标的"
            if scope else
            f'首句直接答问并点名"{focus}"'
        )
        # ★ 2026-09-29：平台自有数据八族的**使用口径**（行业侧三族是行业 Agent
        #   的主战场：行业拥挤度/板块资金流/行业轮动）。放在基类 = 一次覆盖
        #   A13-A16 + A20 五个 Agent（各写一份必然漂移）。
        #   依据：AGENTS.md「数据进得了上下文，但 prompt 从没提过它 ⇒ 模型不知道
        #   那是信号」——本轮实测这五族的 prompt 里原本一个字都没提。
        platform_block = render_platform_data_teaching(self.agent_id)
        return (
            f"按「{self._framework_for(payload)}」框架分析{focus}。{scope_note}\n输出JSON：\n"
            f'- "conclusion": {first_line}，200字内，引用本地信号/事件的'
            "具体事实，不复述无关数据。有申万估值截面须引具体PE/PB判高低"
            "（PE分位<20%关注、>80%高估，可判「估值洼地」）；有渗透率须判生命周期"
            "（预研/导入/成长/成熟/饱和）\n"
            '- "confidence": high|medium|low\n'
            '- "outlook": 向好|平稳|走弱|不明确\n'
            '- "cycle_position": 当前行业周期位置（30字内，须用上述框架术语，'
            "并解释本地信号对应哪个阶段）\n"
            '- "drivers": 核心驱动因素2-4条（须落到本行业，如库存/价格/政策/需求）\n'
            '- "risks": 行业主要风险1-3条'
            + platform_block
        )

    def _build_context(self, payload: AnalysisPayload) -> str:
        """注入关注指标、PE估值点、申万行业估值截面、渗透率数据的最近若干期。

        每行带 DataFreshnessEvaluator 评估的新鲜度图标/权重/标注；
        expired (conf<0.1) 完全过滤；stale/lagging 展示但标注。
        """
        from datetime import date

        from src.core.data_freshness import DataFreshnessEvaluator

        evaluator = DataFreshnessEvaluator()
        # 行业 hint：**按请求解析**（兜底 Agent 的行业名每次不同）
        industry_hint = self._industry_name_for(payload) or None
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
            f"[日期{today.isoformat()}；{self._industry_name_for(payload)}行业"
            + (f"；{expired_count}点过期过滤" if expired_count else "")
            + "]\n"
        )

        return header + "\n".join(lines) if lines else header + "（无行业关注指标数据点）"

    def _skip_reason(self, payload: AnalysisPayload) -> str | None:
        """关注指标零命中且无可信事件且无申万估值/渗透率时跳过LLM。

        信息层提取到事件（如产业链新闻）时允许基于事件做定性研判，避免"有问题无回答"。
        申万行业估值截面和渗透率数据本身也可支撑行业研判，不应跳过。
        """
        industry = self._industry_name_for(payload)
        keywords = self._watch_keywords_for(payload)
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
                f"采集数据中无{industry}行业关注指标"
                f"（关注：{'/'.join(keywords[:6])}…）且无相关可信事件，跳过LLM定性"
            )
        return None

    def _foreign_focus_reason(self, payload: AnalysisPayload) -> str | None:
        """标的**不归本 Agent 管**时的确定性结论（**不调 LLM**，归它管则 None）。

        ## 现场（`docs/PRD.md` §19.16.5 第 1 条）

        兜底 Agent（A20）已接管银行，但 A13（科技）/A14（消费）仍会输出
        「600036 属银行、**不在本次科技行业数据覆盖内**…无法给出可验证的持有结论」
        —— 用户读到的仍然是"系统缺能力"。

        ## 判据在编排层，这里只渲染

        `hint["industry_scope"]` 由 `supervisor.industry_scope_for` 给出
        （它拿 `INDUSTRY_KEYWORDS` 这个路由的**单一事实源**判）。领域层**不重新判定**
        —— 两份判据必然漂移，而漂移的症状是"路由认为不归它管、它自己认为归它管"，
        两边都"有理有据"且不报错。

        问句**点名了**本行业（`self_named`，如"消费板块里的银行股"）时不走这条：
        那是用户主动要的视角，只禁用免责话术（见 `_requirements`）。

        ## 兜底 Agent（`dynamic_industry`）永远不走这条

        它按请求解析行业（见 `dynamic_industry` 的说明），"归属"对它没有意义 ——
        它接管的就是**没有专属 Agent 的行业**。万一编排层给它带了 `industry_scope`
        （那是 bug），短路会输出「行业结论由…负责」，而那个"负责方"很可能就是它自己。
        """
        if self.dynamic_industry:
            return None
        scope = payload.hint.get("industry_scope") or {}
        if not scope or scope.get("self_named"):
            return None
        return (
            f"{payload.focus or '本次标的'} 属{scope.get('focus_industry')}行业，"
            f"不在本 Agent（{scope.get('agent_industry')}）覆盖范围内；"
            f"行业结论由{scope.get('served_by')}负责，"
            f"本节不作{scope.get('agent_industry')}框架研判。"
        )

    async def execute(self, input: AgentInput) -> AgentOutput:  # type: ignore[override]
        payload = self._parse_payload(input.payload)
        self._prepare(payload)
        foreign = self._foreign_focus_reason(payload)
        if foreign:
            # `skip_kind` 让"走了确定性分支"与"压根没跑"可区分（AGENTS.md：
            # 规则式路径也必须留下机器可读的痕迹）。
            return AgentOutput(
                task_id=input.task_id, agent_id=self.agent_id,
                conclusion=foreign, confidence=Confidence.LOW, trace_id=input.task_id,
                result={"industry_scope": payload.hint.get("industry_scope"),
                        "skipped": True, "skip_kind": "out_of_scope"},
                reasoning_steps=[TraceStep(
                    step=1, step_type="data_retrieval",
                    description="标的属其他行业，本 Agent 不作本行业框架研判")],
            )
        reason = self._skip_reason(payload)
        if reason:
            return AgentOutput(
                task_id=input.task_id, agent_id=self.agent_id,
                conclusion=reason, confidence=Confidence.LOW, trace_id=input.task_id,
                result={"industry_signal_calc": payload.hint.get("industry_signal"),
                        "skipped": True, "skip_kind": "no_local_input"},
                reasoning_steps=[TraceStep(
                    step=1, step_type="data_retrieval",
                    description="关注指标零命中，跳过LLM调用")],
            )
        output = await super().execute(input)
        return self._strip_disclaimers(output, payload)

    # ---------------- 免责话术的**确定性**清理（最后一道门） ----------------
    #
    # 为什么光靠 prompt 不够（真实端到端实测，2026-09-29）：
    #   A13/A14 的 prompt 里已经明写「**禁止**输出『非本框架覆盖标的』
    #   『不在本行业数据覆盖内』『无法给出可验证的持有结论』这类免责话术」，
    #   实测**照样输出**（小模型在"焦点是一只不属于它的票"时，会挑一句话认输）。
    #   所以：规划期换焦点（治本）+ prompt 禁令（引导）+ **本函数（保证）**。
    #
    # 边界（三条，防止把正常结论也吃掉）：
    #   ① 只在 `industry_scope` 存在时生效（= 本次标的确实不属于本行业）；
    #   ② **按句**处理，只丢命中话术的那一句 —— 同段里的行业分析要保住；
    #   ③ 丢了几句全部为空 → 用 `_foreign_focus_reason` 的确定性结论兜底；
    #   ④ 丢了什么**记进 result**（`disclaimers_dropped`），不许静默改写用户看到的话。

    def _strip_disclaimers(
        self, output: AgentOutput, payload: AnalysisPayload,
    ) -> AgentOutput:
        scope = payload.hint.get("industry_scope") or {}
        if not scope or self.dynamic_industry:
            return output
        text = str(output.conclusion or "")
        if not text:
            return output
        sentences = _split_sentences(text)
        kept = [s for s in sentences if not _has_disclaimer(s)]
        dropped = [s for s in sentences if _has_disclaimer(s)]
        if not dropped:
            return output
        conclusion = "".join(kept).strip() or (
            self._foreign_focus_reason(payload) or text)
        result = dict(output.result or {})
        result["disclaimers_dropped"] = [s.strip()[:120] for s in dropped]
        result["disclaimers_dropped_reason"] = (
            "命中「非本框架/不在本次覆盖内/无法给出可验证结论」这类免责话术 —— "
            f"本次标的属{scope.get('focus_industry')}行业，行业结论由"
            f"{scope.get('served_by')}负责，本 Agent 只答"
            f"{scope.get('agent_industry')}行业本身（见 PRD §19.16.5）")
        logger.info("%s 清理免责话术 %d 句：%s", self.agent_id, len(dropped),
                    result["disclaimers_dropped"])
        return output.model_copy(update={"conclusion": conclusion, "result": result})

    def _watched_points(self, payload: AnalysisPayload) -> list[dict[str, Any]]:
        keywords = self._watch_keywords_for(payload)
        if not keywords:
            return list(payload.data_points)
        return [
            p for p in payload.data_points
            if any(k in str(p.get("indicator", "")) for k in keywords)
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
            return (f"{ind}{when}{pe:g}高于{self._industry_name_for(payload)}行业警戒线"
                    f"{self.pe_high_watermark:g}，估值偏高")
        return f"{ind}{when}{pe:g}处于{self._industry_name_for(payload)}行业常规区间"

    def _prepare(self, payload: AnalysisPayload) -> None:
        watched = self._watched_points(payload)
        payload.hint["industry_signal"] = {
            "industry": self._industry_name_for(payload),
            "framework": self._framework_for(payload),
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
