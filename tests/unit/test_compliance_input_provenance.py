"""★★★ 「来源未量到」不许伪装成「量到 0」—— A12 合规判定的溯源护栏。

## 这个文件守的是哪一件事

用户能看见的唯一一句话是 A12 的 `compliance_level`。它当时**恒为「无」**、
旗标恒为 `["未见明显合规风险信号"]`、`confidence` 恒为 `"high"`，
而且因为 `level == "无"` 会走纯规则路径**跳过 LLM**，整条链路**没有一个环节报错**。

根因不是阈值写错了，是**两种相反的处境共用一个返回值**：

    6 个族都量到了、值都没超阈值   →  「无」（真的没发现风险）
    6 个族一条都没量到             →  「无」（什么都不知道）   ← 伪造的合格证

`tests/unit/test_contract_consistency.py::_KNOWN_UNPRODUCED_RULE_FAMILIES` 记录了
当时的事实：`关联交易`/`商誉`/`质押`/`担保`/`货币资金`/`有息负债`
**在采集侧一个生产者都没有** —— 也就是说当时走的**正是第二行**。

本项目 2026-09-28 的原话裁定：「**「没量到」与「量到 0」必须分开显示**；
读不到数据时显示"未量到/无法统计"，绝不用 0 糊过去（宁可不显示，也不显示假绿）。」
这个文件就是那条裁定的机器复现。

## 判据为什么这样写（而不是断言某个字符串）

- 断言**关系**（`measured=False ⇒ 等级是未量到`、`0.0 ⇒ measured=True`），
  不是断言"文案长这样"—— 文案会被"优化得更友好"，
  而 `test_prompt_table_quotes_are_real` 那类逐字断言会因此变成维护负担。
- **假绿的反面也要测**：真的量过、都没超阈值时，必须**仍然**能报「无」，
  否则修完这一条就会把正常的"没发现风险"也变成"未量到"，
  那是把一个假绿换成一个假红。
- **单一事实源**：`logic._RULE_FAMILIES` 与
  `test_contract_consistency._consumed_rule_families()`（AST 从消费侧派生）
  必须对得上 —— 两份清单漂移时，本文件与那个文件会同时红。
"""

from __future__ import annotations

import pytest

from src.core.models import AgentInput
from src.domain.agents.analysis import ComplianceAnalysisAgent
from src.domain.agents.analysis.compliance import logic
from src.domain.agents.analysis.compliance.logic import (
    FLAG_NO_SIGNAL,
    FLAG_UNMEASURED,
    LEVEL_UNMEASURED,
    evaluate_compliance,
)

#: 6 个规则族（从被测模块现读，不重抄）
_FAMILIES = logic._RULE_FAMILIES  # noqa: SLF001

#: 每个族一个**能触发取值**的样本指标名（含该族关键词即可，`_find_value` 是子串匹配）
_SAMPLE_INDICATOR = {
    "关联交易": "关联交易占营收比",
    "商誉": "商誉占净资产比",
    "质押": "大股东质押比例",
    "担保": "对外担保占净资产比",
    "货币资金": "货币资金占总资产比",
    "有息负债": "有息负债占总资产比",
}


class _FakeGateway:
    """只记账、不回话 —— 本文件的重点是「有没有调它」与「输出了什么」。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def complete(self, task_tier, system, prompt, **kwargs):  # pragma: no cover
        self.calls.append({"task_tier": task_tier, "prompt": prompt})
        raise AssertionError(
            "本用例不该调用 LLM：合规规则未获得输入/无需 LLM 时必须走纯规则路径"
        )


def _dp(indicator: str, value: float) -> dict:
    return {"indicator": indicator, "value": value, "period_date": "2026-06",
            "source_name": "年报", "data_id": f"d_{indicator}"}


# ============================================================
# ① 枚举本身可信（否则下面全部空跑 = 假绿）
# ============================================================


def test_family_table_is_non_trivial():
    """★ 自证：族清单非空、无重复，且与 `_RATIO_RULES` 同源。

    没有这条，`_FAMILIES` 一旦被改成空元组，下面所有参数化断言都会
    **空跑通过**（vacuously true）—— 本项目把头号失败模式定为假绿。
    """
    assert len(_FAMILIES) == 6, f"规则族应当是 6 个，实际 {_FAMILIES}"
    assert len(set(_FAMILIES)) == len(_FAMILIES), f"族清单有重复：{_FAMILIES}"
    assert tuple(kw for kw, _bands in logic._RATIO_RULES) == logic._RATIO_FAMILIES  # noqa: SLF001
    assert set(_SAMPLE_INDICATOR) == set(_FAMILIES), (
        "样本表与族清单不一致 —— 新增一个族就必须在这里补一个样本指标名"
    )


def test_rule_families_cover_every_consumed_keyword():
    """★ 单一事实源：`logic` 的族清单 == 从消费侧 AST 派生出来的关键词集。

    `test_contract_consistency.py::_consumed_rule_families()` 用 **AST** 扫
    `_RATIO_RULES` 的键与 `_find_value(data_points, "X")` 的字面量。
    那才是"消费侧到底消费了哪几族"的权威来源；`logic._RULE_FAMILIES`
    是同一件事的第二份表述 —— 两份漂移时本断言红。

    为什么值得单列：本项目实测过「同一个 key 写在 N 处，必有一处被漏改」，
    后果是情报 5 个端点对所有人 403。这里的后果会轻一些但同源：
    新增一个族却忘了进 `_RULE_FAMILIES` → 该族"没量到"**不会**被计入缺口，
    「未量到」判定漏一层，合格证又从新地方长出来。
    """
    from tests.unit.test_contract_consistency import _consumed_rule_families

    consumed = set(_consumed_rule_families())
    assert consumed, "消费侧一个关键词都没派生出 —— AST 判据坏了"
    missing_from_logic = sorted(consumed - set(_FAMILIES))
    missing_from_scan = sorted(set(_FAMILIES) - consumed)
    assert not missing_from_logic, (
        "消费侧在用的族没进 `logic._RULE_FAMILIES`"
        f"（它的「未量到」判定会漏掉这些族）：{missing_from_logic}"
    )
    assert not missing_from_scan, (
        "`logic._RULE_FAMILIES` 里有消费侧已不再消费的族："
        f"{missing_from_scan}\n→ 规则改了就要同步（否则永远报「未量到」）。"
    )


# ============================================================
# ② 核心：没有输入 ≠ 无风险
# ============================================================


def test_no_input_is_unmeasured_not_none():
    """★★★ 用户报障的那一行：数据点里没有任何规则族 → **不许**说「无风险」。

    旧实现返回 `compliance_level_calc == "无"` +
    `compliance_flags == ["未见明显合规风险信号"]`。
    本断言把这张伪造的合格证钉死在两个方向上：
      · 等级不是「无」；
      · 旗标里没有「未见明显合规风险信号」这句话。
    """
    r = evaluate_compliance([_dp("PE", 18.0)], events=[])

    assert r["compliance_level_calc"] == LEVEL_UNMEASURED, (
        f"无规则输入时等级应为「{LEVEL_UNMEASURED}」，实际 {r['compliance_level_calc']!r}"
    )
    assert FLAG_NO_SIGNAL not in r["compliance_flags"], (
        "无规则输入却输出了「未见明显合规风险信号」—— 这是伪造的体检合格证"
    )
    assert r["compliance_flags"] == [FLAG_UNMEASURED]
    assert r["compliance_measured"] is False
    assert r["severe_flag_count"] == 0


def test_completely_empty_input_is_unmeasured():
    """彻底空输入（无数据点、无事件）同样是「未量到」，不是「无」。"""
    r = evaluate_compliance([], events=[])
    assert r["compliance_level_calc"] == LEVEL_UNMEASURED
    assert r["compliance_measured"] is False
    assert sorted(r["compliance_families_unmeasured"]) == sorted(_FAMILIES)
    assert r["compliance_families_measured"] == []


def test_non_litigation_events_are_not_compliance_input():
    """★ 一条"正面产品事件"对合规判定**没有信息量**。

    旧实现只看 `level == "无" and not events`，而"事件列表非空但都是产品类"
    会让 `_event_flags()` 返回空 —— 如果这里判成"有输入"，
    等级又会从「未量到」漏回「无」，合格证从新地方长出来。
    """
    events = [{"event_type": "product", "direction": "positive",
               "evidence_quote": "发布新品", "confidence": 0.9}]
    r = evaluate_compliance([], events=events)
    assert r["compliance_level_calc"] == LEVEL_UNMEASURED
    assert r["compliance_measured"] is False
    assert r["compliance_event_flags"] == 0


def test_litigation_event_is_input_and_drives_level():
    """★ 反向：真的有一条诉讼/监管事件 → 就算没有任何比率族，也不是「未量到」。"""
    events = [{"item_id": "i", "event_type": "litigation", "direction": "negative",
               "subject": "某公司", "evidence_quote": "公司被证监会立案调查",
               "confidence": 0.9}]
    r = evaluate_compliance([], events=events)
    assert r["compliance_measured"] is True
    assert r["compliance_level_calc"] == "高"
    assert r["compliance_event_flags"] == 1


# ============================================================
# ③ 反面：真的量过、都没超阈值 → 仍然必须能报「无」
# ============================================================


@pytest.mark.parametrize("family", sorted(_FAMILIES))
def test_measured_low_value_still_reports_none(family: str):
    """★★ 每个族单独量到一个低值 → `measured=True`、等级「无」、旗标是那句占位。

    这一条防的是**把假绿换成假红**：修完①之后很容易顺手让所有
    "没有旗标"的情形都变成「未量到」，那等于把"真的没发现风险"
    也报成"数据缺口"，用户照样读不到真实信息。
    """
    r = evaluate_compliance([_dp(_SAMPLE_INDICATOR[family], 1.0)])
    assert r["compliance_measured"] is True
    assert r["compliance_level_calc"] == "无"
    assert r["compliance_flags"] == [FLAG_NO_SIGNAL]
    assert r["compliance_families_measured"] == [family]


@pytest.mark.parametrize("family", sorted(_FAMILIES))
def test_zero_is_a_measurement_not_a_gap(family: str):
    """★★★ 「量到 0」与「没量到」必须分开 —— 这就是用户裁定的那一条。

    质押比例 0% 是**有效读数**（真的没质押）；
    "上下文里没有质押这条指标"是**什么都不知道**。
    两者的差别只看 `is not None`，不看真假值。
    """
    r = evaluate_compliance([_dp(_SAMPLE_INDICATOR[family], 0.0)])
    assert r["compliance_measured"] is True, (
        f"{family} 的值是 0.0 —— 这是「量到 0」，不是「没量到」"
    )
    assert r["compliance_level_calc"] == "无"
    assert r["compliance_flags"] == [FLAG_NO_SIGNAL]
    assert family not in r["compliance_families_unmeasured"]


def test_four_levels_are_all_reachable():
    """★ 四个等级都必须**可达**（判据不许有死分支）。"""
    seen = {
        evaluate_compliance([_dp("大股东质押比例", 85.0)])["compliance_level_calc"],
        evaluate_compliance([_dp("关联交易占营收比", 35.0)])["compliance_level_calc"],
        evaluate_compliance([_dp("商誉占净资产比", 1.0)])["compliance_level_calc"],
        evaluate_compliance([])["compliance_level_calc"],
    }
    assert seen == {"高", "中", "无", LEVEL_UNMEASURED}, f"等级未全部可达：{seen}"


def test_measured_flag_agrees_with_placeholder():
    """★ 一致性：占位旗标与 `compliance_measured` **必须**互相蕴含。

    两个字段分开下发（一个给人看、一个给机器判），漂移时界面会显示
    "未量到"而审计字段说 `measured=True`，或者反过来。
    """
    cases = [
        evaluate_compliance([]),
        evaluate_compliance([_dp("PE", 12.0)]),
        evaluate_compliance([_dp("商誉占净资产比", 1.0)]),
        evaluate_compliance([_dp("大股东质押比例", 85.0)]),
    ]
    for r in cases:
        if r["compliance_measured"]:
            assert FLAG_UNMEASURED not in r["compliance_flags"], r
        else:
            assert r["compliance_flags"] == [FLAG_UNMEASURED], r
            assert r["compliance_level_calc"] == LEVEL_UNMEASURED, r


# ============================================================
# ④ Agent 面：那张合格证不许从纯规则路径漏出去
# ============================================================


def _make_input(payload: dict) -> AgentInput:
    return AgentInput(task_id="t12_prov", tenant_id="tenant_001", payload=payload)


async def test_agent_unmeasured_path_is_low_confidence_and_says_gap():
    """★★★ 端到端：`data_points` 里没有任何规则族时，A12 的输出必须自曝缺口。

    断言的是**用户看得见的那几个字段**（不是内部标记）：
      · `compliance_level` 是「未量到」；
      · `confidence` 不是 high；
      · `burst_risk` 不许写"未见明确爆雷路径"（那也是一句无据结论）；
      · 整份结果里**任何地方**都不许出现「未见明显合规风险信号」。
    """
    gw = _FakeGateway()
    out = await ComplianceAnalysisAgent(gw).execute(_make_input({
        "focus": "600036",
        "data_points": [_dp("PE(TTM):600036", 6.2)],
        "events": [],
    }))

    assert gw.calls == [], "未量到时不该调 LLM（它同样没有输入，只会给出无据的定性）"
    assert out.result["compliance_level"] == LEVEL_UNMEASURED
    assert out.result["compliance_level_calc"] == LEVEL_UNMEASURED
    assert out.result["confidence"] != "high"
    assert out.result["burst_risk"] != "未见明确爆雷路径"
    assert out.result["_rule_only_reason"] == "no_input"
    assert out.result["compliance_measured"] is False
    assert sorted(out.result["compliance_families_unmeasured"]) == sorted(_FAMILIES)
    assert "未获得输入" in out.result["conclusion"]

    blob = repr(out.result)
    assert FLAG_NO_SIGNAL not in blob, (
        "结果里仍然出现「未见明显合规风险信号」—— 伪造的合格证还在漏"
    )


async def test_agent_measured_clean_path_stays_high_confidence():
    """★ 反向：真的量到了、都没超阈值 → 仍然报「无」+ `measured_clean` + high。

    两条路径的**分支判据**必须不同（`_rule_only_reason` 是机器可读的那个），
    否则"走了纯规则"与"压根没跑"在审计里长得一样。
    """
    gw = _FakeGateway()
    out = await ComplianceAnalysisAgent(gw).execute(_make_input({
        "focus": "600036",
        "data_points": [_dp("商誉占净资产比", 0.5), _dp("大股东质押比例", 0.0)],
        "events": [],
    }))

    assert gw.calls == []
    assert out.result["compliance_level"] == "无"
    assert out.result["_rule_only_reason"] == "measured_clean"
    assert out.result["compliance_measured"] is True
    assert out.result["compliance_families_measured"] == ["商誉", "质押"]
    assert out.result["red_flags"] == []
    # ★ 这条用例量到了 2/6 族 —— 结论**必须**自带覆盖率（见下一条）
    assert out.result["confidence"] == "medium", (
        "只量到一部分族时不该给 high 置信度 —— 那是残缺的合格证"
    )


async def test_measured_clean_conclusion_discloses_coverage():
    """★★★ 「无」结论必须自带覆盖率，且要点名**哪些族没量到**。

    ## 为什么单列一条（这是 CHG-0073 那条纪律的更细一层）

    用户在前端看到的只有 `conclusion` 一行。实测只量到 **2/6** 族时，
    结论文本原本只写「未见明显合规风险信号」——覆盖率藏在 `key_points` 里，
    于是用户读成"这家公司合规方面查过了、没问题"。

    数字上没错，但它仍是**一张残缺的合格证**：
    六族全空时报「无」是伪造（已修）；两族量到就报「无」而不说清另外四族，
    只比前者好一点。

    ⚠️ 而且四种「没量到」的原因**各不相同**（口径不适用 / 该票不在专题表内 /
    无免费源 / 本次未取），**禁止合并成一句** —— 最后一种最危险，
    因为它长得跟前三种一模一样。
    """
    gw = _FakeGateway()
    out = await ComplianceAnalysisAgent(gw).execute(_make_input({
        "focus": "600036",
        "data_points": [_dp("商誉占净资产比", 0.74), _dp("有息负债占总资产比", 0.99)],
        "events": [],
    }))
    conclusion = out.result["conclusion"]
    assert "2/6" in conclusion, (
        f"「无」结论没有写明覆盖率（应为 2/6）：{conclusion!r}"
    )
    assert "未量到" in conclusion, (
        f"「无」结论没有点名未量到的族：{conclusion!r}"
    )
    assert "不等于" in conclusion, (
        f"「无」结论没有说清「未量到 ≠ 零风险」：{conclusion!r}"
    )
    # 未量到的四族必须逐个点名（不是只给个数）
    for fam in ("关联交易", "担保", "货币资金", "质押"):
        assert fam in conclusion, f"未量到的 `{fam}` 没有出现在结论里：{conclusion!r}"
    # 要点里要给出"原因不止一种"的提示，避免读者把四种处境当成一种
    assert any("各自原因不同" in kp for kp in out.result["key_points"]), (
        f"要点没有区分四种「没量到」的原因：{out.result['key_points']}"
    )


async def test_agent_requirements_forbid_clean_claim_when_unmeasured():
    """★ 走到 LLM 的那条路上，`_requirements()` 也必须把"未量到"这条禁令带上。

    ## 这条路径是**真实可达**的（不是为测试造的）

    `_should_skip_llm()` 只要 `payload.events` 非空就**不跳过** LLM —— 这是刻意的
    保守选择：事件里可能藏着 `_event_flags()` 认不出的合规信号（它只认
    `litigation` + `negative`）。但"事件列表非空"与"合规判据获得输入"**不是一回事**：
    一条"发布新品/正面"事件既不是旗标也不是输入，于是会出现

        payload.events 非空  →  调 LLM
        `_event_flags()` 为空 + 无比率族  →  规则等级 = 未量到

    两者同时成立。此时模型看到的仍是同一段 requirements；少了这句禁令，
    它会照着旧模板写「未见合规风险」—— 缺口从 LLM 那一侧再漏一次。

    同时断言「LLM 确实被调用了」：否则本用例会因为走纯规则路径而空跑通过。
    """
    events = [{"item_id": "i", "event_type": "product", "direction": "positive",
               "subject": "某公司", "evidence_quote": "发布新品", "confidence": 0.9}]
    gw = _FakeGateway()
    agent = ComplianceAnalysisAgent(gw)
    payload = agent._parse_payload(  # noqa: SLF001
        {"focus": "600036", "data_points": [], "events": events}
    )
    agent._prepare(payload)  # noqa: SLF001

    assert payload.hint["compliance_calc"]["compliance_level_calc"] == LEVEL_UNMEASURED
    assert agent._should_skip_llm(payload) is False, (  # noqa: SLF001
        "有事件时不跳过 LLM —— 否则本用例测不到 requirements"
    )

    req = agent._requirements(payload)  # noqa: SLF001
    assert "未量到" in req, "requirements 里没有「未量到」这个取值 —— 模型会照旧写「无」"
    assert "不得" in req and "数据缺口" in req
    assert "高|中|无|未量到" in req
