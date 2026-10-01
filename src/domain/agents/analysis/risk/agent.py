"""A11 财务风险Agent（财务排雷）。"""

from __future__ import annotations

from typing import Any

from src.domain.agents.analysis.base import AnalysisAgentBase, AnalysisPayload
from src.domain.agents.analysis.platform_data_teaching import (
    render_platform_data_teaching,
)


def _find_value(payload: AnalysisPayload, keyword: str) -> float | None:
    for p in payload.data_points:
        if keyword in str(p.get("indicator", "")) and isinstance(p.get("value"), (int, float)):
            return float(p["value"])
    return None


#: 解禁压力阈值（**写进代码，不留在注释里** —— AGENTS.md 硬约束）
UNLOCK_CAP_MARKET_WARN = 500e8      # 单日合计解禁 >500 亿 → 全市场级压力
UNLOCK_CAP_SINGLE_WARN = 100e8      # 单只解禁 >100 亿 → 个股级冲击
UNLOCK_COUNT_WARN = 20              # 单日解禁家数 >20 → 面状压力


def _unlock_points(payload: AnalysisPayload) -> list[dict]:
    return [p for p in payload.data_points
            if str(p.get("indicator", "")).startswith("cal:unlock:")]


def _unlock_summary(payload: AnalysisPayload) -> dict:
    """解禁数据摘要（供 LLM 直接引用，不用自己从数据点里挑）。

    Returns:
        `{"has_data": bool, "market_cap": 元, "company_count": 家,
          "top_stock_cap": 元, "dates": [...], "top_stocks": [...]}`
    """
    pts = _unlock_points(payload)
    if not pts:
        return {"has_data": False}

    def _latest(suffix: str) -> float | None:
        rows = [p for p in pts
                if str(p.get("indicator", "")).endswith(suffix)]
        if not rows:
            return None
        rows = sorted(rows, key=lambda p: str(p.get("period_date", "")),
                      reverse=True)
        try:
            return float(rows[0].get("value") or 0)
        except (TypeError, ValueError):
            return None

    # 汇总所有解禁日（LLM 需要知道"未来还有多少压力"）
    dates = sorted({str(p.get("period_date", "")) for p in pts
                    if p.get("period_date")})
    # 找最大的一只（从 extra.top_stocks）
    top_stocks: list[dict] = []
    best = sorted(
        (p for p in pts if str(p.get("indicator", "")).endswith("market_cap")),
        key=lambda p: float(p.get("value") or 0), reverse=True)
    if best:
        extra = best[0].get("extra") or {}
        top_stocks = (extra.get("top_stocks") or [])[:5]
    return {
        "has_data": True,
        "market_cap": _latest("market_cap"),
        "company_count": _latest("company_count"),
        "top_stock_cap": _latest("top_stock_cap"),
        "dates": dates[:15],
        "top_stocks": top_stocks,
    }


def _unlock_flags(payload: AnalysisPayload) -> list[str]:
    """解禁压力旗标（本地规则；只标**客观事实**，不判方向）。

    ⚠️ 刻意**不输出"看跌"**：解禁是否造成下跌取决于股东是否真减持、
    市场承接力等，本地规则无法判断 —— 把方向判断交给 LLM 并附证据要求。
    """
    s = _unlock_summary(payload)
    if not s.get("has_data"):
        return []
    flags: list[str] = []
    cap = s.get("market_cap")
    if cap and cap > UNLOCK_CAP_MARKET_WARN:
        flags.append(f"单日解禁市值 {cap / 1e8:.0f} 亿元（超 "
                     f"{UNLOCK_CAP_MARKET_WARN / 1e8:.0f} 亿警戒线）")
    single = s.get("top_stock_cap")
    if single and single > UNLOCK_CAP_SINGLE_WARN:
        flags.append(f"最大单只解禁 {single / 1e8:.0f} 亿元（超 "
                     f"{UNLOCK_CAP_SINGLE_WARN / 1e8:.0f} 亿警戒线）")
    cnt = s.get("company_count")
    if cnt and cnt > UNLOCK_COUNT_WARN:
        flags.append(f"当日解禁 {cnt:.0f} 家（超 {UNLOCK_COUNT_WARN} 家，面状压力）")
    return flags


class RiskAnalysisAgent(AnalysisAgentBase):
    """财务排雷与造假预警（PRD A11，P1）。

    经典排雷比率（资产负债率/流动比率/毛利率异动）在本地计算并给出
    旗标，LLM只负责综合解读与定性。

    ## ★ 2026-09-28 第十四轮：接入「限售解禁」信号

    背景（用户指出）：
    > 「教给 A11 的 prompt 怎么用解禁数据」

    在此之前 `cal:unlock:*` 数据虽然进入了 A11 的白名单（能拿到），
    但 **prompt 里没有一个字提它** —— 数据摆在上下文里，模型不知道那是风险信号。
    （这正是 `requirement-closure-and-impact` skill 说的
      「贯通点第 ⑩ 层：Agent system_prompt 有没有教它怎么用」。）

    ## 解禁为什么是财务风险信号

    限售股解禁 = **潜在减持压力**。判据（本地规则，不靠 LLM 判断）：

    | 指标 | 阈值 | 含义 |
    |---|---|---|
    | 单日解禁市值 | > 500 亿 | 全市场级供给冲击 |
    | 最大单只解禁市值 | > 100 亿 | 个股级冲击 |
    | 解禁家数 | > 20 家 | 面状压力 |

    ⚠️ **本地只给"事实旗标"，方向判断交给 LLM** ——
    解禁不等于必跌（取决于股东是否真减持、市场承接力），
    本地规则只负责把「有大额解禁」这个**客观事实**标出来。
    """

    system_prompt = (
        "严谨风控专员，负责财务排雷。依据财务数据点与本地风险旗标识别造假征兆与"
        "偿债风险；未提及的风险不得断言。\n"
        "\n"
        "★ 解禁信号的使用规则（2026-09-28 新增）：\n"
        "- 上下文里的 `cal:unlock:*` 是**限售解禁**数据：\n"
        "  · `cal:unlock:market_cap`   当日合计解禁市值（元）\n"
        "  · `cal:unlock:top_stock_cap` 当日最大单只解禁市值（元）\n"
        "  · `cal:unlock:company_count` 当日解禁家数\n"
        "  · 其 `extra.top_stocks` 含个股级明细（代码/名称/占流通市值比/限售股类型）\n"
        "- **解禁 ≠ 必跌**：它是「潜在减持压力」，方向取决于股东行为与市场承接力。\n"
        "  你的职责是把**客观规模**说清楚（多大、几只、占比），而不是断言涨跌。\n"
        "- **有数据必须引用**：若上下文有解禁数据而结论完全没提，属于漏用信号。\n"
        "- **没有数据就声明缺口**：不得用训练记忆里的解禁数字填补。"
        # ★ 2026-09-29：平台自有数据八族的**使用口径**（风险视角：
        #   解禁=减持前置、个股/主线告警=事件面风险）
        + render_platform_data_teaching("A11_fin_risk")
    )

    def __init__(self, gateway, agent_id: str = "A11_fin_risk",
                 skill_library=None) -> None:
        super().__init__(agent_id, gateway, skill_library)

    def get_capabilities(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "capabilities": ["financial_mining", "fraud_warning",
                             "unlock_pressure_assessment"],
            "task_tier": self.task_tier,
        }

    def health_check(self) -> bool:
        return True

    def _prepare(self, payload: AnalysisPayload) -> None:
        debt_ratio = _find_value(payload, "资产负债率")
        current_ratio = _find_value(payload, "流动比率")
        flags: list[str] = []
        if debt_ratio is not None and debt_ratio > 70:
            flags.append(f"资产负债率{debt_ratio:g}%超70%警戒线")
        if current_ratio is not None and current_ratio < 1.0:
            flags.append(f"流动比率{current_ratio:g}低于1，短期偿债压力大")

        # ★ 解禁压力旗标（本地规则，阈值写进代码而非注释）
        unlock_flags = _unlock_flags(payload)
        flags.extend(unlock_flags)

        payload.hint["red_flag_calc"] = flags or ["常规财务比率未见明显异常"]
        # 把解禁摘要单独放一份，便于 LLM 直接引用（不用自己从数据点里挑）
        payload.hint["unlock_summary"] = _unlock_summary(payload)

    def _enrich_result(self, payload: AnalysisPayload, data: dict[str, Any]) -> dict[str, Any]:
        data["red_flag_calc"] = payload.hint.get("red_flag_calc")
        data["unlock_summary_calc"] = payload.hint.get("unlock_summary")
        return data

    def _requirements(self, payload: AnalysisPayload) -> str:
        has_unlock = bool((payload.hint.get("unlock_summary") or {})
                          .get("has_data"))
        unlock_line = (
            '- "unlock_assessment": 解禁压力评估（**必填**，须引用上面的解禁摘要：'
            "规模多大/几只/占流通市值比；并说明「潜在减持压力」而非断言涨跌）\n"
            if has_unlock else
            '- "unlock_assessment": 本次**无解禁数据**，如实填 '
            '「未获取到解禁数据，无法评估解禁压力」\n'
        )
        return (
            "输出JSON：\n"
            '- "conclusion": 风险评估，150字内\n'
            '- "confidence": high|medium|low\n'
            '- "risk_level": 低|中|高\n'
            '- "red_flags": 与red_flag_calc呼应的风险明细数组\n'
            + unlock_line +
            '- "key_points": 2-4条'
        )
