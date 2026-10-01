"""分析层Agent共享基类。

统一职责：payload解析 → 数据点上下文化 → LLM(reasoning层,JSON模式) →
输出解析与置信度融合。子类只需提供system_prompt与_requirements钩子。
"""

from __future__ import annotations

import json
import logging
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

logger = logging.getLogger(__name__)


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


# ============================================================
# 横截面（同一天 N 个成员）的**维度还原**
# ============================================================
#
# ## 为什么需要（2026-09-29 实测现场）
#
# 一批 `:all` 指标是**横截面**而非时间序列：
#
#     ind:sw_third_pe_ttm:all          335 条，全部 period_date=2026-09-29，
#                                      每条属于一个不同的申万三级行业
#     ind:sw_third_dividend_yield:all  335 条，同上
#     idx_val:snapshot:all               5 条，6 只宽基
#
# 而 `_build_context()` 的渲染是 `- {指标} {期}={值} c{置信} {来源}` ——
# **不带 `extra`**。于是模型收到的是 60 行完全一样的
# `ind:sw_third_pe_ttm:all 2026-09-29=80.53`，**不知道哪个数是哪个行业**。
# 后果不是报错，是模型随手挑一个当"该行业 PE"，或者干脆编一个。
#
# 还有第二个更隐蔽的问题：`context_max_periods = 60` 把它当"最近 60 期"截断，
# 而这里根本没有"期" —— 同一个日期上 335 个成员被**任意**留下 60 个。
# 于是**两次运行给出不同的子集**（SQL 返回顺序不稳定），
# 同一个问题两次答案不一样，且无法复现。
#
# ## 判据（行为判据，不猜字段名）
#
# 一个分组被判为横截面，当且仅当：
#   ① 同一个 `period_date` 上至少有 `_CROSS_SECTION_MIN_MEMBERS` 条；
#   ② 这些条目的 `extra` 里**存在**一个键，其取值至少有 2 个不同的非空值
#      —— "能被区分"才算横截面。
#
# 为什么下限是 3 而不是 2：`fed:policy_range` 的事故正是**同一天 2~3 条**
# （上限/下限/有效利率）而**没有任何字段能区分**它们 —— 那不是横截面，
# 是"一个 indicator 多值语义"的缺陷，正确修法是**拆成单值序列**
# （已拆为 `fed:target_upper` / `fed:target_lower` / `fed:effr`）。
# 拿"同一天多条"当横截面的判据，会把那个缺陷**掩盖**掉，而不是暴露它。
#
# ## 渲染策略（只加信息，不减信息）
#
#   ① 排序改成**完全确定**：`(期 desc, 值 desc, 维度标签)` —— 截断后子集可复现，
#      且先看到极值（分析师真正要的那几个）；
#   ② 每行前缀维度标签 `[银行业]`，模型能对上号；
#   ③ 头部明写"同日 N 个成员，展示 M 条（**已截断**）"—— 截断必须显式。
_CROSS_SECTION_MIN_MEMBERS = 3

#: `extra` 里可能代表"成员"的键，**按优先级**取第一个满足判据②的。
#: 刻意不做模糊匹配：取错维度比不取更糟（会给出一个看起来像标签的错标签）。
_CROSS_SECTION_DIM_KEYS: tuple[str, ...] = (
    "industry_name", "industry", "index_name", "index", "board_name",
    "sector_name", "sector", "stock_name", "name", "code", "ts_code",
)


def _numeric(value: Any) -> float | None:
    """能当数就用数；不能就 None（**不把字符串硬转成 0**）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _dim_label(point: dict[str, Any], key: str) -> str:
    extra = point.get("extra")
    if not isinstance(extra, dict):
        return ""
    raw = extra.get(key)
    return "" if raw is None else str(raw).strip()


def _cross_section_dim(rows: list[dict[str, Any]]) -> str | None:
    """这一组是横截面吗？是则返回能区分成员的 `extra` 键，否则 `None`。"""
    if len(rows) < _CROSS_SECTION_MIN_MEMBERS:
        return None
    newest = str(rows[0].get("period_date", ""))
    members = [p for p in rows if str(p.get("period_date", "")) == newest]
    if len(members) < _CROSS_SECTION_MIN_MEMBERS:
        return None
    for key in _CROSS_SECTION_DIM_KEYS:
        values = {_dim_label(p, key) for p in members}
        values.discard("")
        if len(values) >= 2:
            return key
    return None


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
        cross_notes: list[str] = []       # 横截面：明写"同日 N 成员 / 展示 M"

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

            # ★ 横截面：排序必须完全确定（期 desc → 值 desc → 维度标签），
            #   否则 `context_max_periods` 截断留下的子集**每次运行都不同**。
            dim = _cross_section_dim(series)
            if dim:
                newest_date = str(series[0].get("period_date", ""))
                member_count = sum(
                    1 for p in series if str(p.get("period_date", "")) == newest_date)
                series = sorted(
                    series,
                    key=lambda p: (
                        str(p.get("period_date", "")),
                        _numeric(p.get("value")) if _numeric(p.get("value")) is not None
                        else float("-inf"),
                        _dim_label(p, dim),
                    ),
                    reverse=True,
                )
                cross_notes.append(
                    f"{indicator}：{newest_date} 同日 {member_count} 个成员"
                    f"（按 {dim} 区分）"
                )

            # 截断到 context_max_periods
            if self.context_max_periods is not None:
                series = series[:self.context_max_periods]
            if dim:
                shown = sum(
                    1 for p in series
                    if str(p.get("period_date", "")) == str(series[0].get("period_date", ""))
                )
                cross_notes[-1] += (
                    f"，按值降序展示 {shown} 条"
                    + ("（**已截断**）" if shown < member_count else "（全量）")
                )
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
                value = p.get("value")
                if value is None:
                    # ★「没量到」的**显式记号**：`p.get("value", "缺失")` 只在
                    #   **键不存在**时给默认值；键存在而值为 `None` 时会渲染成
                    #   字面量 `None` —— 模型会把它读成 0 或直接忽略，
                    #   于是"没量到"伪装成了"量到 0"（本项目头号失败模式）。
                    value = "缺失"
                # 紧凑记号：图标+指标 期=值 c置信 来源；省去重复中文标签（模型按序解读）
                parts = [
                    f"- {fe.status_icon}{indicator}",
                ]
                # ★ 横截面：把"这个数是谁的"补回去，否则模型只看到一堆无主数字
                label = _dim_label(p, dim) if dim else ""
                if label:
                    parts.append(f"[{label}]")
                parts += [
                    f"{p.get('period_date', '?')}={value}",
                    f"c{display_conf:.2f}",
                    str(p.get("source_name", "?")),
                ]
                if fe.weight_multiplier < 1.0:
                    parts.append(f"w×{fe.weight_multiplier}")
                if fe.note and fe.status != "expired":
                    parts.append(fe.note)
                lines.append(" ".join(parts))

        # 时效概览（紧凑：去掉模型无需知晓的衰减公式说明行）
        total = len(payload.data_points)
        header = (
            f"[日期{today.isoformat()}；展示{len(lines)}/{total}，"
            f"过期过滤{expired_count}]\n"
        )
        if cross_notes:
            header += "▦横截面（同一天多个成员，**不是时间序列**）：\n" + "".join(
                f"  · {n}\n" for n in cross_notes)
        if stale_indicators:
            header += f"⚠️新鲜度下降：{', '.join(stale_indicators)}\n"

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

    def _audit_rule_only(self, *, trace_id: str, model_used: str,
                         reason: str) -> None:
        """把"走了规则路径、没调 LLM"这件事**落进 LLM 审计**。

        判据与取舍见调用点的大段注释。要点：
          · tokens_in / tokens_out = **0**（真实值就是 0，不是"未量到"）
          · provider = `"rule"` —— 与真实提供商区分得开，不污染成本归因
          · 原因走 `error` 通道（复用既有 schema，运维页解析不用改）

        ⚠️ 必须**永不抛异常**：规则路径是"LLM 不可用时的兜底"，
        审计写不进去不能反过来把兜底也弄坏（与 `local_gate` 同一纪律）。
        也必须容忍**没有 `audit_log` 的假网关**（测试替身遍地都是，
        要求它们都实现审计接口是把测试替身当生产依赖）。
        """
        try:
            audit = getattr(self._gateway, "audit_log", None)
            if audit is None:
                return
            from src.infrastructure.llm.models import LLMResponse

            audit.record(
                trace_id=trace_id, agent_id=self.agent_id,
                task_tier=self.task_tier,
                response=LLMResponse(
                    content="", model_used=model_used, provider="rule",
                    tokens_in=0, tokens_out=0, latency_ms=0,
                ),
                cached=False,
                error=f"rule_only: {reason}"[:200],
            )
        except Exception:  # noqa: BLE001 审计失败绝不能影响兜底路径
            logger.debug("%s 的 rule-only 审计写入失败（忽略）",
                         self.agent_id, exc_info=True)


    async def execute(self, input: AgentInput) -> AgentOutput:
        payload = self._parse_payload(input.payload)
        self._prepare(payload)
        # ★ 2026-09-27 第八轮：A12 等可纯规则化的 Agent 走"零 LLM"路径
        #   审计实证：A12 在"无风险信号"分支仍调云端 reasoning，浪费 18,500 tokens
        if hasattr(self, "_should_skip_llm") and self._should_skip_llm(payload):
            data = self._build_rule_only_result(payload)
            model_used = str(data.get("model_used", "rule-only"))
            # ★★ 2026-09-28 第二十三轮：**规则式路径也要落审计**（消除审计黑洞）
            #
            # 【为什么必须补】原先这里直接 return，一个字节都不写审计 ——
            #   而 `LLMAuditLog.record()` **只在网关里**被调用。后果：
            #
            #     运维页/审计文件里，这一轮 A08 看起来"没有发生任何事"
            #     （0 条记录、0 token），与"这个 Agent 压根没跑"**无法区分**。
            #
            #   实测现场：用户问宏观问题时，审计里只有 2 条记录
            #   （planning + A17），A08-A12 全部无痕 ——
            #   排查时第一反应是"分析层没执行"，而真相是"走了规则路径"。
            #   这正是 AGENTS.md 那条硬约束：
            #   **「"没量到"与"量到 0"必须分开显示」**。
            #
            # 【成本口径必须为 0】用 tokens_in/out = 0 + provider="rule" 落账：
            #   `call_cost_cny()` 对未定价模型返回 0，`tokens_by_tenant()`
            #   累加 0 → **不会污染账单**，但人会看到"这里发生了一次规则判定"。
            #
            # 【不新增 schema 字段】复用 `error` 通道携带原因（与网关的
            #   `vram_reroute:` / `circuit_open:` 同一手法），
            #   这样运维页的既有解析不用改。
            self._audit_rule_only(
                trace_id=input.task_id, model_used=model_used,
                reason=str(data.get("rule_only_reason") or "rule_gate"),
            )
            return AgentOutput(
                task_id=input.task_id, agent_id=self.agent_id,
                conclusion=str(data.get("conclusion", "")),
                confidence=coerce_confidence(data.get("confidence", "medium")),
                data_refs=self._collect_refs(payload),
                trace_id=input.task_id,
                reasoning_steps=[TraceStep(
                    step=1, step_type="indicator_calculation",
                    description=(
                        f"纯规则判定（跳过 LLM）；severe_flags={data.get('severe_flag_count', 0)} "
                        f"level={data.get('compliance_level_calc', '无')}"
                    ),
                )],
                result={**self._enrich_result(payload, data),
                        "model_used": model_used,
                        "tokens_in": 0, "tokens_out": 0,
                        "_rule_only": True},
            )
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
        if payload.user_query:
            query_block = f"### 提问（conclusion首句须直接作答）\n{payload.user_query}"
            if payload.focus:
                query_block += f"\n焦点：{payload.focus}"
        else:
            query_block = f"### 分析焦点\n{payload.focus or '综合分析'}"
        skill_hits = self._match_skills(payload)
        # 规则合并：原2/3/4条（不编造/缺口/方向性）语义重叠，压成一条；
        # 流动性规则条件插入并动态编号。
        rules = [
            "1. conclusion首句直接答问，禁模板点评",
            "2. 仅依据上方带period_date的数据与事件；无则声明数据缺口并降置信，"
            "禁编造或引用训练记忆中的数值",
        ]
        if has_liq:
            rules.append(
                "3. 大盘/买卖/仓位：按「总量阶段→三市分项→双创宽度与PE分位→结论」，"
                "缩量(<2.5万亿)回踩不追高；data_gaps如实声明")
        rules.append(f"{'4' if has_liq else '3'}. 遵循技能Phase步骤与任务要求的输出格式")
        rule_block = "### 规则\n" + "；\n".join(rules)
        # 时效红线：与规则2同源，压成一句（保留今天锚定+period_date grounding）。
        grounding = (
            f"### 时效红线\n今天{today_str}。只用上方数据，引用须带period_date；"
            "上方没有即「数据缺口」，不得使用任何训练记忆的年份/数值。")
        prompt = (
            f"{query_block}\n"
            f"{rule_block}\n"
            f"{grounding}\n\n"
            f"## 输入数据\n{self._build_context(payload)}\n\n"
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
                reasoning_effort="low",  # 纯JSON重排，收敛思维链省token/延迟
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
