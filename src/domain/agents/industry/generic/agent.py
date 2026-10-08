"""A20 兜底行业 Agent：**A13-A16 覆盖不到的行业由它接管**。

## 报障现场（用户 2026-09-29 原话）

> 「600036（招商银行属银行，非本行业消费）在数据缺口下无法给出明确持有结论：
>   **缺少银行 agent 吗？那就生成一个负责其他产业的 agent。做个兜底行业的 agent，
>   支持根据输入内容，动态注册对应行业 agent**」

修前的真实表现：一条问"高股息招商银行"的复合问，落到了 **A14（消费）** 手里，
它的结论第一句就是「600036（招商银行）**非本框架（消费）覆盖标的**」——
数据全在，但**没有一个 Agent 该管银行**。

## 它和 A13-A16 的三个区别

| | A13-A16 | A20（本类） |
|---|---|---|
| 行业名 | 类级常量（写死） | **按每次请求解析**（`_industry_name_for`） |
| 关注指标 | 类级 `watch_keywords` | **按行业动态解析** |
| 路由 | `INDUSTRY_KEYWORDS` 关键词命中 | **兜底**：命中不了任何专属 Agent 时接管 |

## 行业怎么确定（**确定性，不猜**）

1. 文中的 **6 位代码** → `catalog/industry_of.industry_of()`（本地行情仓
   `quant_stock_basic.industry`，Tushare 名录）→ 实测 `600036 → 银行`；
2. 文中的**股票简称** → `synonym_dict.resolve_entity()` 解析成代码 → 同上；
3. 文中**直接出现行业名**（拿本地 110 个行业名做**最长匹配**）→ 用它；
4. 都拿不到 → 返回空，由基类按"无关注指标"如实说明（**不猜行业** ——
   猜错会让它用错误的框架分析，比"没有结论"更糟）。

## 关注指标怎么定（**动态，不写死**）

- 申万截面里 `extra.industry_name` 命中该行业的行（如"银行"→ 申万一级银行 PE 7.27）；
- `indicator` 里含该行业名或行业词的时序点；
- 渗透率点（若问句涉及）。

⚠️ **`watched_indicator_count > 0` 是关键**：基类
`_skip_reason()` 在它为 0 且无事件/无申万/无渗透率时**静默跳过 LLM** ——
那是本仓最典型的失败模式（"没什么可分析的"其实只是"没匹配上"）。
本类把"申万截面按 `extra.industry_name` 命中"也算作关注指标，
正是为了让银行这类**只有截面、没有专属时序指标**的行业不再被跳过。

## ★ 2026-10-08：一条问句里**多个行业**时，逐行业各出一节

用户那条报障问句里有**两个**行业（`601088` → `煤炭开采`；「宁波银行」→ `银行`）：
`煤炭开采` 有主（A15），`银行` 没人管 ⇒ 编排层的并集判据
（`supervisor.needs_generic_industries`）把 A20 挂上来管**银行**。
但本类原先的输出契约是**一次一个行业**（`_industry_name_for` 只解析一个行业）
⇒ 即使挂上了，也只有**一节**结论，另一个行业在交付里不存在。

⇒ 现在：`requested_industries()` 给出**本次要兜底的全部行业**（并集 − 已有专属
Agent 的行业，判据与编排层**同源**：`needs_generic_industries`）；
`execute()` 在 **≥2 个行业**时**逐行业各跑一次**既有路径，把 N 节结论合并成
一份输出（每节保留原有的 JSON 契约与字段）。

⚠️ **单行业（0 或 1 个）时走的是修复前那条原文路径** —— `execute()` 直接
`return await super().execute(input)`，一个字都不多：挂载、裁剪、输出**逐字不变**。
"""

from __future__ import annotations

import re
from typing import Any

from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, TraceStep
from src.domain.agents.analysis.unlock_teaching import render_unlock_teaching
from src.domain.agents.industry.base import IndustryAgentBase

#: 不能当行业名的噪声（这些名字本身就是"综合/其他"的意思，用来分析没有信息量）
_NOISE_INDUSTRIES = frozenset({"", "综合", "其他", "综合行业"})

#: 置信度排序（合并多行业结论时取**最弱**那一节）。
_CONF_RANK = {Confidence.HIGH: 2, Confidence.MEDIUM: 1, Confidence.LOW: 0}

#: **显式截断**上限：一条问句里点到的"没人管的行业"超过这个数时，
#: 只逐行业分析前 N 个（每个行业一次 reasoning 调用），并在 `result` 里
#: 记下被截断的行业名 —— 截断必须显式（"少写了一个行业"不许伪装成"只有这些行业"）。
_MAX_SECTIONS = 6


class GenericIndustryAgent(IndustryAgentBase):
    """兜底行业深度分析：行业名与关注指标**按输入内容解析**。

    继承 A13-A16 的骨架（本地景气信号 + 防幻觉 + 统一 prompt 契约），
    只覆盖"行业怎么确定 / 关注什么指标"这两个扩展点。
    """

    #: 类级默认（基类要求）。**实际值按请求解析**，见 `_industry_name_for`。
    industry_name = "综合"
    framework = "行业景气—估值—供需三维框架（无专属框架时用通用框架，并声明口径）"
    watch_keywords = ()   #: 类级默认空；按请求动态解析
    #: ★ 声明"我按请求解析行业与关注指标" —— 让护栏走动态分支而不是判我缺声明
    #: （见 `test_contract_consistency.py::test_industry_watch_keywords_...`）
    dynamic_industry = True
    #: 通用框架下不设行业特定警戒线，沿用基类默认（40 倍）
    pe_high_watermark = 40.0
    capabilities_names = ("generic_industry_analysis", "dynamic_industry_routing")

    FOCUS_HINT_KEY = "generic_industry_focus"
    """逐行业各出一节时，把"这一节分析哪个行业"钉在 `payload.hint` 上的键。

    ⚠️ **只在 A20 自己发起的子运行里出现**（编排层不写它）—— 单行业路径
    的 payload 里没有这个键，`resolve_industry()` 的第 1 层因此不生效，
    行为与修复前逐字一致。
    """

    system_prompt = (

        render_unlock_teaching("A20_generic_industry")

        +
        "资深行业分析师，负责**没有专属行业 Agent 覆盖**的行业"
        "（如银行/非银金融/公用事业/交通运输/建筑等）。"
        "你必须：①首句点名你在分析哪个行业以及**该行业是怎么确定的**"
        "（个股代码→本地名录 / 股票简称 / 问句直述）；"
        "②只依据给定数据点说话，**不得**用训练记忆补行业数据；"
        "③若本行业只有估值截面、没有专属产业时序指标，**必须明说这一边界**，"
        "并把结论限定在估值与已知宏观事实上；"
        "④禁脱离数据预测具体涨跌幅。"
    )

    def __init__(self, gateway, agent_id: str = "A20_generic_industry") -> None:
        super().__init__(agent_id, gateway)

    # ------------------------------------------------------------------
    # 行业解析（确定性三路 + 一路兜底）
    # ------------------------------------------------------------------

    @staticmethod
    def _text(payload) -> str:
        return f"{payload.focus or ''} {payload.user_query or ''}".strip()

    def resolve_industry(self, payload) -> tuple[str, str]:
        """返回 `(行业名, 依据说明)`；解析不出时行业名为空串。

        ⚠️ 真正的解析在 `catalog/industry_of.resolve_industry_from_text` ——
        **编排层（决定要不要挂本 Agent）与这里必须用同一份实现**，
        否则"编排层认为该挂兜底、Agent 自己解析出另一个行业"，
        结论里的行业名会与路由不一致，且不报错。

        ★ 2026-10-08 三层优先级（口径与编排层的挂载判据**对齐**）：

        1. `hint["generic_industry_focus"]` —— **本次子运行**被钉住的行业
           （逐行业各出一节时由本类自己写入，编排层不写它）；
        2. `requested_industries()` 的**首元素** —— "本次要兜底的第一个行业"。
           必须有这一层：问句里第一个行业有主（`煤炭开采` → A15）、
           第二个行业没人管（`银行`）时，单值解析器返回的是**有主的那个**
           ⇒ 若不看这一层，A20 会拿 A15 的行业去做兜底分析（**挂的是 A、写的是 B**）；
        3. 原单值三路解析（`resolve_industry_from_text`）—— **逐字保留**：
           `requested_industries()` 为空（无行业/都有人管）时的行为与修复前一致，
           既有测试与单行业路径不受影响。
        """
        forced = payload.hint.get(self.FOCUS_HINT_KEY) or {}
        if isinstance(forced, dict) and forced.get("industry"):
            return str(forced["industry"]), str(forced.get("basis") or "")
        requested = self.requested_industries(payload)
        if requested:
            return requested[0]
        from src.infrastructure.catalog.industry_of import (
            resolve_industry_from_text,
        )

        return resolve_industry_from_text(self._text(payload))

    def requested_industries(self, payload) -> list[tuple[str, str]]:
        """本次请求要 A20 兜底的**全部**行业 `[(行业名, 依据), …]`（保序去重）。

        ## 判据与编排层**同源**（不另写一套）

        `supervisor.needs_generic_industries(self._text(payload))`
        = 「并集解析出的行业」−「已有 A13–A16 之一接管的行业」。
        编排层用它决定**挂不挂** A20；本类用它决定**逐哪几个行业各出一节**。
        两处若各写一份，"挂的是 A、写的是 B"**不会报错**，只会静默写错行业。

        ## 为什么过滤掉"有主的行业"

        `煤炭开采`（601088）归 A15、`白酒` 归 A14 —— 它们的中观结论由专属
        Agent 产出。A20 只补**没人管**的那些（否则同一个行业会被两个 Agent
        各写一份，用户看到两份口径不同的结论）。
        """
        from src.orchestration.supervisor import (
            needs_generic_industries,
            resolve_focus_industries,
        )

        text = self._text(payload)
        names = [n for n in needs_generic_industries(text) if n]
        basis = {
            str(n): str(h)
            for n, h in resolve_focus_industries(text)
            if n
        }
        return [(n, basis.get(n, "")) for n in names if n not in _NOISE_INDUSTRIES]

    # ------------------------------------------------------------------
    # 多行业：逐行业各出一节（单行业走原文路径，逐字不变）
    # ------------------------------------------------------------------

    async def execute(self, input: AgentInput) -> AgentOutput:  # type: ignore[override]
        """★ ≥2 个"没人管"的行业 ⇒ 逐行业各跑一次既有路径，合并成一份输出。

        ⚠️ **0 或 1 个行业 ⇒ `return await super().execute(input)`**：
        执行的就是修复前那一条路径（同一个 `input`、同一份 prompt、同一个
        输出契约）—— "单行业逐字不变"是**结构保证**，不是靠断言维持。
        """
        payload = self._parse_payload(input.payload)
        industries = self.requested_industries(payload)
        if len(industries) < 2:
            return await super().execute(input)
        truncated = industries[_MAX_SECTIONS:]
        sections: list[tuple[str, str, AgentOutput]] = []
        for name, how in industries[:_MAX_SECTIONS]:
            # 每个行业一份**子 payload**（hint 是新建的 dict ⇒ 不污染原 payload，
            # 也保证并发/连续请求之间不串行业）。
            sub_payload = payload.model_copy(update={
                "hint": {
                    **(payload.hint or {}),
                    self.FOCUS_HINT_KEY: {"industry": name, "basis": how},
                },
            })
            sub_input = input.model_copy(
                update={"payload": sub_payload.model_dump()})
            sections.append((name, how, await super().execute(sub_input)))
        return self._merge_industry_sections(
            input, sections, truncated=[n for n, _h in truncated])

    def _merge_industry_sections(
        self, input: AgentInput,
        sections: list[tuple[str, str, AgentOutput]],
        *, truncated: list[str],
    ) -> AgentOutput:
        """N 节结论 → 一份 `AgentOutput`（每节结构 = 既有单行业输出）。"""
        blocks: list[str] = []
        per_section: list[dict[str, Any]] = []
        resolutions: list[dict[str, Any]] = []
        data_refs: list[str] = []
        tokens_in = 0
        tokens_out = 0
        for index, (name, how, out) in enumerate(sections, 1):
            head = f"### [{index}/{len(sections)}] {name}"
            if how:
                head += f"（行业判定依据：{how}）"
            blocks.append(f"{head}\n{out.conclusion}")
            section_result = dict(out.result or {})
            resolution = section_result.get("generic_industry_resolution") or {}
            if isinstance(resolution, dict) and resolution:
                resolutions.append({"industry": name, "basis": how, **resolution})
            try:
                tokens_in += int(section_result.get("tokens_in") or 0)
                tokens_out += int(section_result.get("tokens_out") or 0)
            except (TypeError, ValueError):
                pass
            per_section.append({
                "industry": name,
                "basis": how,
                "confidence": out.confidence.value,
                "conclusion": out.conclusion,
                "skipped": bool(section_result.get("skipped")),
                "skip_kind": str(section_result.get("skip_kind") or ""),
                "model_used": str(section_result.get("model_used") or ""),
                "industry_signal_calc": section_result.get("industry_signal_calc"),
            })
            for ref in out.data_refs:
                if ref not in data_refs:
                    data_refs.append(ref)
        # 置信度取**最弱**一节：逐行业结论里只要有一节不可信，
        # 整份交付就不该声称 high（与 A12 的"等级取最坏"同一条纪律）。
        merged_conf = min(
            (out.confidence for _n, _h, out in sections),
            key=lambda c: _CONF_RANK.get(c, 1))
        result: dict[str, Any] = {
            # 形状与单行业兼容：既有键仍在（取**第一节**，即主行业那份）
            "generic_industry_resolution": (
                resolutions[0] if resolutions
                else dict(sections[0][2].result.get(
                    "generic_industry_resolution") or {})),
            # 新增（加法）：逐行业各一份
            "generic_industry_resolutions": resolutions,
            "generic_industry_sections": per_section,
            "model_used": str(
                (sections[0][2].result or {}).get("model_used") or ""),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "multi_industry": True,
            "industries": [n for n, _h, _o in sections],
        }
        if truncated:
            result["industries_truncated"] = truncated
            result["industries_truncated_reason"] = (
                f"一节一个行业 = 一次推理调用，超过 {_MAX_SECTIONS} 个行业时"
                "只分析前几个（显式截断，不静默少写）")
        return AgentOutput(
            task_id=input.task_id, agent_id=self.agent_id,
            conclusion="\n\n".join(blocks),
            confidence=merged_conf,
            data_refs=data_refs, trace_id=input.task_id,
            reasoning_steps=[
                TraceStep(
                    step=index, step_type="llm_inference",
                    description=(
                        f"逐行业分析[{name}]："
                        f"model={(out.result or {}).get('model_used') or '?'} "
                        f"skipped={bool((out.result or {}).get('skipped'))}"))
                for index, (name, _how, out) in enumerate(sections, 1)
            ],
            result=result,
        )

    # ------------------------------------------------------------------
    # 覆盖基类的两个扩展点
    # ------------------------------------------------------------------

    def _industry_name_for(self, payload) -> str:
        name, _how = self.resolve_industry(payload)
        return name or self.industry_name  # 解析不出时退回"综合"

    def _framework_for(self, payload) -> str:
        name, how = self.resolve_industry(payload)
        if not name or name in _NOISE_INDUSTRIES:
            return self.framework
        return (f"{self.framework}；本行业={name}"
                f"（判定依据：{how or '未解析出'}）")

    def _watch_keywords_for(self, payload) -> tuple[str, ...]:
        """按解析出的行业给出关键词（用于**时序点**的子串匹配）。

        申万截面那类"行业名在 `extra` 里、不在 indicator 里"的点，
        由下面 `_watched_points` 单独处理 —— 两者的匹配面不同，必须分开。
        """
        name, _how = self.resolve_industry(payload)
        out: list[str] = []
        if name:
            out.append(name)
            # 行业名的核心词（"银行"→"银行"；"通信设备"→"通信"/"设备"）
            for token in re.findall(r"[\u4e00-\u9fff]{2,}", name):
                out.append(token)
        out.append("ind:sw_")          # 申万截面永远可支撑行业研判
        out.append("ind:penetration")  # 渗透率（若问句涉及成长赛道）
        return tuple(dict.fromkeys(out))

    def _watched_points(self, payload) -> list[dict[str, Any]]:
        """关注点 = 基类的子串匹配 **∪** 申万截面里 `extra.industry_name` 命中本行业的行。

        为什么要多这一路（实测）：银行这类行业**没有专属产业时序指标**，
        它在本地唯一的行业级数据就是申万截面里那一行
        （`ind:sw_first_pe_ttm:all` 且 `extra.industry_name == '银行'`）。
        只靠 `indicator` 子串匹配 ⇒ `watched_indicator_count == 0`
        ⇒ 基类 `_skip_reason()` **静默跳过 LLM** ⇒ 用户看到
        「无本行业关注指标…跳过LLM定性」——**那正是本次报障的形态**。
        """
        watched = list(super()._watched_points(payload))
        seen = {id(p) for p in watched}
        name, _how = self.resolve_industry(payload)
        if not name:
            return watched
        for p in payload.data_points:
            if id(p) in seen:
                continue
            extra = p.get("extra")
            if not isinstance(extra, dict):
                continue
            if str(extra.get("industry_name") or "").strip() == name:
                watched.append(p)
                seen.add(id(p))
        return watched

    def _enrich_result(self, payload, data: dict[str, Any]) -> dict[str, Any]:
        name, how = self.resolve_industry(payload)
        data = super()._enrich_result(payload, data)
        # 溯源：把"行业是怎么确定的"随结果下发（用户要能判这个结论准不准）
        data["generic_industry_resolution"] = {
            "industry": name,
            "basis": how,
            "resolved": bool(name),
            "watched_from_sw_section": sum(
                1 for p in self._watched_points(payload)
                if isinstance(p.get("extra"), dict)
                and str(p["extra"].get("industry_name") or "") == name),
        }
        return data
