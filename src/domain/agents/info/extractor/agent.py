"""A06 信息提取Agent：从非结构化文本提取结构化事件表（PRD A06，P1）。

防幻觉策略：事件必须带evidence_quote（原文证据）且item_id可溯源到具体条目；
方向/类型/置信度由本地代码规范化到枚举白名单，LLM输出的越界值不透传。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, TraceStep
from src.domain.agents.analysis.base import output_hit_cap, parse_llm_json
from src.domain.agents.info.models import normalize_direction, normalize_event_type
from src.domain.skills.library import SkillLibrary
from src.infrastructure.llm import LLMGateway, TaskTier

logger = logging.getLogger(__name__)

_ITEM_TEXT_CHARS = 500
"""单条目正文注入prompt的最大字符数（控制上下文规模）"""

_MAX_EVENTS = 30
"""单次提取事件数上限（超出部分丢弃，防止幻觉刷屏）"""

#: ★ `CHG-0250`：**prompt 里要几条**。与 `_MAX_EVENTS` 是两件事 ——
#: 后者是"收到多少条就留多少条"的**事后**截断，这里是**事前**的"要多少"。
#:
#: ## 为什么必须事前限制（2026-10-16 实测）
#:
#: `medium` 层输出预算 **2048 token**（含思维链）。一条事件 JSON 有 7 个字段、
#: 其中 `evidence_quote` 要求逐字引用（30 字）⇒ 实测每条约 **90~120 token**，
#: 加上外层包装 ⇒ **2048 大约只装得下 16 条**。而 prompt 原文写的是
#: 「从上述条目中提取**全部**可确认的事件（每条信息可提取 0-3 个事件）」——
#: 20 多条资讯 ⇒ 最多可要求 60 条 ⇒ **必然写不完** ⇒ 输出被截断 ⇒
#: `parse_llm_json` 报 `Expecting value: line 224 column 18 (char 5278)` ⇒
#: **一条事件都读不出来**（0 条 ≪ 16 条）。
#:
#: ⇒ 事前要 16 条、比"事后一条都拿不到"严格更好；被截断时还有一次
#: "**要得更少**"的重试（见 `_RETRY_MAX_EVENTS`）——★ 原样重发必然同样被截断。
_PROMPT_MAX_EVENTS = 16

#: 截断后重试时的条数（更小的要求）。取 6：留出足够余量让 JSON 一定收得了尾。
_RETRY_MAX_EVENTS = 6

#: A06 的系统提示（两处调用共用**同一个字面量**，避免漂移）。
_SYSTEM_PROMPT = (
    "你是财经信息结构化专家，负责从新闻/公告/研报中提取可验证的事件。"
    "严格依据给定文本，禁止编造事件或数值。"
)

#: 6 位 A 股代码（**只认这个形状**：多标的归属判据用它，名称/主题词不算代码）
_CODE_RE = re.compile(r"\d{6}")


def _extract_diagnostics(response: Any, task_tier: Any) -> dict[str, Any]:
    """A06 的用量诊断（两次调用共用**同一份实现**）。

    `max_tokens` 的来源优先取**响应自带的实际值**（网关四重夹过之后的值），
    取不到才退回"本层默认预算" —— 两者都取不到就是 `0`（= 未知），
    此时 `output_hit_cap()` **不会**断言截断（宁可不说，也不猜）。
    """
    cap = int(getattr(response, "max_tokens", 0) or 0)
    if not cap:
        from src.infrastructure.llm.gateway import tier_output_budget

        cap = tier_output_budget(str(task_tier))
    return {
        "model": getattr(response, "model_used", "") or "",
        "provider": getattr(response, "provider", "") or "",
        "tokens_out": int(getattr(response, "tokens_out", 0) or 0),
        "reasoning_tokens": int(getattr(response, "reasoning_tokens", 0) or 0),
        "max_tokens": cap,
        "task_tier": str(task_tier),
    }


def _smaller_ask(prompt: str) -> str:
    """截断后的**更小的要求** —— 不是原样重发（原样重发必然同样被截断）。"""
    return (
        prompt
        + f"\n\n★★★ 上一次输出**被长度上限截断**了（JSON 没写完 ⇒ 一条都读不出来）。"
        f"这次**最多只输出 {_RETRY_MAX_EVENTS} 条最重要的事件**；"
        "`magnitude` 一律留空、`evidence_quote` 压到 20 字内、"
        "**不要**输出任何解释文字，只输出一个紧凑的 JSON 对象。"
    )


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
            "从上述条目中提取可确认的事件（每条信息可提取0-3个事件）。"
            f"★ **最多输出 {_PROMPT_MAX_EVENTS} 条事件**：超过时只保留最重要的 "
            f"{_PROMPT_MAX_EVENTS} 条 —— 本层输出上限很小，写多了会被**截断**，"
            "截断后**一条都读不出来**（宁可少而准）。输出JSON对象：\n"
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
            _SYSTEM_PROMPT,
            prompt, agent_id=self.agent_id, trace_id=input.task_id, json_mode=True,
            # ★★ 2026-10-15：**关掉思维链**（报障驱动，`CHG-0249` / PRD §五十三）。
            #
            # 现场（`data/pilot/audit/llm_audit.jsonl`，trace `task_20261015_f03aa9c5`）：
            #   21:17:16 | A06_extractor | deepseek-flash | in=2042 **out=2048
            #            | reasoning_tokens=2048** | error=None
            # ⇒ `tokens_out` **全部**是思维链 ⇒ **正文 0 字符** ⇒
            #   `parse_llm_json` 抛「LLM输出非合法JSON: Expecting value:
            #   line 1 column 1 (char 0)」—— 用户看到的就是这一句。
            #   `char 0`（空串）与"模型不会输出 JSON"长得一样，**会把排查方向带偏**。
            #
            # 为什么 `none` 是正确取值（不是"降级妥协"）：
            #   · 本 Agent 的活是「从给定文本里挑子集 + 按 schema 填字段」，
            #     `evidence_quote` 还**必须逐字来自输入** ⇒ 不需要多步推演；
            #   · 同一条处方在 `planner.py` 上早就用过：那边 2026-09-28 加了
            #     `reasoning_effort="none"` 之后，`supervisor_planner` 的
            #     "100% CoT ⇒ 正文空"在全量审计里**再没出现过**
            #     （全量 4.4 万+ 行里最后一条就是 2026-09-28 那次）。
            #
            # ⚠️ 为什么**不**顺手把 `max_tokens` 抬上去：`medium` 层的输出预算
            #   2048 对"正文"是够的（本 Agent 历史上成功的一次 out=1745），
            #   被吃光的只是思维链；而抬预算会**撞单跳墙钟**——
            #   `_OUTPUT_TOKENS_PER_SEC=210` 是 **deepseek** 的实测值，
            #   而本层链首 dashscope 在这场实测是 2048 tok / 21.16 s ≈ **97 tok/s**
            #   ⇒ 2048 已经贴着 20 s 预算，抬到 3072 只会让链首更必然被砍。
            #   （`test_attempt_budget_output_consistency.py` 的不变量只覆盖
            #   "付费做链首"的层，`medium` 链首是免费云端 ⇒ 它没被那条护栏覆盖，
            #   这一条已登记在 PRD §五十三 的已知缺口里。）
            reasoning_effort="none",
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
        diagnostics = _extract_diagnostics(response, self.task_tier)
        try:
            data = parse_llm_json(self.agent_id, response.content,
                                  diagnostics=diagnostics)
        except AgentExecutionError:
            # ★★ `CHG-0250`：**只在"输出顶到上限"时**改用**更小的要求**重试一次。
            #
            # 为什么必须换要求：截断是"要得太多装不下"的**确定性**后果 ——
            # 原样重发（哪怕带上"你上次不是合法JSON"的修复提示）会**同样被截断**，
            # 白花一跳的钱与时间。这与分析层基类那条"JSON 修复重试"是两件事：
            # 那条治的是"转义/围栏"这类**写法**问题，这条治的是**容量**问题。
            #
            # 为什么只重试一次、且只在这一种情形：其它失败（真的写坏、模型不会写）
            # 换个小要求也没用；而无限重试会把一个"降级"放大成"整条链路超时"。
            if not output_hit_cap(diagnostics):
                raise
            logger.warning(
                "A06 输出被上限截断（out=%s 顶到上限 %s）⇒ 改用更小的要求"
                "（最多 %s 条）重试一次",
                diagnostics.get("tokens_out"), diagnostics.get("max_tokens"),
                _RETRY_MAX_EVENTS)
            response = await self._gateway.complete(
                self.task_tier,
                _SYSTEM_PROMPT,
                _smaller_ask(prompt),
                agent_id=self.agent_id, trace_id=input.task_id, json_mode=True,
                reasoning_effort="none",
                semantic_cache=False,
                # ⚠️ **不传 `max_tokens`**：层级预算与单跳墙钟是配套的
                #    （`tests/unit/test_empty_body_and_cot_budget.py` 有护栏钉住），
                #    抬预算只会让这个免费云链首更容易被墙钟砍掉。
                use_cache=False,
            )
            diagnostics = _extract_diagnostics(response, self.task_tier)
            data = parse_llm_json(self.agent_id, response.content,
                                  diagnostics=diagnostics)

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
