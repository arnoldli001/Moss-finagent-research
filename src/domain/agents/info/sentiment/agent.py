"""A07 舆情分析Agent：市场情绪量化 + 情绪周期定位（PRD A07，P2）。

防幻觉策略：情绪分/分布/热点主体全部由本地规则计算（logic.py），
LLM（reasoning层）只负责解读指标并做周期定位，禁止自造数值。
"""

from __future__ import annotations

import re
from typing import Any

from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, TraceStep, coerce_confidence
from src.domain.agents.analysis.base import parse_llm_json
from src.domain.agents.info.sentiment.logic import (
    compute_sentiment_metrics,
    compute_sentiment_metrics_by_group,
    group_events,
)
from src.domain.skills.library import SkillLibrary
from src.infrastructure.llm import LLMGateway, TaskTier

_PHASES = ("乐观", "分歧", "谨慎", "恐慌", "不明确")

#: 6 位 A 股代码（**只认这个形状**：判断"是不是多标的"用它；
#: 事件里的 `subject` 也可能是公司简称，那不算逐票分组的依据）
_CODE_RE = re.compile(r"\d{6}")


class SentimentAgent:
    """A07_sentiment。

    ★ 2026-09-28 第九轮：task_tier 从 `reasoning` 降为 `light`
      A07 输出"情绪三档 + 周期五档"，本质是**离散分类任务**，
      1.5B 本地模型（受约束解码）完全够用，且 json_schema 已锁定输出结构。
      旧实现走 reasoning 层（云端 deepseek 或本地 8B），实测 ~30s/批；
      新实现走 light 层本地 1.5B，实测 ~3s/批。
      节省 ~27s（−90%）。
    """

    task_tier: TaskTier = "light"

    def __init__(
        self, gateway: LLMGateway, agent_id: str = "A07_sentiment",
        skill_library: SkillLibrary | None = None,
        default_skill: str = "sentiment-heat-tracking",
    ) -> None:
        self.agent_id = agent_id
        self._gateway = gateway
        self._skill_library = skill_library
        self._default_skill = default_skill

    def get_capabilities(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "capabilities": ["sentiment_quantification", "sentiment_cycle_positioning"],
            "task_tier": self.task_tier,
        }

    def health_check(self) -> bool:
        return True

    def _load_default_skill(self) -> str:
        """加载固定技能正文；失败返回空（不阻断主流程）。"""
        if self._skill_library is None or not self._default_skill:
            return ""
        try:
            return self._skill_library.load_skill(self.agent_id, self._default_skill)
        except Exception:  # noqa: BLE001 技能加载失败不阻断主流程
            return ""

    async def execute(self, input: AgentInput) -> AgentOutput:
        events = input.payload.get("events", [])
        if not events:
            return AgentOutput(
                task_id=input.task_id, agent_id=self.agent_id,
                conclusion="无事件可分析，跳过舆情量化",
                confidence=Confidence.LOW, trace_id=input.task_id,
                reasoning_steps=[TraceStep(step=1, step_type="data_retrieval",
                                           description="输入events为空，跳过LLM调用")],
                result={"sentiment_metrics": compute_sentiment_metrics([])},
            )

        metrics = compute_sentiment_metrics(events)
        # ★★ 2026-10-08：**多标的逐只出分**（报障的"舆情版"）。
        #
        # 现场：一条问两只票的问句里，A07 原先把**全部**事件混算成一个情绪分
        # （`logic.py` 的 `compute_sentiment_metrics`）⇒ 两只票的利好/利空互相
        # 抵消，用户读到的"情绪偏暖"**不知道是针对哪只**。
        #
        # 判据：事件里带 `stock_code` 的**不同代码 ≥2 个**（代码由采集层逐只
        # `fetch_news(code)` 盖章、A06 继承到事件上 —— 不是让模型自己写）。
        # `multi` 为假 ⇒ 下面每个分支都是原文，单标的输出**逐字不变**。
        code_groups = {
            key: group for key, group in group_events(events).items()
            if _CODE_RE.fullmatch(key)
        }
        multi = len(code_groups) >= 2
        metrics_by_group = (
            compute_sentiment_metrics_by_group(events) if multi else {})
        event_lines = [
            f"- [{e.get('direction', 'neutral')}] {e.get('subject', '?')} "
            f"({e.get('event_type', 'other')}, 置信 {e.get('confidence', 0.5)}): "
            f"{e.get('evidence_quote', '')}"
            for e in events[:30]
        ]
        skill_text = self._load_default_skill()
        skill_block = (
            f"\n\n## 专业技能指引（遵循其Phase步骤与输出要求）\n{skill_text[:4000]}\n"
            if skill_text else ""
        )
        #: 逐标的分组指标块（**只在多标的时**出现 ⇒ 单标的 prompt 逐字不变）
        group_block = ""
        if multi:
            group_block = (
                "\n## 逐标的情绪指标（**每只标的一份**，禁止合并成一个分）\n"
                + "\n".join(
                    f"- [{key}] 加权情绪分 {m['weighted_sentiment']}"
                    f"（[-1,1]，越大越乐观）；事件 {m['event_count']} 条；"
                    f"分布 {m['distribution']}；热点主体 {m['top_subjects']}"
                    for key, m in metrics_by_group.items())
                + "\n★ 本次问句点名了**多只标的**（"
                + "、".join(metrics_by_group)
                + "）：`conclusion` 必须**逐只**说明各自舆情，"
                  "`per_subject` 必须**每只一条**（`subject` 用上面方括号里的代码）；"
                  "禁止只写一只，禁止用总体情绪分代替逐只结论。\n"
            )
        prompt = (
            f"## 本地计算的情绪指标（权威数值，禁止修改）\n"
            f"加权情绪分 {metrics['weighted_sentiment']}（[-1,1]，越大越乐观）\n"
            f"事件分布 {metrics['distribution']}\n"
            f"热点主体 {metrics['top_subjects']}\n"
            + group_block +
            "\n## 事件明细\n" + "\n".join(event_lines) +
            skill_block +
            "\n\n## 任务要求\n基于上述指标与事件做情绪解读与周期定位。输出JSON对象：\n"
            '- "conclusion": 舆情综合研判（120字内，必须引用情绪分与分布数值）\n'
            '- "confidence": "high"|"medium"|"low"\n'
            '- "sentiment_phase": "乐观"|"分歧"|"谨慎"|"恐慌"|"不明确"\n'
            '- "narrative": 情绪主线与演变逻辑（80字内）\n'
            + ('- "per_subject": [{"subject": "标的名或6位代码", '
               '"sentiment_phase": "乐观"|"分歧"|"谨慎"|"恐慌"|"不明确", '
               '"conclusion": "该标的的舆情研判（80字内，须引用它的情绪分）"}]'
               " —— 上面每一只标的各一条\n" if multi else "")
            + "注意：sentiment_phase必须与情绪分方向自洽（如情绪分-0.6不得判乐观）。"
            "本分析仅供研究参考，不构成投资建议。"
        )
        #: 受约束解码的 schema：多标的时**追加**逐只字段（单标的时逐字不变）。
        #: ⚠️ 必须进 schema —— 本地 1.5B 走 GBNF 约束解码，
        #:    schema 里没有的键模型**根本输出不出来**（"让它自己加一个字段"是做不到的）。
        json_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "conclusion": {"type": "string"},
                "confidence": {"type": "string",
                               "enum": ["high", "medium", "low"]},
                "sentiment_phase": {"type": "string",
                                    "enum": ["乐观", "分歧", "谨慎", "恐慌", "不明确"]},
                "narrative": {"type": "string"},
            },
            "required": ["conclusion", "sentiment_phase"],
        }
        if multi:
            json_schema["properties"]["per_subject"] = {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string"},
                        "sentiment_phase": {
                            "type": "string",
                            "enum": ["乐观", "分歧", "谨慎", "恐慌", "不明确"]},
                        "conclusion": {"type": "string"},
                    },
                    "required": ["subject", "sentiment_phase"],
                },
            }
        response = await self._gateway.complete(
            self.task_tier,
            "你是市场舆情分析师，负责把结构化事件表转译为情绪判读。"
            "只引用给定的指标数值，禁止编造数据。",
            prompt, agent_id=self.agent_id, trace_id=input.task_id, json_mode=True,
            # ★ 2026-09-28 第九轮：1.5B 需要受约束解码（受 json_schema）才能稳定输出
            json_schema=json_schema,
        )
        data = parse_llm_json(self.agent_id, response.content)

        phase = str(data.get("sentiment_phase", "不明确"))
        if phase not in _PHASES:
            phase = "不明确"
        conclusion = str(data.get("conclusion", ""))
        # 一致性防线：强空情绪不允许输出乐观周期（LLM自相矛盾时降级为不明确）
        if phase == "乐观" and metrics["weighted_sentiment"] <= -0.3:
            phase, conclusion = "不明确", f"{conclusion}（情绪分与周期定位矛盾，已置为不明确）"

        # ★ 多标的：逐只周期定位（**本地指标是权威值**，模型的 phase 只做定位；
        #   越界/漏写的那些按"不明确"处理，不许静默丢掉一只票）。
        phases_by_group: dict[str, str] = {}
        if multi:
            raw_per_subject = data.get("per_subject")
            written: dict[str, str] = {}
            if isinstance(raw_per_subject, list):
                for row in raw_per_subject:
                    if not isinstance(row, dict):
                        continue
                    key = str(row.get("subject") or "").strip()
                    if key:
                        written[key] = str(row.get("sentiment_phase") or "")
            for key, group_metrics in metrics_by_group.items():
                candidate = written.get(key, "")
                if candidate not in _PHASES:
                    candidate = "不明确"
                # 与总体分同一条一致性防线：逐只也不许"情绪分为负却判乐观"
                if candidate == "乐观" and group_metrics["weighted_sentiment"] <= -0.3:
                    candidate = "不明确"
                phases_by_group[key] = candidate

        result: dict[str, Any] = {
            "sentiment_metrics": metrics,
            "sentiment_phase": phase,
            "narrative": str(data.get("narrative", ""))[:120],
            "model_used": response.model_used,
        }
        if multi:
            # 加法字段：总体分照旧（`sentiment_metrics` 不变），逐只明细另给一份
            result["sentiment_metrics_by_group"] = metrics_by_group
            result["sentiment_phase_by_group"] = phases_by_group
            result["multi_subject"] = True
        return AgentOutput(
            task_id=input.task_id, agent_id=self.agent_id,
            conclusion=conclusion or f"加权情绪分 {metrics['weighted_sentiment']}",
            confidence=coerce_confidence(data.get("confidence", "medium")),
            data_refs=sorted({e.get("item_id", "") for e in events} - {""}),
            trace_id=input.task_id,
            reasoning_steps=[
                TraceStep(step=1, step_type="indicator_calculation",
                          description=f"本地情绪量化：加权分 {metrics['weighted_sentiment']}，"
                                      f"分布 {metrics['distribution']}"
                                      + (f"；逐标的 {list(metrics_by_group)}" if multi else "")),
                TraceStep(step=2, step_type="llm_inference",
                          description=f"model={response.model_used} cache={response.cache_kind} "
                                      f"fallback={response.fallback_used}"),
            ],
            result=result,
        )
