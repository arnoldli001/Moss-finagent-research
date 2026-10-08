"""A12_compliance 的**多标的**合规判定（一次问句里 ≥2 只票）。

## 修的是什么（只读审计定位，属"**给错答案**"而不是"缺数据"）

`CHG-0216` 之后个股指标按 `resolved_codes` **逐只**排，所以 A12 的
`payload.data_points` 里同时躺着两只票的同名指标
（`商誉占净资产比:601088` 与 `商誉占净资产比:002142`）。
而 `evaluate_compliance()` 原先把整份输入当成**一只票**在扫：

    `_find_value(data_points, "货币资金")` → 整份列表里的**首个**命中
    `_find_value(data_points, "有息负债")` → 同上（**可能是另一只票的**）
    `compliance_families_measured`        → 跨代码**并集**
    一个 `level`                            → 收下全部标的，旗标里没有代码/公司

⇒ 用户看到一条**无归属**、可能张冠李戴的合规等级；最坏是一张
"A 量到 3 族 + B 量到 3 族 = 已量到 6/6 族、未见风险"的**假合格证**
（`compliance_families_measured` 的并集让"没有任何一只票的 6 族齐过"
这件事在界面上**看不出来**）。这比缺数据更危险：缺数据用户看得出来，错数看不出来。

## 本文件守的四件事

1. ★★ **每只票各自判定**，值只来自**各自的代码**（不是跨代码"首个命中"）；
2. ★★ `has_input` / 族清单**不许跨代码合并** ——
   "6/6 族"必须是**同一只票**的 6 个族，汇总字段取**交集**；
3. ★ **修复前的错法可复现**（把失败模式钉在断言里，防止悄悄回退）；
4. ★ **回归护栏**：单标的（0/1 个代码）与修复前**逐字一致**
   （7 个既有字段按字面量对齐 —— 这些字面量取自修复前的实测输出）。

跑法：
    uv run python -m pytest tests/unit/test_compliance_multi_target.py -q
"""
from __future__ import annotations

import asyncio

from src.domain.agents.analysis import ComplianceAnalysisAgent
from src.domain.agents.analysis.compliance import logic
from src.domain.agents.analysis.compliance.logic import (
    FLAG_NO_SIGNAL,
    FLAG_UNMEASURED,
    FLAG_UNMEASURED_MULTI,
    LEVEL_UNMEASURED,
    UNLABELED_TARGET,
    _payload_codes,
    _point_code,
    evaluate_compliance,
)
from tests.unit.test_compliance_agent import (  # noqa: F401 复用既有替身与构造器
    REPLY,
    FakeGateway,
    _dp,
    _make_input,
)

BANK = "002142"    # 宁波银行
COAL = "601088"    # 中国神华

#: 修复前的**真值口径**：整份输入一只票 = 这 7 个字段（模块 docstring §4）
_LEGACY = (
    "compliance_flags", "severe_flag_count", "compliance_level_calc",
    "compliance_measured", "compliance_families_measured",
    "compliance_families_unmeasured", "compliance_event_flags",
)

#: 每个族一个能触发取值的样本指标名（含该族关键词即可，`_find_value` 是子串匹配）
_FAMILY_INDICATOR = {
    "关联交易": "关联交易占营收比",
    "商誉": "商誉占净资产比",
    "质押": "大股东质押比例",
    "担保": "对外担保占净资产比",
    "货币资金": "货币资金占总资产比",
    "有息负债": "有息负债占总资产比",
}


def _fam(code: str, values: dict[str, float]) -> list[dict]:
    """构造 `指标名:代码` 形状的数据点（与连接器实测输出同形状）。"""
    suffix = f":{code}" if code else ""
    return [_dp(_FAMILY_INDICATOR[k] + suffix, v) for k, v in values.items()]


def _legacy(result: dict) -> dict:
    return {k: result[k] for k in _LEGACY}


def _events() -> list[dict]:
    return [{"item_id": "i", "event_type": "litigation", "direction": "negative",
             "subject": "某公司", "evidence_quote": "公司被证监会立案调查",
             "confidence": 0.9}]


# ===========================================================================
# ① 代码认领层：从 indicator 后缀拿到归属
# ===========================================================================


def test_point_code_parses_the_tail() -> None:
    """★ 归属只认**最后一段**里的 6 位数字（三种写法都认）。"""
    assert _point_code({"indicator": "商誉占净资产比:601088"}) == "601088"
    assert _point_code({"indicator": "600519.SH"}) == ""
    assert _point_code({"indicator": "PB:600519.SH"}) == "600519"
    assert _point_code({"indicator": "PB:SH600519"}) == "600519"
    # 行业/大盘/宏观指标**不是**标的：没有 6 位数字段
    assert _point_code({"indicator": "mkt:turnover:total"}) == ""
    assert _point_code({"indicator": "ind:sw_third_pe_ttm:all"}) == ""
    assert _point_code({"indicator": "PE"}) == ""
    assert _point_code({"indicator": ""}) == ""


def test_payload_codes_sorted_and_deduped() -> None:
    """★ 代码清单**排序**（与点的先后顺序无关）：换个返回顺序不该换一份结论。"""
    points = (_fam(COAL, {"商誉": 1.0}) + [{"indicator": f"PB:{BANK}", "value": 0.7}]
              + _fam(COAL, {"质押": 0.0}) + [{"indicator": "mkt:north_flow", "value": 1.0}])
    assert _payload_codes(points) == [BANK, COAL]
    assert _payload_codes([{"indicator": "mkt:north_flow", "value": 1.0}]) == []


def test_no_catalog_indicator_ends_with_a_code_shaped_segment() -> None:
    """★★ 守卫：登记目录里**没有**"以 6 位数字结尾、但它不是个股代码"的指标。

    这是"单标的走原样口径"这条优化能成立的前提：一旦某个非个股指标以 6 位数字
    结尾（期号、集合号），它会被认领成一个**幽灵标的**，把单标的输入误判成
    多标的（症状是等级偏保守的「未量到」）。判据直接扫 planner 目录，
    目录里将来加一条这样的 id 就会红。
    """
    from src.orchestration.planner import INDICATOR_CATALOG

    ids = [str(item.get("id") or "") for item in INDICATOR_CATALOG]
    assert len(ids) >= 50, f"目录只读到 {len(ids)} 条 —— 枚举坏了，本判据会空跑通过"
    offenders = [
        ind for ind in ids
        if "{" not in ind and _point_code({"indicator": ind})
    ]
    assert not offenders, (
        "以下登记指标会被误认成个股代码（末尾恰好一个 6 位数字）：\n"
        + "\n".join(f"  · {o}" for o in offenders)
        + "\n→ 单标的输入会被误判成多标的。要么改指标名，要么在 "
          "`logic._CODE_IN_SEGMENT_RE` 的认领口径里排除它。"
    )


# ===========================================================================
# ② 判定层：逐只判定，且**不许跨代码合并**
# ===========================================================================


def test_each_code_gets_its_own_value_not_the_first_hit() -> None:
    """★★★ 本文件最重要的一条：同一族的两只票各取**自己的**值。

    构造：中国神华商誉 40%（超阈值）在前、宁波银行商誉 1%（后面）。
    修复前 `_find_value` 取首个命中 ⇒ 宁波银行会被**加上**中国神华的旗标。
    """
    points = _fam(COAL, {"商誉": 40.0}) + _fam(BANK, {"商誉": 1.0})
    out = evaluate_compliance(points)

    by = {row["code"]: row for row in out["compliance_per_code"]}
    assert set(by) == {COAL, BANK}, by
    assert by[COAL]["level"] == "中", by[COAL]
    assert by[BANK]["level"] == "无", by[BANK]
    # 旗标只属于中国神华，且带归属前缀（宁波银行不许染上）
    assert any(COAL in f and "商誉" in f for f in by[COAL]["risk_flags"]), by[COAL]
    assert by[BANK]["risk_flags"] == [], by[BANK]


def test_three_families_each_is_never_six_of_six() -> None:
    """★★ 两只票各自 3 个族（**互不相同**）⇒ 汇总字段不许报「6/6 族」。

    这是那张假合格证的**机器复现**：修复前 `compliance_families_measured`
    是跨代码并集，于是"A 量到 3 族 + B 量到 3 族 = 6/6"，
    而**没有任何一只票**的 6 族齐过 —— 用户会读成"这家公司查全了、没问题"。
    """
    a_values = {"商誉": 1.0, "质押": 0.0, "担保": 0.0}
    b_values = {"货币资金": 1.0, "有息负债": 1.0, "关联交易": 1.0}
    points = _fam(COAL, a_values) + _fam(BANK, b_values)
    out = evaluate_compliance(points)

    union = sorted(set(a_values) | set(b_values))
    assert union == sorted(logic._RULE_FAMILIES), (  # noqa: SLF001 修复前算的就是它
        "本用例必须让两票的族**互补**，否则复现不出并集那条错法"
    )
    assert out["compliance_families_measured"] != union, (
        f"汇总字段又变成了跨代码并集：{out['compliance_families_measured']}"
    )
    assert out["compliance_families_measured"] == [], (
        f"两票没有任何共同量到的族 ⇒ 交集应为空，实际 {out['compliance_families_measured']}"
    )
    # 逐只明细必须各自 3/6（不是 6/6）
    coverage = {
        row["code"]: (len(row["families_measured"]), len(row["families_unmeasured"]))
        for row in out["compliance_per_code"]
    }
    assert coverage == {COAL: (3, 3), BANK: (3, 3)}, coverage


def test_goodwill_in_a_and_debt_in_b_never_form_a_flag_for_anyone() -> None:
    """★ 审计原话的第二个形状：一只票有**商誉**、另一只有**有息负债**。

    修复前：`商誉` 只命中 A（旗标归 A，还算对），但 `有息负债` 被算进
    **A 的族清单**（`_find_value` 跨代码首个命中）—— 于是 A 的"量到"里
    混着 B 的数。判据：旗标只能来自**自己那只票**的数据。
    """
    points = _fam(COAL, {"商誉": 40.0}) + _fam(BANK, {"有息负债": 42.0})
    out = evaluate_compliance(points)

    assert "存贷双高" not in " | ".join(out["compliance_flags"]), out
    by = {row["code"]: row for row in out["compliance_per_code"]}
    assert by[COAL]["families_measured"] == ["商誉"], by[COAL]
    assert by[BANK]["families_measured"] == ["有息负债"], by[BANK]
    assert by[BANK]["risk_flags"] == [], by[BANK]
    assert any("商誉" in f and COAL in f for f in by[COAL]["risk_flags"]), by[COAL]


def test_double_high_never_pairs_two_companies() -> None:
    """★★★ 一只票有`货币资金`、另一只有`有息负债` ⇒ **不许**拼出一条「存贷双高」。

    这是"给错答案"里最典型的一条：修复前两个数各自"首个命中"，
    拼出来的那条旗标**两家公司单独看都不成立**。
    """
    points = _fam(COAL, {"货币资金": 45.0}) + _fam(BANK, {"有息负债": 42.0})
    out = evaluate_compliance(points)

    flat = " | ".join(out["compliance_flags"])
    assert "存贷双高" not in flat, f"汇总层拼出了跨公司的存贷双高：{flat}"
    for row in out["compliance_per_code"]:
        assert not any("存贷双高" in f for f in row["risk_flags"]), row
    assert out["compliance_level_calc"] == "无", out
    # 逐只只量到一半 ⇒ 汇总族清单是**交集**（空），不是并集
    assert out["compliance_families_measured"] == [], out


def test_double_high_still_fires_within_one_code() -> None:
    """★ 反面对照：同一只票的两个数都高 ⇒ 该触发还必须触发。

    没有这条，"禁止跨代码拼接"很容易被写成"禁止拼接"（把真信号一起删掉）
    —— 那是把一个假绿换成一个假红。
    """
    points = _fam(COAL, {"货币资金": 45.0, "有息负债": 42.0})
    out = evaluate_compliance(points)
    assert out["compliance_level_calc"] == "高", out
    assert any("存贷双高" in f for f in out["compliance_flags"]), out
    assert out["compliance_families_measured"] == ["有息负债", "货币资金"], out


def test_multi_target_flags_carry_the_owner_code() -> None:
    """★ 旗标必须自带归属（代码前缀），否则用户无法判断这条风险是谁的。"""
    points = _fam(COAL, {"质押": 85.0}) + _fam(BANK, {"商誉": 1.0})
    out = evaluate_compliance(points)
    pledge = [f for f in out["compliance_flags"] if "质押" in f]
    assert pledge, out
    assert pledge[0].startswith(f"[{COAL}]"), pledge
    assert any(COAL in f for f in out["compliance_per_code"][1]["flags"]
               + out["compliance_per_code"][0]["flags"]), out


def test_one_code_without_any_compliance_data_blocks_a_clean_verdict() -> None:
    """★★★ A 六族齐且都没超阈值、B **一条合规数据都没有** ⇒ 汇总不许报「无」。

    修复前：level 直接是「无」+「未见明显合规风险信号」+ 6/6 族 ——
    这张合格证是**替 B 签的**（B 什么都没有）。修后汇总必须是「未量到」，
    且逐只明细里 B 明写未量到。
    """
    points = (_fam(COAL, dict.fromkeys(_FAMILY_INDICATOR, 1.0))
              + [{"indicator": f"PE(TTM):{BANK}", "value": 6.0},
                 {"indicator": f"PB:{BANK}", "value": 0.7}])
    out = evaluate_compliance(points)

    assert out["compliance_multi_target"] is True, out
    assert out["compliance_level_calc"] == LEVEL_UNMEASURED, out
    assert FLAG_NO_SIGNAL not in out["compliance_flags"], out
    assert out["compliance_flags"] == [FLAG_UNMEASURED_MULTI], out
    assert out["compliance_measured"] is False, out
    by = {row["code"]: row for row in out["compliance_per_code"]}
    assert by[COAL]["level"] == "无" and by[COAL]["measured"] is True
    assert by[BANK]["level"] == LEVEL_UNMEASURED and by[BANK]["measured"] is False


def test_events_are_counted_once_and_never_fanned_out() -> None:
    """★ 事件归属判不出来（A06 契约无代码字段）⇒ 只在汇总层计入，不摊派。

    两个方向都要守：**不许丢**（它可能是一条严重的合规信号）、
    **不许摊派**（摊派就是又一次张冠李戴）。
    """
    points = _fam(COAL, dict.fromkeys(_FAMILY_INDICATOR, 1.0)) + _fam(
        BANK, dict.fromkeys(_FAMILY_INDICATOR, 1.0))
    out = evaluate_compliance(points, _events())

    assert out["compliance_event_flags"] == 1, out
    assert out["compliance_unattributed_event_flags"] == 1, out
    assert out["compliance_level_calc"] == "高", out
    assert sum(1 for f in out["compliance_flags"] if "立案" in f) == 1, out
    for row in out["compliance_per_code"]:
        assert row["event_flags"] == 0, row


def test_unlabeled_family_point_is_kept_separate() -> None:
    """★ 带族关键词却没有代码后缀的点：**不丢**、也**不摊派**给某一只票。"""
    points = (_fam(COAL, {"商誉": 1.0}) + _fam(BANK, {"商誉": 1.0})
              + _fam("", {"质押": 0.0}))
    out = evaluate_compliance(points)
    rows = {row["code"]: row for row in out["compliance_per_code"]}
    assert set(rows) == {COAL, BANK, ""}, rows
    assert rows[""]["label"] == UNLABELED_TARGET
    assert rows[""]["families_measured"] == ["质押"]
    # 汇总取交集 ⇒ 三只（含未标注）都量到的族为空
    assert out["compliance_families_measured"] == [], out


def test_no_code_at_all_is_not_multi_target() -> None:
    """★ 已知边界：一个代码都认不出来 ⇒ 只可能是单标的原样口径（不会更糟）。

    "多标的但一个后缀都没有"这种输入在本仓库里**不存在**：5 个比率族全在
    `supervisor._CODE_SUFFIX_INDICATORS` 里（采集侧必然带后缀）。
    这条判据把边界写下来：认领不到代码时**退化到原样口径**，
    而不是把两只票的数拼成一个等级（拼接只发生在按代码分组之后）。
    """
    out = evaluate_compliance(_fam("", {"商誉": 1.0, "质押": 0.0}))
    assert out["compliance_multi_target"] is False
    assert out["compliance_codes"] == []
    assert len(out["compliance_per_code"]) == 1
    assert out["compliance_per_code"][0]["label"] == UNLABELED_TARGET


# ===========================================================================
# ③ Agent 层：多标的**不许**走那张"单数口径"的合格证
# ===========================================================================


def test_calc_carries_per_code_dimension_through_enrich() -> None:
    """★ 逐只结果必须随 `AgentOutput.result` 下发（界面/审计的唯一诚实表达方式）。"""
    gw = FakeGateway(REPLY)
    out = asyncio.run(ComplianceAnalysisAgent(gw).execute(_make_input({
        "focus": f"{COAL}、{BANK}",
        "data_points": _fam(COAL, {"质押": 85.0}) + _fam(BANK, {"商誉": 1.0}),
        "events": [],
    })))

    assert out.result["compliance_multi_target"] is True, out.result
    assert out.result["compliance_codes"] == [BANK, COAL], out.result
    rows = out.result["compliance_per_code"]
    assert {row["code"] for row in rows} == {COAL, BANK}, rows
    # ★ 汇总族清单是交集（不是并集）——这正是那张"6/6"假合格证的入口
    assert out.result["compliance_families_measured"] == [], out.result


def test_multi_target_never_skips_the_llm() -> None:
    """★★★ 多标的**一律不跳 LLM**：纯规则路径的文案是单数口径的。

    修复前：两只票的并集"看起来齐全"（甚至报 6/6 族、level=无）⇒ 走纯规则、
    不调 LLM ⇒ 一张无归属的合格证直接出到用户面前，链路**没有任何环节报错**。
    """
    gw = FakeGateway(REPLY)
    out = asyncio.run(ComplianceAnalysisAgent(gw).execute(_make_input({
        "focus": f"{COAL}、{BANK}",
        "data_points": _fam(COAL, dict.fromkeys(_FAMILY_INDICATOR, 1.0))
        + _fam(BANK, dict.fromkeys(_FAMILY_INDICATOR, 1.0)),
        "events": [],
    })))

    assert len(gw.calls) == 1, "多标的走了纯规则路径（跳过 LLM）—— 合格证从这里漏出去"
    assert out.result.get("_rule_only") is not True, out.result
    assert out.result["model_used"] != "rule-only", out.result


def test_single_target_still_skips_the_llm_when_measured_clean() -> None:
    """★ 反向护栏：单标的"量到了、都没超阈值"仍然走纯规则（省 18.5k tokens/轮）。"""
    gw = FakeGateway(REPLY)
    out = asyncio.run(ComplianceAnalysisAgent(gw).execute(_make_input({
        "focus": COAL,
        "data_points": _fam(COAL, dict.fromkeys(_FAMILY_INDICATOR, 1.0)),
        "events": [],
    })))
    assert gw.calls == [], "单标的的量到且干净不该调 LLM"
    assert out.result["_rule_only_reason"] == "measured_clean", out.result
    assert out.result["compliance_multi_target"] is False, out.result


def test_multi_target_requirements_demand_per_code() -> None:
    """★ prompt 必须**明令逐只**，并点明"汇总值是取最坏、不是每只票的读数"。

    原话「LLM结论须与 `compliance_level_calc` 自洽」单独存在时**是危险的**：
    多标的的汇总值是"最坏的那只票"，模型照它写就等于把 A 的处境扣到 B 头上。
    """
    agent = ComplianceAnalysisAgent(FakeGateway(REPLY))
    payload = agent._parse_payload({  # noqa: SLF001
        "focus": f"{COAL}、{BANK}",
        "data_points": _fam(COAL, {"质押": 85.0}) + _fam(BANK, {"商誉": 1.0}),
        "events": [],
    })
    agent._prepare(payload)  # noqa: SLF001
    req = agent._requirements(payload)  # noqa: SLF001

    assert "多标的" in req and "逐只" in req, req
    assert COAL in req and BANK in req, req
    assert "汇总" in req and "compliance_by_code" in req, req
    # 既有的单标的契约不许被删（模型仍要在这四个取值里选）
    assert "高|中|无|未量到" in req, req


def test_single_target_requirements_are_unchanged() -> None:
    """★★ 单标的的 `_requirements()` 输出**逐字不变**（prompt 契约没被动过）。

    期望串按修复前的 f-string 逐字写出来：多标的段落是**加**上去的，
    单标的时它必须恰好展开成空串（连一个空格都不许多）。
    两条分支都覆盖：量到且干净（无缺口段）与未量到（带缺口段）。
    """
    cases = [
        # (数据点, 期望的 requirements 原文)
        (_fam(COAL, {"商誉": 1.0}), (
            "本地规则爆雷等级为「无」，LLM结论须与此自洽。\n"
            "输出JSON：\n"
            '- "conclusion": 合规与爆雷可能性评估，120字内，须引用具体旗标/事件\n'
            '- "confidence": high|medium|low\n'
            '- "compliance_level": 高|中|无|未量到（须与本地规则一致）\n'
            '- "burst_risk": 爆雷路径简述（质押平仓/商誉减值/立案处罚；无则填"未见明确爆雷路径"）\n'
            '- "red_flags": 风险明细数组（与本地旗标呼应）\n'
            '- "key_points": 2-4条'
        )),
        ([_dp("PE(TTM):601088", 6.2)], (
            "本地规则爆雷等级为「未量到」，LLM结论须与此自洽。\n"
            "\n★★ 本地规则本次**未获得输入**（未量到的族："
            "关联交易/商誉/担保/有息负债/货币资金/质押）。"
            "你**不得**输出「未见合规风险」「无风险」这类结论；"
            "必须把这是**数据缺口**写明，并把 compliance_level 填 `未量到`。\n"
            "输出JSON：\n"
            '- "conclusion": 合规与爆雷可能性评估，120字内，须引用具体旗标/事件\n'
            '- "confidence": high|medium|low\n'
            '- "compliance_level": 高|中|无|未量到（须与本地规则一致）\n'
            '- "burst_risk": 爆雷路径简述（质押平仓/商誉减值/立案处罚；无则填"未见明确爆雷路径"）\n'
            '- "red_flags": 风险明细数组（与本地旗标呼应）\n'
            '- "key_points": 2-4条'
        )),
    ]
    for points, expected in cases:
        agent = ComplianceAnalysisAgent(FakeGateway(REPLY))
        payload = agent._parse_payload(  # noqa: SLF001
            {"focus": COAL, "data_points": points, "events": []})
        agent._prepare(payload)  # noqa: SLF001
        assert agent._requirements(payload) == expected  # noqa: SLF001


def test_rule_only_multi_guard_never_signs_a_certificate() -> None:
    """★ 兜底分支：即使有人绕过 `_should_skip_llm`，也不许输出统一等级。

    这条分支正常到不了（多标的在 `_should_skip_llm` 就被拦下），
    但"到不了"与"到了也安全"是两件事 —— 它测的是后者。
    """
    agent = ComplianceAnalysisAgent(FakeGateway(REPLY))
    payload = agent._parse_payload({  # noqa: SLF001
        "focus": f"{COAL}、{BANK}",
        "data_points": _fam(COAL, dict.fromkeys(_FAMILY_INDICATOR, 1.0))
        + _fam(BANK, dict.fromkeys(_FAMILY_INDICATOR, 1.0)),
        "events": [],
    })
    agent._prepare(payload)  # noqa: SLF001
    calc = payload.hint["compliance_calc"]
    assert calc["compliance_multi_target"] is True

    guarded = agent._build_rule_only_result(payload)  # noqa: SLF001
    assert guarded["_rule_only_reason"] == "multi_target_guard", guarded
    assert guarded["compliance_level"] == LEVEL_UNMEASURED, guarded
    assert FLAG_NO_SIGNAL not in guarded["conclusion"], guarded
    assert COAL in guarded["conclusion"] and BANK in guarded["conclusion"], guarded


# ===========================================================================
# ④ 回归护栏：单标的与修复前**逐字一致**（字面量取自修复前实测输出）
# ===========================================================================

#: 修复前实测的 7 个字段（逐条来自 `evaluate_compliance` 的修复前实现；
#: 这些字面量就是"单标的路径没被动过"的判据 —— 改坏了必然对不上）。
_SINGLE_TARGET_EXPECTED: dict[str, tuple[list[dict], list[dict], dict]] = {
    "空输入": ([], [], {
        "compliance_flags": [FLAG_UNMEASURED], "severe_flag_count": 0,
        "compliance_level_calc": LEVEL_UNMEASURED, "compliance_measured": False,
        "compliance_families_measured": [],
        "compliance_families_unmeasured": sorted(logic._RULE_FAMILIES),  # noqa: SLF001
        "compliance_event_flags": 0}),
    "只有PE": ([_dp("PE", 18.0)], [], {
        "compliance_flags": [FLAG_UNMEASURED], "severe_flag_count": 0,
        "compliance_level_calc": LEVEL_UNMEASURED, "compliance_measured": False,
        "compliance_families_measured": [],
        "compliance_families_unmeasured": sorted(logic._RULE_FAMILIES),  # noqa: SLF001
        "compliance_event_flags": 0}),
    "六族全低值(单代码)": (_fam(COAL, dict.fromkeys(_FAMILY_INDICATOR, 1.0)), [], {
        "compliance_flags": [FLAG_NO_SIGNAL], "severe_flag_count": 0,
        "compliance_level_calc": "无", "compliance_measured": True,
        "compliance_families_measured": sorted(_FAMILY_INDICATOR),
        "compliance_families_unmeasured": [], "compliance_event_flags": 0}),
    "质押85": (_fam(COAL, {"质押": 85.0}), [], {
        "compliance_flags": ["大股东股权质押比例85%超80%，平仓与控制权变更高危"],
        "severe_flag_count": 1, "compliance_level_calc": "高",
        "compliance_measured": True, "compliance_families_measured": ["质押"],
        "compliance_families_unmeasured": ["关联交易", "商誉", "担保", "有息负债", "货币资金"],
        "compliance_event_flags": 0}),
    "存贷双高(同代码)": (_fam(COAL, {"货币资金": 45.0, "有息负债": 42.0}), [], {
        "compliance_flags": [
            "存贷双高：货币资金/总资产45%且有息负债/总资产42%同时畸高，账面资金真实性存疑"],
        "severe_flag_count": 1, "compliance_level_calc": "高",
        "compliance_measured": True,
        "compliance_families_measured": ["有息负债", "货币资金"],
        "compliance_families_unmeasured": ["关联交易", "商誉", "担保", "质押"],
        "compliance_event_flags": 0}),
    "三条普通旗标": ([_dp("关联交易占比", 35.0), _dp("商誉占净资产", 40.0),
                      _dp("大股东质押比例", 55.0)], [], {
        "compliance_flags": [
            "关联交易占比35%超30%，存在利益输送嫌疑",
            "商誉/净资产40%超30%，存在商誉减值爆雷隐患",
            "大股东股权质押比例55%超50%，存在平仓与控制权风险"],
        "severe_flag_count": 0, "compliance_level_calc": "高",
        "compliance_measured": True,
        "compliance_families_measured": ["关联交易", "商誉", "质押"],
        "compliance_families_unmeasured": ["担保", "有息负债", "货币资金"],
        "compliance_event_flags": 0}),
    "旗标+事件": (_fam(COAL, {"质押": 85.0}), _events(), {
        "compliance_flags": ["大股东股权质押比例85%超80%，平仓与控制权变更高危",
                             "涉诉/监管事件：公司被证监会立案调查"],
        "severe_flag_count": 2, "compliance_level_calc": "高",
        "compliance_measured": True, "compliance_families_measured": ["质押"],
        "compliance_families_unmeasured": ["关联交易", "商誉", "担保", "有息负债", "货币资金"],
        "compliance_event_flags": 1}),
    "混合后缀(单代码)": (_fam(COAL, {"商誉": 0.5}) + _fam("", {"质押": 0.0}), [], {
        "compliance_flags": [FLAG_NO_SIGNAL], "severe_flag_count": 0,
        "compliance_level_calc": "无", "compliance_measured": True,
        "compliance_families_measured": ["商誉", "质押"],
        "compliance_families_unmeasured": ["关联交易", "担保", "有息负债", "货币资金"],
        "compliance_event_flags": 0}),
}


def test_single_target_legacy_fields_are_byte_identical() -> None:
    """★★★ 回归护栏：单标的（0/1 个代码）的 7 个既有字段**逐字不变**。

    为什么这条必须有：这次改动是"按代码分组"，而分组判据依赖"6 位数字后缀 =
    代码"这个假设。假设一旦过宽（幽灵代码）或过窄（认不出后缀），
    单标的路径就会被误改 —— 而它是**绝大多数真实请求**走的那条路。
    """
    for name, (points, events, expected) in _SINGLE_TARGET_EXPECTED.items():
        out = evaluate_compliance(points, events)
        assert out["compliance_multi_target"] is False, (name, out)
        assert len(out["compliance_per_code"]) == 1, (name, out)
        assert _legacy(out) == expected, f"{name}: {_legacy(out)} != {expected}"


def test_single_target_flags_have_no_owner_prefix() -> None:
    """★ 归属前缀只在多标的时加 —— 单标的加了就破坏逐字兼容。"""
    out = evaluate_compliance(_fam(COAL, {"质押": 85.0}))
    assert not any(f.startswith("[") for f in out["compliance_flags"]), out
    assert out["compliance_per_code"][0]["code"] == COAL
