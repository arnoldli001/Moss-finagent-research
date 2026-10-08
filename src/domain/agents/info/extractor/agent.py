"""A06 信息提取Agent：从非结构化文本提取结构化事件表（PRD A06，P1）。

防幻觉策略：事件必须带evidence_quote（原文证据）且item_id可溯源到具体条目；
方向/类型/置信度由本地代码规范化到枚举白名单，LLM输出的越界值不透传。
"""

from __future__ import annotations

import re
from typing import Any

from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, TraceStep
from src.domain.agents.analysis.base import parse_llm_json
from src.domain.agents.info.models import normalize_direction, normalize_event_type
from src.domain.skills.library import SkillLibrary
from src.infrastructure.llm import LLMGateway, TaskTier

_ITEM_TEXT_CHARS = 500
"""单条目正文注入prompt的最大字符数（控制上下文规模）"""

_MAX_EVENTS = 30
"""单次提取事件数上限（超出部分丢弃，防止幻觉刷屏）"""

#: 6 位 A 股代码（**只认这个形状**：多标的归属判据用它，名称/主题词不算代码）
_CODE_RE = re.compile(r"\d{6}")


class ExtractorAgent:
    """A06_extractor。"""

    task_tier: TaskTier = "medium"

    def __init__(
        self, gateway: LLMGateway, agent_id: str = "A06_extractor",
        skill_library: SkillLibrary | None = None,
        default_skill: str = "news-entity-extraction",
    ) -> None:
        self.agent_id = agent_id
        self._gateway = gateway
        self._skill_library = skill_library
        self._default_skill = default_skill

    def get_capabilities(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "capabilities": ["event_extraction", "structured_information"],
            "task_tier": self.task_tier,
        }

    def health_check(self) -> bool:
        return True

    def _load_default_skill(self) -> str:
        """加载固定技能正文（信息层任务单一，无需触发匹配）；失败返回空。"""
        if self._skill_library is None or not self._default_skill:
            return ""
        try:
            return self._skill_library.load_skill(self.agent_id, self._default_skill)
        except Exception:  # noqa: BLE001 技能加载失败不阻断主流程
            return ""

    async def execute(self, input: AgentInput) -> AgentOutput:
        raw_items = input.payload.get("info_items", [])
        if not raw_items:
            return AgentOutput(
                task_id=input.task_id, agent_id=self.agent_id,
                conclusion="无可信信息可提取，跳过事件提取",
                confidence=Confidence.LOW, trace_id=input.task_id,
                reasoning_steps=[TraceStep(step=1, step_type="data_retrieval",
                                           description="输入info_items为空，跳过LLM调用")],
                result={"events": [], "stats": {"total": 0, "positive": 0,
                                                "negative": 0, "neutral": 0}},
            )

        # ★ 2026-10-08：**多标的**时给每条带上"它来自哪只票"（`stock_code`）。
        #
        # 采集层现在按 `focus_stock_codes` **逐只**取新闻、并把代码标在条目上
        # （`supervisor._fetch_news_per_code`）。归属若在这里丢掉，下游 A07 只能
        # 把两只票的事件**混算成一个情绪分** —— 用户读到的"情绪偏暖"
        # 不知道是针对宁波银行还是中国神华。
        #
        # ⚠️ **单标的时一个字都不变**：`multi_items` 为假 ⇒ 条目行格式、
        #    prompt 正文、事件字段与修复前**逐字相同**（有回归护栏钉住）。
        code_by_item_id = {
            str(item.get("item_id", "")): str(item.get("stock_code") or "")
            for item in raw_items
        }
        item_codes = {c for c in code_by_item_id.values() if _CODE_RE.fullmatch(c)}
        multi_items = len(item_codes) >= 2
        lines = []
        for item in raw_items:
            text = str(item.get("text", ""))[:_ITEM_TEXT_CHARS]
            title = item.get("title", "")
            code = code_by_item_id.get(str(item.get("item_id", "")), "")
            prefix = f"({code}) " if (multi_items and code) else ""
            lines.append(
                f"- [{item.get('item_id', '?')}] {prefix}{title} | {text}"
            )
        #: 只多标的时追加（单标的时为空串 ⇒ prompt 逐字不变）
        multi_note = (
            "\n★ 本次是**多标的**问句：条目前缀 `(6位代码)` 是**该条目所属的标的**。"
            "请把事件的 `subject` 写成对应的标的（公司简称或该 6 位代码），"
            "**禁止**把两只标的的事件合并成一个主体，也禁止把 A 标的的事件写到 B 上。"
            if multi_items else ""
        )

        skill_text = self._load_default_skill()
        skill_block = (
            f"\n\n## 专业技能指引（遵循其Phase步骤与输出要求）\n{skill_text[:4000]}\n"
            if skill_text else ""
        )
        prompt = (
            "## 待提取信息条目\n" + "\n".join(lines) + multi_note +
            skill_block +
            "\n\n## 任务要求\n"
            "从上述条目中提取全部可确认的事件（每条信息可提取0-3个事件）。输出JSON对象：\n"
            '- "events": [{"item_id": "来源条目ID", "event_type": "earnings|merger|policy|'
            'product|management|litigation|financing|other", '
            '"subject": "事件主体（公司/行业/宏观）", '
            '"direction": "positive|negative|neutral", '
            '"magnitude": "影响程度简述（20字内，无量化则留空）", '
            '"event_date": "事件发生日期YYYY-MM-DD（未知留空）", '
            '"evidence_quote": "支撑该事件的原文片段（必须逐字来自输入，30字内）", '
            '"confidence": 0到1之间的数值}]\n'
            "硬性要求：evidence_quote禁止改写或编造；宁缺毋滥，无法确认的事件不要输出。"
            "本提取仅供研究参考，不构成投资建议。"
        )
        response = await self._gateway.complete(
            self.task_tier,
            "你是财经信息结构化专家，负责从新闻/公告/研报中提取可验证的事件。"
            "严格依据给定文本，禁止编造事件或数值。",
            prompt, agent_id=self.agent_id, trace_id=input.task_id, json_mode=True,
            # ★★ **禁语义复用，只留精确层**（`CHG-0182`）。
            #
            # 与 `intel_extract`（`CHG-0181`）**同一条规则**：本 Agent 的输出里
            # 有 `evidence_quote`，而它的契约就写在上面的 schema 里 ——
            # 「支撑该事件的原文片段（**必须逐字来自输入**，30字内）」
            # 且「**硬性要求：evidence_quote 禁止改写或编造**」。
            #
            # ⇒ 把 A 条资讯的抽取结果复用给 B 条，引文在 B 里根本不存在
            #   ⇒ 下游按"逐字可核对"消费时拿到的是**假证据**。
            #
            # 规则见 PRD §41.13.5：**只有当"输出不逐字引用输入"时，语义复用才安全。**
            # 精确层照常保留（同一条资讯重跑仍命中）。
            semantic_cache=False,
        )
        data = parse_llm_json(self.agent_id, response.content)

        valid_ids = {str(i.get("item_id", "")) for i in raw_items}
        events: list[dict[str, Any]] = []
        for raw in data.get("events", [])[:_MAX_EVENTS]:
            if not isinstance(raw, dict):
                continue
            item_id = str(raw.get("item_id", ""))
            if item_id not in valid_ids:
                continue  # item_id不可溯源的事件直接丢弃
            try:
                confidence = min(1.0, max(0.0, float(raw.get("confidence", 0.5))))
            except (TypeError, ValueError):
                confidence = 0.5
            event = {
                "item_id": item_id,
                "event_type": normalize_event_type(str(raw.get("event_type", ""))),
                "subject": str(raw.get("subject", ""))[:60],
                "direction": normalize_direction(str(raw.get("direction", ""))),
                "magnitude": str(raw.get("magnitude", ""))[:40],
                "event_date": str(raw.get("event_date", ""))[:10],
                "evidence_quote": str(raw.get("evidence_quote", ""))[:60],
                "confidence": round(confidence, 3),
            }
            if multi_items:
                # ★ 归属继承自**来源条目**（不是让模型自己写代码：模型写的代码
                #   无法核对，而条目上的代码是采集层按 `fetch_news(code)` 盖的章）。
                event["stock_code"] = code_by_item_id.get(item_id, "")
            events.append(event)

        stats = {
            "total": len(events),
            "positive": sum(1 for e in events if e["direction"] == "positive"),
            "negative": sum(1 for e in events if e["direction"] == "negative"),
            "neutral": sum(1 for e in events if e["direction"] == "neutral"),
        }
        ratio_ok = (stats["total"] / len(raw_items)) if raw_items else 0.0
        confidence_level = (Confidence.HIGH if ratio_ok >= 1.5
                            else Confidence.MEDIUM if stats["total"] > 0 else Confidence.LOW)
        return AgentOutput(
            task_id=input.task_id, agent_id=self.agent_id,
            conclusion=(
                f"从 {len(raw_items)} 条可信信息中提取 {stats['total']} 个事件"
                f"（利好 {stats['positive']} / 利空 {stats['negative']} "
                f"/ 中性 {stats['neutral']}）"
            ),
            confidence=confidence_level,
            data_refs=sorted(valid_ids - {""}),
            trace_id=input.task_id,
            reasoning_steps=[
                TraceStep(step=1, step_type="data_retrieval",
                          description=f"接收 {len(raw_items)} 条已核验信息"),
                TraceStep(step=2, step_type="llm_inference",
                          description=f"model={response.model_used} cache={response.cache_kind} "
                                      f"fallback={response.fallback_used}"),
                TraceStep(step=3, step_type="indicator_calculation",
                          description="事件规范化：direction/event_type白名单校验，"
                                      "无溯源item_id的事件丢弃"),
            ],
            result={"events": events, "stats": stats, "model_used": response.model_used},
        )
