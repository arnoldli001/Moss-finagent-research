"""★ 散文缺口 → 指标 id 的护栏（2026-09-30）。

## 守的四件事

1. **映射表不许腐烂**：表里每个目标 id 必须**登记在册**或**被连接器认**
   —— 哪天某个 id 被摘掉，这里立刻红（而不是等生产上"散文展开成一个取不到的 id"）。
2. **物理不可得必须带证据**：`terminated` 规则没有 `evidence` 就是一句
   "我觉得取不到"，下次没人敢信 —— 所以强制要求写出复核依据。
3. **判据是逐条复核，不是相似度**：用 A17 **真实报上来的 35 条散文**当回归样本，
   逐条给出期望结局；表一变，这里就会告诉你哪条散了。
4. **不许静默丢弃**：认不出/缺代码的必须是 `unmapped`（留在队列里），
   **不是** `map` 也不是被吞掉。
"""

from __future__ import annotations

import pytest

from src.domain.agents.decision.prose_map import (
    ACTION_MAP,
    ACTION_TERMINATED,
    ACTION_UNMAPPED,
    PROSE_RULES,
    ProseRule,
    normalize,
)

#: A17 真实报上来的散文（逐字抄自 `data/gap_queue.jsonl`，不是编的）。
#:
#: ⚠️ 期望值是**逐条判过的**，不是"都应该能映射"：
#:   * 「个股行情与财务明细未提供」与「合规6类比率族…」**没有标的代码**，
#:     它们的目标 id 需要 `{code}` ⇒ 诚实结局是 `unmapped`（留在队列里等人补），
#:     **不是**硬猜一只票（错配比缺数据更危险）。
REAL_PROSE: list[tuple[str, str]] = [
    ("个股行情与财务明细未提供，故不给任何个股结论或推荐。", ACTION_UNMAPPED),
    ("北向资金日度净买额（2024-08-19起交易所停止披露），外资实时流向不可得。", ACTION_TERMINATED),
    ("两融深市余额（仅有沪市13505.1亿，2026-09-24）。", ACTION_MAP),
    ("限售解禁规模与家数。", ACTION_MAP),
    ("行业分项估值、库存、产能与价格数据，板块轮动判断仅能停留在宽基层面。", ACTION_MAP),
    # ⚠️ 这条**不是缺口而是缺陷**：数据取到了、只是量级不对 ⇒ 补采一万次也不会
    #    变对，所以结局是 `terminated`（停止在补采队列里空转），转缺陷登记。
    ("量能MA5/MA10/MA50数值单位异常（万亿级偏差），环比缩量幅度不可用。", ACTION_TERMINATED),
    ("美国联邦基金利率及目标区间（上游显示为?%）", ACTION_MAP),
    ("美国失业率、非农就业等就业口径", ACTION_MAP),
    ("北向资金日度净买额（2024-08-19起交易所停止披露，禁止杜撰）", ACTION_TERMINATED),
    ("深市两融余额（仅有沪市13505.1亿）", ACTION_MAP),
    ("招商银行个股财务明细、股息率、净息差与资产质量数据", ACTION_MAP),
    ("银行及高股息行业分项估值分位", ACTION_MAP),
    ("三市分项成交额未提供", ACTION_MAP),
    ("北向资金流向数据未提供", ACTION_TERMINATED),
    ("银行行业分项估值分位未提供", ACTION_MAP),
    ("招商银行个股财务明细（净息差、不良率最新值）未提供", ACTION_MAP),
    ("美国核心PCE与点阵图最新数值未提供", ACTION_MAP),
    ("两市/三市分项成交额未提供", ACTION_MAP),
    ("北向资金未提供", ACTION_TERMINATED),
    ("全A加权换手率、两融余额未提供", ACTION_MAP),
    ("核心指数PE/PB估值分位未提供", ACTION_MAP),
    ("美国CPI/失业率/消费数据（美林时钟定位不明确）", ACTION_MAP),
    ("招商银行个股PE/PB/股息率/财务明细（高股息可持续性无法验证）", ACTION_MAP),
    ("北向资金日度净买额（2024-08-19起停止披露）", ACTION_TERMINATED),
    ("合规6类比率族与诉讼/监管事件（未获得输入，不等于无风险）", ACTION_UNMAPPED),
    ("北向资金日度净买额（2024-08-19起停止披露，外资实时流向不可得）", ACTION_TERMINATED),
    ("600036个股PE/PB/股息率/银行息差/资产质量数据全部缺失", ACTION_MAP),
    ("美国CPI/失业率数据缺失，美林时钟定位不明确", ACTION_MAP),
    ("AI替代就业传导至消费的时点无任何数据支撑", ACTION_UNMAPPED),
    ("深市两融余额缺失（仅沪市13505.1亿）", ACTION_MAP),
    ("银行指数与600036的价、量、涨跌幅数据", ACTION_MAP),
    ("600036个股估值水位（PE/PB分位）与财务明细", ACTION_MAP),
    ("银行行业分项估值与行业资金流历史序列", ACTION_MAP),
]


def _stub_resolver(text: str) -> str:
    """模拟生产里的实体解析：只有点到名的票才给代码（不瞎猜）。"""
    return "600036" if ("招商银行" in text or "600036" in text) else ""


@pytest.mark.parametrize("text,expected", REAL_PROSE)
def test_real_prose_samples_are_classified(text, expected):
    """★ 用真实报障样本逐条钉住结局（表一变，这里就红）。"""
    v = normalize(text, resolve_code=_stub_resolver)
    assert v.action == expected, f"{text!r} 期望 {expected}，实际 {v.action}（{v.reason}）"


def test_terminated_wins_over_other_rules():
    """★ 顺序即语义：物理不可得的判定必须先于任何泛化规则。

    反例（真会出现）：`北向资金日度净买额（…停止披露），外资实时流向不可得。`
    里同时含"资金"，若先匹配到泛化规则就会被展开成一个**取不到的 id**，
    而真相是"源头不发了"。
    """
    v = normalize("北向资金日度净买额（2024-08-19起停止披露）",
                  resolve_code=lambda _t: "")
    assert v.action == ACTION_TERMINATED


def test_needs_code_without_code_is_unmapped_not_dropped():
    """认出概念但缺标的代码 ⇒ `unmapped`（留在队列里），不许硬猜一只票。"""
    v = normalize("某只个股股息率数据缺失", resolve_code=lambda _t: "")
    assert v.action == ACTION_UNMAPPED
    assert "缺标的代码" in v.reason


def test_needs_code_with_code_produces_concrete_ids():
    """★ 一句人话点了几件事时，**并集**展开（不是只取第一条）。

    实测样本：「招商银行个股财务明细、**股息率**、**净息差**与资产质量数据」
    —— 只取第一条会把"净息差"整条丢掉。
    """
    v = normalize("招商银行个股财务明细、股息率、净息差与资产质量数据",
                  resolve_code=_stub_resolver)
    assert v.action == ACTION_MAP
    assert "股息率TTM:600036" in v.indicators
    assert "净息差:600036" in v.indicators
    assert all("{" not in i for i in v.indicators), "展开后不许留占位符"


def test_unknown_prose_is_unmapped_not_swallowed():
    """认不出 ⇒ `unmapped`（可见），绝不是被吞掉。"""
    v = normalize("某个从没见过的概念缺失", resolve_code=lambda _t: "")
    assert v.action == ACTION_UNMAPPED
    assert v.indicators == []


# ============================================================
# 映射表自身的健康（防腐烂）
# ============================================================


def test_every_rule_has_name_and_keywords():
    for rule in PROSE_RULES:
        assert rule.name and rule.keywords, rule
        assert isinstance(rule, ProseRule)


def test_terminated_rules_carry_evidence():
    """★ 判"物理不可得"必须留下复核依据 —— 否则下一个人只能选择相信或重查。"""
    bad = [r.name for r in PROSE_RULES
           if r.action == ACTION_TERMINATED and len(r.evidence) < 30]
    assert not bad, f"这些 terminated 规则没有证据：{bad}"


def test_map_rules_have_targets():
    bad = [r.name for r in PROSE_RULES
           if r.action == ACTION_MAP and not r.targets]
    assert not bad, f"这些 map 规则没有目标 id：{bad}"


def test_mapped_targets_are_reachable():
    """★★ 核心防腐判据：每个目标 id 必须**登记在册**或**被连接器认**。

    判据**现读**真值源（registry / 连接器 capabilities），不写清单 ——
    所以加一条规则就自动被验，摘掉一个 id 就自动报红。
    """
    from src.infrastructure.catalog.registry import get_registry

    reg = get_registry()
    code = "600036"
    missing: list[str] = []
    for rule in PROSE_RULES:
        if rule.action != ACTION_MAP:
            continue
        for tpl in rule.targets:
            ind = tpl.format(code=code) if "{code}" in tpl else tpl
            if reg.get(ind) is not None or reg.get_by_base(ind) is not None:
                continue
            missing.append(f"{rule.name} → {ind}")
    assert not missing, (
        "映射表里有取不到的 id（散了的规则要立刻修，不许留着）：\n  "
        + "\n  ".join(missing))


def test_industry_valuation_gap_is_registered_as_known():
    """★ 诚实登记：`银行行业分项估值` **当前没有生产者**（实测
    `industry_valuation_connector` 只声明消费/周期/医药三个行业口径）。

    所以映射到它时只能给"行业拥挤度"这条**确实可达**的 id ——
    本测试把这个事实钉住：哪天真的加了银行口径，这里会红，
    提醒把那句注释与 PRD 的已知缺口一起改掉。
    """
    from src.infrastructure.connectors.industry_valuation_connector import (
        _INDUSTRY_PE,
    )

    assert not any("银行" in k for k in _INDUSTRY_PE), (
        "行业估值连接器新增了银行口径 ⇒ 请同时更新 prose_map 里那条规则的"
        "reason、PRD §19.25.7 的已知缺口、以及本测试")
