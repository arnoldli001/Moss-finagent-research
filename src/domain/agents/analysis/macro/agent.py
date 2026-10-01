"""A08 宏观分析Agent。"""

from __future__ import annotations

from typing import Any

from src.core.schemas import Confidence
from src.domain.agents.analysis.base import AnalysisAgentBase, AnalysisPayload
from src.domain.agents.analysis.unlock_teaching import render_unlock_teaching

# ★ 2026-09-28 第十轮：A08 纯模板触发关键词
# 命中下列词 → 视为"标准宏观问" → 走纯模板路径（不调 LLM，省 1 次云端 ~10s）。
# 关键词尽量收敛到"高频且有明确数据答案"的问法；非常规问法（"通胀
# 结构性变化""中美利差")仍走 LLM。
_MACRO_TEMPLATE_TRIGGERS: tuple[str, ...] = (
    "美联储", "加息", "降息", "FOMC", "FedWatch",
    "美国CPI", "美国通胀", "PCE", "联邦基金利率",
    "美国宏观", "美债收益率",
    # 中美利差（标准宏观指标组合）
    "中美利差",
)

#: ★ 2026-09-28 第二十三轮：**存在"必答口径"的问法** —— 命中这些词时，
#: 用户的**主问题**就是利率方向；此时若无利率数据，纯模板只能写
#: 「缺少联邦基金利率数据，无法判定方向」，等于**没回答**。
#:
#: 与 `_MACRO_TEMPLATE_TRIGGERS` 的区别（这两个集合的用途完全不同）：
#:   · triggers 回答"这是不是一道标准宏观问" → 决定**可不可以**走模板
#:   · required 回答"这道题必须有哪些数据才算答了" → 决定**该不该**走模板
#: 原先只有前者，于是"命中触发词 + 任意两条宏观数据"就能跳过 LLM。
_RATE_QUERY_KEYWORDS: tuple[str, ...] = (
    "加息", "降息", "利率", "联邦基金", "FOMC", "点阵图", "政策利率",
    # 英文/缩写问法
    "fed funds", "fomc", "rate hike", "rate cut",
)

#: 利率口径的**任一**指标存在 → 模板能给出利率方向（不必要求全都有）。
_FED_INDICATORS: tuple[str, ...] = (
    "us_fed_rate",       # AkShare FOMC 决议（**已停产**，仅作历史兜底）
    "fed:policy_range",  # FRED 目标区间（旧口径，[下限, 上限] 两条点）
    "fed:target_upper",  # ★ 第二十三轮拆出的独立序列
    "fed:target_lower",
    "fed:effr",          # DFF 有效联邦基金利率
    # ★★ 2026-09-30：**利率方向还可以从美债收益率读**（换源后的活序列）。
    #   为什么必须认它们：`us_fed_rate` 源停更后，"有没有利率数据"如果只认
    #   `fed:` 三条，一旦那三条当天没取到，模板就会退回 LLM 说"无法判定方向"
    #   —— 而库里其实有 `fred:DGS10 = 5.17%`（市场定价的利率预期）。
    #   这是用户点名要的「美债数据」第一次真正参与**方向判定**。
    "fred:DGS10",
    "fred:T10Y2Y",
)

#: 超过这个天数就在结论里**标注"该值可能已过时"**。
#:
#: 取 45 天：FOMC 一年 8 次会议（约 45 天一次）—— 超过一个会议周期还没更新，
#: 就说明这条源已停更或断档，必须提醒读者"这个数不是本期的"。
#: 为什么是标注而不是丢弃：陈旧值**仍然是真实历史值**，丢掉它等于
#: 让"AkShare 停更"这件事彻底不可见（而它正是要修的上游问题）。
_STALE_DAYS: int = 45


def _has_rate_data(points: list) -> bool:
    """payload 里有没有**任何**一条利率口径的数据。

    为什么用"任一"而不是"全部"：模板只需要一个可判方向的数
    （优先有效利率，退而取目标区间）。要求全部齐备会把
    "只有 FRED 目标区间" 这种**足够下结论**的场景也推回 LLM。
    """
    return any(
        str(p.get("indicator", "")) in _FED_INDICATORS
        or str(p.get("indicator", "")).lower().startswith(("fed:", "fred:"))
        for p in points
    )


def _asks_about_rate(text: str) -> bool:
    """这道题的主问题是不是**利率方向**。"""
    low = text.lower()
    return any(k.lower() in low for k in _RATE_QUERY_KEYWORDS)


def _latest_numeric(points: list, indicator: str) -> float | None:
    """从 data_points 取指定 indicator 最新一条数值（按 period_date 排序）。"""
    matching = [p for p in points if str(p.get("indicator", "")) == indicator]
    if not matching:
        return None
    matching.sort(key=lambda p: str(p.get("period_date", "")), reverse=True)
    v = matching[0].get("value")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _latest_period(points: list, indicator: str) -> str:
    """取指定 indicator 的**最新期间**（没有则空串）。

    用途：`fed:policy_range` 旧口径**同一天有多条**（上下限），
    必须先把同一天的取齐再当区间读，不能按"最新一条"取值。
    """
    periods = [str(p.get("period_date", ""))
               for p in points if str(p.get("indicator", "")) == indicator]
    return max(periods) if periods else ""


def _indicator_age_days(points: list, indicator: str) -> int | None:
    """指定 indicator 最新一期的**距今天数**（解析不出则 None）。

    为什么要它（第二十三轮收尾实测）：修好白名单后 A08 拿到了
    `us_fed_rate = 4.5`，但它的期间是 **2025-07-31**（AkShare 那条源停更，
    陈旧 14 个月）。模板据此给出「降息空间有限，年内再加息概率不低」——
    读起来像今天的判断，实际对应一年前的利率水平。
    **"没量到"与"量到 0"要分开**：陈旧值不是没有值，但必须标出它多旧。
    """
    from datetime import date

    period = _latest_period(points, indicator)
    if not period:
        return None
    try:
        y, m, d = (int(x) for x in period[:10].split("-"))
        return (date.today() - date(y, m, d)).days
    except (TypeError, ValueError):
        return None


class MacroAnalysisAgent(AnalysisAgentBase):
    """宏观经济周期定位与流动性分析（PRD A08，P0）。"""

    # 月度宏观指标最近12期足以判断趋势，全量历史会冲淡用户问题焦点
    context_max_periods = 12

    system_prompt = (

        render_unlock_teaching("A08_macro")

        +
        # ★★ 2026-09-30（用户口径：能精准解决的**不写进 prompt**）：
        #   **删掉硬编码的"有哪些数据"枚举**。
        #
        # 改前：「依据给定中国(CPI/PPI/M2/社融/PMI/GDP)与美国(CPI/核心CPI/非农/
        #   失业率/联邦利率/PCE)数据点…」—— 这份枚举有三个问题：
        #   ① 它**会漂移**：美国那半边写的是 `us_*` 时代的族名，而源停更后
        #      实际到货的是 `fred:*`（美债收益率/失业率/非农水平值/核心通胀**指数**）
        #      ⇒ prompt 与数据对不上，模型按旧名字找数据；
        #   ② 它是**数据事实**，而数据事实的正确落点是**数据本身**
        #      （现在每行都随 `hint.macro_basis` 带上口径："fred:PAYEMS =
        #      非农**水平值（千人）**，不是新增"、"PMI = 制造业，50=荣枯线"）；
        #   ③ 本项目已为"prompt 表达有哪些数据"付过代价（AGENTS.md 明令：
        #      **不许**用 prompt 表达「有哪些数据 / 在哪取 / 叫什么 / 怎么算」）。
        # 保留下来的是**语义判断**（怎么定位周期、方向怎么给、没数据怎么说）——
        # 那才是 prompt 该管的事。
        "资深宏观分析师。依据给定的中国与美国宏观数据点及财经事件，"
        "判断经济周期与流动性。\n"
        "- 每一行数据的**口径与单位**随数据下发（见各行附注：水平值 / 指数 / "
        "同比 / 需派生），引用数值时必须带口径，不要把\u300c指数\u300d当\u300c同比\u300d读；\n"
        "- 问加息/降息而无 FedWatch：声明数据缺口，按已有数据给方向"
        "（如通胀回落→降息概率升），禁答「无法判断」；\n"
        "- 周期用美林时钟：复苏/过热/滞胀/衰退；\n"
        "- **必查项**（美债收益率/失业率/非农/核心通胀/政策利率）有缺时，"
        "结论里如实声明缺了哪几维（缺口清单随数据下发），"
        "**不许**把\u300c没量到\u300d写成\u300c没有风险\u300d。"
        # ⚠️ 口径（数据侧已随行下发，prompt 不再重复）：`PMI` 是**制造业**口径
        #   （50=荣枯线）、`GDP` 是**累计值（亿元）**、`GDP:同比` 是**累计同比（%）**。
        #   判据：`tests/unit/test_macro_required_and_basis.py` 断言这些口径
        #   都在 `_MACRO_BASIS`（数据侧）里，且 prompt 里**不复述**它们。
        # ★★★ 2026-10-01 CHG-0155（用户口径）：**预期差**驱动股价。
        "★★★ 投研分析核心原则：**概念板块与个股的股价上涨动力来自「预期差」，不是预期本身**。\n"
        "- 预期 = 市场已有共识 ⇒ 已被定价 = 中位线（**不构成涨跌动力**）；\n"
        "- **预期差 = 市场已有预期 vs 数据/事件推断的实际预期** 之差 ⇒ 涨跌的**真正动力**；\n"
        "- 正向预期差（实际 > 市场）⇒ 资金流入 / 估值上修；\n"
        "- 负向预期差（实际 < 市场）⇒ 资金撤离 / 估值下修；\n"
        "- 宏观侧应用：判断美林时钟当前阶段 vs 市场共识（PMI/CPI/利率路径）的差距；\n"
        "- **禁止**只罗列「预期数据」就给出方向结论；必须先定位预期差方向与幅度，再判方向；\n"
        "- 当上下文无市场预期数据时，显式声明「市场预期不可得」，输出「无法判方向」。"
    )

    def __init__(self, gateway, agent_id: str = "A08_macro",
                 skill_library=None) -> None:
        super().__init__(agent_id, gateway, skill_library)

    def get_capabilities(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "capabilities": ["cycle_positioning", "liquidity_analysis"],
            "task_tier": self.task_tier,
        }

    def health_check(self) -> bool:
        return True

    # ★ 第十轮：纯模板路径 —— 命中标准宏观问 + 数据齐 → 跳过 LLM
    # ★ 第二十三轮：加"**必答口径**"判据（见下）
    def _should_skip_llm(self, payload: AnalysisPayload) -> bool:
        """命中标准宏观问 **且** 该问的必答口径齐备 → 才走纯模板。

        ## 第二十三轮为什么加第三道闸（真实报障）

        用户问：「当前宏观环境如何，预测下一年美国的加息、降息节奏，
        对A股的影响，以及AI应用加速失业率增加对消费的影响节奏…」
        前端显示：`model=rule-only … 缺少联邦基金利率数据，无法判定方向`

        **原判据的两道闸都能过，但它们是错的两道闸**：

            闸门 1：命中触发词（"加息"/"降息"）          ✅ 过
            闸门 2：宏观类数据点 ≥ 2                     ✅ 过（48 条）
                    ↑ 这 48 条全是 CPI/PPI/us_cpi_yoy/us_core_cpi
                      —— **一条利率数据都没有**

        也就是说：闸门 2 数的是"**宏观数据条数**"，而不是
        "**这道题需要的数据**"。用户问利率节奏，系统拿 CPI 条数
        证明了"可以跳过 LLM"，然后输出一句"缺少联邦基金利率数据"。
        **成本省下了，问题没回答。**

        ## 新增闸门 3：必答口径齐备

        当问题**明确问利率方向**（`_asks_about_rate`）时，
        payload 里必须至少有**一条**利率口径数据（`_has_rate_data`）。
        没有 → **退回 LLM 路径**：让模型带着真实缺口、用完整 prompt
        给出"方向 + 缺口声明"，而不是由模板写一句"无法判定"。

        ⚠️ 边界（刻意保留，避免过度修正）：
          · 问题**没在问利率**（如"美联储政策怎么看"、"美国宏观如何"）
            → 不触发闸门 3，模板照走（此时"缺少利率数据"是**如实登记**，
            不是答非所问）；
          · 利率数据**陈旧**（如 AkShare 停更在 2025-07）仍算"有数据" ——
            陈旧度由 `_build_context` 的新鲜度标注负责，模板的
            `key_points` 会带出原值，让用户自己看到期数。
            把"陈旧"也判成缺失会把可用信息一起丢掉。
        """
        text = f"{payload.user_query} {payload.focus}".lower()
        if not any(t.lower() in text for t in _MACRO_TEMPLATE_TRIGGERS):
            return False
        # 必须有至少 2 条宏观类数据点（中美各 1 或单边 2）才走模板：
        # 避免"用户只问'美联储'但 collect 完全失败"时硬出模板结论
        # 模板会用真实数值拼结论，没有数据时反而是幻觉。
        macro_count = sum(
            1 for p in payload.data_points
            if str(p.get("indicator", "")).lower().startswith(
                # ★ 2026-09-30 补 `fred:`：换源后美债/失业率都是 `fred:*`，
                #   不认它 ⇒ **明明有数据却判"宏观数据不足"**、退回 LLM
                #   （"数据到了、判据看不见"的又一次 —— 与白名单那次同类）。
                ("us_", "fred:", "fed:", "cpi", "ppi", "m2", "社融"))
        )
        if macro_count < 2:
            return False
        # ★ 闸门 3：问利率就必须有利率数据，否则退回 LLM 如实作答
        if _asks_about_rate(text) and not _has_rate_data(payload.data_points):
            return False
        return True

    def _build_rule_only_result(self, payload: AnalysisPayload) -> dict[str, Any]:
        """A08 纯模板：基于已采集的中美宏观数值拼结论。

        设计目标：命中标准宏观问时给出**有数据支撑**的方向判断，
        不杜撰数字、不调用 LLM、审计可追溯（model_used=rule-only）。

        ## ★ 第二十三轮：利率口径不能再取"最新一条当单点"

        `fed:policy_range` 的历史实现把 FRED 的**三个序列**
        （DFEDTARU 上限 / DFEDTARL 下限 / DFF 有效利率）**写成同一个
        indicator**，一天三条、值分别是 4.00 / 3.75 / 3.88。
        而 `_latest_numeric` 取 `period_date` 最新的**第一条** ——
        同日三条里挑中的是**上限或下限之一**，于是结论会写成
        「目标区间 3.75%（FRED 口径）」——
        **把区间下限当成政策利率**报给用户。这是比"缺数据"更危险的错：
        数字看着有据，语义是错的。

        （当前 `fedwatch_connector` 已按第二十三轮拆成
        `fed:target_upper` / `fed:target_lower` / `fed:effr`；
        下面同时兼容旧的混合口径，避免历史库数据读不出来。）

        新口径的取值优先级：**有效利率 DFF > 目标区间中点 > 旧混合口径的最新值**。
        """
        pts = payload.data_points
        # ★★ 2026-09-30：**取证清单换到"活的"序列上**（用户口径：从数据处理侧解决）。
        #
        # 改前：模板读 `us_fed_rate` / `us_unemployment` —— 而这两条的源
        # （AkShare 东财 `macro_usa_*`）**已停更并停产**（`enabled: false`），
        # 于是"有数据"其实是**一年前的数**，美林时钟的失业率那一维永远是旧值。
        # 改后：优先读 FRED 换源后的 `fed:effr` / `fred:UNRATE`，把旧序列降为
        # **兜底**（历史库里的点仍读得出来，不假装它不存在）。
        # `us_cpi_yoy` 的源**仍在更新**（实测到 2026-08），继续用。
        us_fed = _latest_numeric(pts, "us_fed_rate")
        us_cpi = _latest_numeric(pts, "us_cpi_yoy")
        us_unemp = _latest_numeric(pts, "fred:UNRATE")
        if us_unemp is None:
            us_unemp = _latest_numeric(pts, "us_unemployment")   # 兜底（旧源）
        #: 用户点名要的「美债数据」：10 年期收益率与 10Y−2Y 期限利差
        dgs10 = _latest_numeric(pts, "fred:DGS10")
        t10y2y = _latest_numeric(pts, "fred:T10Y2Y")
        cn_cpi = _latest_numeric(pts, "CPI")
        cn_ppi = _latest_numeric(pts, "PPI")

        # ---- 利率口径：先拆开的独立序列，再退旧的混合口径 ----
        effr = _latest_numeric(pts, "fed:effr")
        upper = _latest_numeric(pts, "fed:target_upper")
        lower = _latest_numeric(pts, "fed:target_lower")
        legacy = _latest_numeric(pts, "fed:policy_range")

        target_text = ""
        if upper is not None and lower is not None:
            target_text = f"{lower:.2f}%~{upper:.2f}%"
        elif legacy is not None:
            # 旧口径：同一 indicator 一天有上下限两条。取**同一天**的全部值
            # 当区间读，而不是"最新一条当单点"。
            legacy_vals = sorted(
                float(p["value"]) for p in pts
                if str(p.get("indicator", "")) == "fed:policy_range"
                and p.get("value") is not None
                and str(p.get("period_date", "")) == _latest_period(pts, "fed:policy_range")
            )
            if len(legacy_vals) >= 2:
                target_text = f"{legacy_vals[0]:.2f}%~{legacy_vals[-1]:.2f}%"
            elif legacy_vals:
                target_text = f"{legacy_vals[0]:.2f}%（单边值，非区间）"

        # 方向判断优先用**有效利率**（可判鹰鸽），其次 us_fed_rate，
        # 最后才退到"只有目标区间"的弱口径。
        anchor = effr if effr is not None else us_fed
        anchor_label = "有效利率" if effr is not None else "联邦基金利率"
        anchor_age = (_indicator_age_days(pts, "fed:effr") if effr is not None
                      else _indicator_age_days(pts, "us_fed_rate"))
        if anchor is not None:
            if anchor >= 4.5:
                stance = "鹰派偏紧"
                direction = "降息空间有限，年内再加息概率不低"
            elif anchor >= 3.5:
                stance = "中性偏紧"
                direction = "维持高位，加息节奏趋缓"
            else:
                stance = "鸽派偏松"
                direction = "降息周期开启"
            if target_text:
                direction += f"（当前目标区间 {target_text}）"
            # ★★ 第二十三轮：**陈旧标注**（报障收尾发现的第二层缺陷）
            #
            # 实测：修复白名单后 A08 拿到 `us_fed_rate = 4.5`，
            # 而它的期间是 **2025-07-31**（AkShare 那条源停更，陈旧 14 个月）。
            # 模板据此输出「降息空间有限，年内再加息概率不低」——
            # **一个与一年前数据对应的结论**，读起来却像今天的判断。
            # 而同期 FRED 口径已在库里给出 3.88 / 3.75~4.00（另一档）。
            #
            # 只标注、**不改写数值**：如实把"这个数是哪一期的"告诉读者，
            # 由人判断可信度。不标 = 让用户把一年前的利率当今天的用。
            if anchor_age is not None and anchor_age > _STALE_DAYS:
                direction += (
                    f"（⚠️ 该值期间距今 {anchor_age} 天，可能已过时；"
                    f"目标区间为最新一期，请以区间为准）")
        elif target_text:
            stance = "中性"
            direction = f"当前目标区间 {target_text}（FRED 口径）"
        else:
            stance = "不明确"
            direction = "缺少联邦基金利率数据，无法判定方向"

        # 通胀状态
        if us_cpi is not None:
            if us_cpi >= 4.0:
                inflation_state = f"高通胀（核心CPI {us_cpi:.1f}%）"
            elif us_cpi >= 2.5:
                inflation_state = f"温和通胀（CPI {us_cpi:.1f}%）"
            elif us_cpi >= 1.0:
                inflation_state = f"低通胀（CPI {us_cpi:.1f}%）"
            else:
                inflation_state = f"通缩风险（CPI {us_cpi:.1f}%）"
        else:
            inflation_state = "通胀数据缺失"

        # 中国端
        cn_state_parts: list[str] = []
        if cn_cpi is not None:
            cn_state_parts.append(f"CPI {cn_cpi:.1f}%")
        if cn_ppi is not None:
            cn_state_parts.append(f"PPI {cn_ppi:.1f}%")
        cn_state = "、".join(cn_state_parts) if cn_state_parts else "中国宏观数据缺失"

        # 美林时钟：通胀 + 失业率 → 象限定位
        if us_cpi is not None and us_unemp is not None:
            if us_cpi >= 3.0 and us_unemp <= 4.5:
                cycle = "过热"
            elif us_cpi >= 3.0 and us_unemp > 4.5:
                cycle = "滞胀"
            elif us_cpi < 3.0 and us_unemp <= 4.5:
                cycle = "复苏"
            elif us_cpi < 3.0 and us_unemp > 4.5:
                cycle = "衰退"
            else:
                cycle = "不明确"
        else:
            cycle = "不明确（缺CPI/失业率）"

        confidence_level = (Confidence.MEDIUM
                            if anchor is not None or target_text
                            else Confidence.LOW)

        # ★ 第二十三轮：把"为什么走了模板"落进审计（见 base.py 的
        #   `_audit_rule_only`）。原来审计里 A08 是**空白**，
        #   分不清"走了模板"和"压根没跑"。
        macro_n = sum(
            1 for p in pts
            if str(p.get("indicator", "")).lower().startswith(
                ("us_", "fred:", "fed:", "cpi", "ppi", "m2", "社融")))
        rate_asked = _asks_about_rate(
            f"{payload.user_query} {payload.focus}".lower())
        reason = (
            f"macro_template; macro_points={macro_n}; "
            f"rate_query={rate_asked}; "
            f"rate_data={'yes' if _has_rate_data(pts) else 'no'}; "
            f"anchor={anchor_label if anchor is not None else 'none'}; "
            f"anchor_age_days={anchor_age if anchor_age is not None else 'n/a'}"
        )

        # ★ 目标区间的**期间**：优先取拆分后的单值序列 `fed:target_upper`，
        #   取不到再退回旧口径 `fed:policy_range` —— `or` 语义（**两者取其一**，
        #   不是拼接）。原先这一句内联在 f-string 里，为压 E501 行长把它提出来；
        #   ⚠️ **提取时不许顺手改语义**：我第一版误写成字符串拼接，
        #   会把两个期间一起打出来，"期间"就变成一串看不懂的东西。
        range_period = (_latest_period(pts, 'fed:target_upper')
                        or _latest_period(pts, 'fed:policy_range'))

        # ★★ 2026-09-30：必查项缺口与口径都从 **`payload.hint`（数据侧）** 取，
        #   不写进 prompt。取不到 hint 时**如实降级为空**（老调用点/单测仍能跑）。
        hint = getattr(payload, "hint", None) or {}
        missing = list(hint.get("macro_required_missing") or [])
        basis = dict(hint.get("macro_basis") or {})
        got_n = int(hint.get("macro_required_got") or 0)
        total_n = int(hint.get("macro_required_total") or 0)

        return {
            "conclusion": (
                f"美国利率政策：{direction}；当前{stance}。通胀状态：{inflation_state}。"
                f"美林时钟定位：{cycle}。中国端：{cn_state}。"
                f"（model=rule-only，纯模板判定，未调 LLM；数据缺口见原文）"
            )[:500],
            "confidence": "high" if confidence_level == Confidence.MEDIUM else "low",
            "cycle_position": cycle,
            "liquidity": ("收紧" if (anchor is not None and anchor >= 4.5)
                          else "中性" if (anchor is not None and anchor >= 3.0)
                          else "宽松" if anchor is not None else "不明确"),
            "key_points": [
                # ★ 第二十三轮：利率与目标区间**分开报**，不再把区间的
                #   某一边当成利率。缺哪一项就写"未提供"，不写 "?"（"?" 会被
                #   误读成"值很小/格式问题"，而它其实是"没有"）。
                #   同时带**期间**：用户要能自己看出这个数是不是本期的。
                ((f"有效利率 {effr:.2f}%（{_latest_period(pts, 'fed:effr')}）")
                 if effr is not None
                 else ((f"联邦基金利率 {us_fed:.2f}%"
                        f"（{_latest_period(pts, 'us_fed_rate')}）")
                       if us_fed is not None else "有效利率 未提供")),
                (f"目标区间 {target_text}（{range_period}）"
                 if target_text else "目标区间 未提供"),
                # ★★ 2026-09-30：**用户点名的「美债数据」进结论**。
                #   为什么放 key_points 而不是 prompt：这是**数据**（有值/没值、
                #   哪一期、什么口径），数据侧组装就该把它摆出来，模型与用户
                #   都直接看到；写进 prompt 只会变成"请记得提美债收益率"这种
                #   会漂移的叮嘱（本项目实测过 prompt 漂移的代价）。
                (f"10 年期美债收益率 {dgs10:.2f}%（{_latest_period(pts, 'fred:DGS10')}）"
                 + (f"、10Y−2Y 利差 {t10y2y:+.2f}pp"
                    if t10y2y is not None else "")
                 if dgs10 is not None else "10 年期美债收益率 未提供"),
                f"美国 CPI {us_cpi if us_cpi is not None else '?'}%"
                + (f"、失业率 {us_unemp if us_unemp is not None else '?'}%"
                   if us_unemp is not None else ""),
                f"中国 {cn_state}",
            ][:6],
            "risks": [
                "纯模板结论未引用事件层；如需讨论特定事件影响需走完整 LLM 路径",
                "数据缺口见 validated_points 的 expried/lagging 标记",
                # ★★ 2026-09-30：**必查项缺口如实声明**（数据侧下发，见
                #   `supervisor` 的 payload builder：`hint.macro_required_missing`）。
                #   为什么必须有：宏观结论的必查项有 13 条（中国四件套 +
                #   美债/失业率/非农/核心通胀/政策利率），任何一条没取到，
                #   结论里就会出现"少了一维但看不出来"——
                #   **"没量到"必须与"量到 0"分开显示**（AGENTS.md 纪律）。
                *([f"⚠️ 宏观必查项未取到 {len(missing)} 条："
                   f"{'、'.join(missing)}（{got_n}/{total_n} 已取到，"
                   f"结论中相应维度**未覆盖**，不是'无风险'）"]
                  if missing else
                  [f"宏观必查项 {got_n}/{total_n} 条已取到"]),
                #: 口径随数据下发（**不进 prompt**）—— 让"这个数是什么"跟着数走
                *([f"口径：{'；'.join(f'{k} = {v}' for k, v in list(basis.items())[:6])}"]
                  if basis else []),
            ],
            "_rule_only": True,
            "rule_only_reason": reason,
            "model_used": "rule-only",
        }

    def _requirements(self, payload) -> str:
        return (
            "输出JSON：\n"
            '- "conclusion": 首句直接答问（缺对应量化数据时声明缺口并给方向，'
            "禁杜撰百分比），200字内\n"
            '- "confidence": high|medium|low\n'
            '- "cycle_position": 复苏|过热|滞胀|衰退|不明确\n'
            '- "liquidity": 宽松|中性|收紧|不明确\n'
            '- "key_points": 3-5条\n'
            '- "risks": 1-3条'
        )
