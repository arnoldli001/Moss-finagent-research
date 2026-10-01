"""A12合规爆雷Agent测试（规则引擎纯本地 + Agent层FakeGateway不联网）。"""

import json

from src.core.models import AgentInput
from src.domain.agents.analysis import ComplianceAnalysisAgent
from src.domain.agents.analysis.compliance.logic import (
    FLAG_UNMEASURED,
    evaluate_compliance,
)
from src.orchestration.supervisor import plan_run


class FakeGateway:
    def __init__(self, reply: dict) -> None:
        self._reply = reply
        self.calls: list[dict] = []

    async def complete(self, task_tier, system, prompt, **kwargs):
        self.calls.append(
            {"task_tier": task_tier, "system": system, "prompt": prompt, **kwargs}
        )
        from src.infrastructure.llm.models import LLMResponse

        return LLMResponse(
            content=json.dumps(self._reply, ensure_ascii=False),
            model_used="fake-model", provider="fake",
            prompt_hash="ph", response_hash="rh",
        )


def _dp(indicator: str, value: float) -> dict:
    return {"indicator": indicator, "value": value, "period_date": "2026-06",
            "source_name": "年报", "data_id": f"d_{indicator}"}


def _litigation(quote: str, confidence: float = 0.9) -> dict:
    return {"item_id": "info_1", "event_type": "litigation", "direction": "negative",
            "subject": "某公司", "evidence_quote": quote, "confidence": confidence}


def _make_input(payload: dict) -> AgentInput:
    return AgentInput(task_id="t12", tenant_id="tenant_001", payload=payload)


REPLY = {
    "conclusion": "大股东高质押叠加立案调查，爆雷风险高",
    "confidence": "high", "compliance_level": "高",
    "burst_risk": "质押平仓/立案处罚",
    "red_flags": ["大股东质押85%", "被证监会立案调查"],
    "key_points": ["质押高危", "监管立案"],
}


# ---------- 规则引擎 ----------

def test_no_rule_input_is_unmeasured_not_none():
    """★ 2026-09-29 语义修正：一个规则族都没量到 → 「未量到」，**不是**「无」。

    旧断言是 `compliance_level_calc == "无"` +
    `compliance_flags == ["未见明显合规风险信号"]` —— 那是把「什么都不知道」
    渲染成一张体检合格证。而实测当时 6 个族**一个生产者都没有**，
    也就是线上一直走的就是这支：`PE` 这种非合规指标**不构成合规判据的输入**。

    新的正反面分别由本函数与 `test_measured_clean_still_reports_none` 覆盖。
    """
    r = evaluate_compliance([_dp("PE", 18.0)])
    assert r["compliance_level_calc"] == "未量到"
    assert r["severe_flag_count"] == 0
    assert r["compliance_measured"] is False
    assert "未见明显合规风险信号" not in r["compliance_flags"]
    assert r["compliance_flags"] == [FLAG_UNMEASURED]


def test_measured_clean_still_reports_none():
    """★ 反面：真的量到了、值没超阈值 → 仍然报「无」（别把假绿换成假红）。"""
    r = evaluate_compliance([_dp("商誉占净资产", 1.0)])
    assert r["compliance_level_calc"] == "无"
    assert r["compliance_measured"] is True
    assert r["compliance_flags"] == ["未见明显合规风险信号"]


def test_related_party_moderate():
    r = evaluate_compliance([_dp("关联交易占营收比", 35.0)])
    assert r["compliance_level_calc"] == "中"
    assert len(r["compliance_flags"]) == 1
    assert "关联交易" in r["compliance_flags"][0]


def test_pledge_severe_high_level():
    r = evaluate_compliance([_dp("大股东质押比例", 85.0)])
    assert r["compliance_level_calc"] == "高"
    assert r["severe_flag_count"] == 1
    assert "80%" in r["compliance_flags"][0]


def test_pledge_moderate_not_severe():
    r = evaluate_compliance([_dp("大股东质押比例", 55.0)])
    assert r["compliance_level_calc"] == "中"
    assert r["severe_flag_count"] == 0
    assert "50%" in r["compliance_flags"][0]


def test_pledge_threshold_dedup_single_flag():
    """85%同时越过50%与80%两档，只产一条（取最高档）。"""
    r = evaluate_compliance([_dp("大股东质押比例", 85.0)])
    assert len([f for f in r["compliance_flags"] if "质押" in f]) == 1


def test_double_high_severe():
    r = evaluate_compliance([_dp("货币资金占总资产", 45.0), _dp("有息负债占总资产", 42.0)])
    assert r["compliance_level_calc"] == "高"
    assert "存贷双高" in r["compliance_flags"][0]


def test_double_high_not_triggered_when_only_one_side():
    r = evaluate_compliance([_dp("货币资金占总资产", 45.0), _dp("有息负债占总资产", 10.0)])
    assert r["compliance_level_calc"] == "无"
    assert not any("存贷双高" in f for f in r["compliance_flags"])


def test_litigation_investigation_event_severe():
    r = evaluate_compliance([], events=[_litigation("公司被证监会立案调查")])
    assert r["compliance_level_calc"] == "高"
    assert r["severe_flag_count"] == 1
    assert "立案调查" in r["compliance_flags"][0]


def test_ordinary_litigation_moderate():
    r = evaluate_compliance([], events=[_litigation("涉及合同纠纷诉讼", confidence=0.5)])
    assert r["compliance_level_calc"] == "中"
    assert r["severe_flag_count"] == 0


def test_non_litigation_events_ignored():
    """非诉讼/监管事件对合规判定没有信息量 → 等级是「未量到」，不是「无」。

    旧断言写的是「无」：一条"发布新品/正面"事件既不是合规旗标，
    也不构成合规判据的输入 —— 把它算成输入会让合格证从新地方长出来。
    """
    events = [{"event_type": "product", "direction": "positive",
               "evidence_quote": "发布新品", "confidence": 0.9}]
    r = evaluate_compliance([], events=events)
    assert r["compliance_level_calc"] == "未量到"
    assert r["compliance_measured"] is False
    assert r["compliance_flags"] == [FLAG_UNMEASURED]


def test_three_normal_flags_escalate_to_high():
    r = evaluate_compliance([
        _dp("关联交易占比", 35.0),
        _dp("商誉占净资产", 40.0),
        _dp("大股东质押比例", 55.0),
    ])
    assert r["compliance_level_calc"] == "高"
    assert r["severe_flag_count"] == 0  # 无严重旗标，但数量达3条升级


def test_guarantee_over_100_severe():
    r = evaluate_compliance([_dp("对外担保占净资产", 120.0)])
    assert r["compliance_level_calc"] == "高"
    assert len([f for f in r["compliance_flags"] if "担保" in f]) == 1


# ---------- Agent层 ----------

async def test_agent_happy_path_with_events():
    gw = FakeGateway(REPLY)
    out = await ComplianceAnalysisAgent(gw).execute(_make_input({
        "focus": "600001",
        "data_points": [_dp("大股东质押比例", 85.0)],
        "events": [_litigation("公司被证监会立案调查")],
    }))

    assert out.agent_id == "A12_compliance"
    assert out.confidence.value == "high"
    assert out.result["compliance_level"] == "高"
    assert out.result["compliance_level_calc"] == "高"
    assert out.result["severe_flag_count"] == 2  # 质押严重旗标 + 立案事件
    prompt = gw.calls[0]["prompt"]
    assert "立案调查" in prompt        # 信息层事件注入
    assert "80%" in prompt             # 本地旗标注入
    assert "信息层事件" in prompt
    assert gw.calls[0]["task_tier"] == "reasoning"


async def test_agent_empty_data_skips_llm():
    """★ 2026-09-27 第八轮：A12 在「无合规风险 + 无事件」时走纯规则路径（不调 LLM）。

    行为变更：
      · 旧实现：data_points=[] → 走"输入数据点与事件均为空"分支 → confidence=low
      · 新实现：data_points=[] + events=[] → 规则给出「未见明显合规风险信号」
                 → 走纯规则路径（不调 LLM，节省 18,500 tokens/轮）
      · 旧测试期望 confidence=low + gw.calls == []，后者仍正确，但前者变 confidence=high
    """
    gw = FakeGateway(REPLY)
    out = await ComplianceAnalysisAgent(gw).execute(_make_input(
        {"data_points": [], "events": []}
    ))
    assert gw.calls == []  # 关键回归：纯规则路径不调 LLM
    assert out.result["model_used"] == "rule-only"  # 标记来自规则


async def test_agent_calc_fields_attached_even_unmeasured():
    """★ 2026-09-29 语义修正：规则**未获得输入**时走纯规则路径，但输出是「未量到」。

    旧测试名 `..._even_clean`、断言 `compliance_level_calc == "无"` +
    `compliance_flags_calc == ["未见明显合规风险信号"]`。而 `PE` 不是合规判据的输入，
    所以那一版实际上在断言"用 PE 推出没有合规风险" —— 一张伪造的合格证。
    仍不调 LLM（两条分支都省 18,500 tokens/轮），但结果与溯源字段必须分开。
    """
    gw = FakeGateway(REPLY)
    out = await ComplianceAnalysisAgent(gw).execute(_make_input({
        "data_points": [_dp("PE", 12.0)],  # 非合规指标 → 规则未获得输入
    }))
    assert gw.calls == []  # ★ 纯规则不调 LLM
    assert out.result["compliance_level_calc"] == "未量到"
    assert out.result["compliance_flags_calc"] == [FLAG_UNMEASURED]
    assert out.result["compliance_measured"] is False
    assert out.result["_rule_only_reason"] == "no_input"
    assert out.result["model_used"] == "rule-only"


def test_capabilities_surface():
    caps = ComplianceAnalysisAgent(FakeGateway(REPLY)).get_capabilities()
    assert "compliance_risk" in caps["capabilities"]
    assert "litigation_monitoring" in caps["capabilities"]


# ---------- 路由 ----------

def test_stock_plan_includes_compliance():
    plan = plan_run("stock", "600001")
    assert "A12_compliance" in plan["agents"]
    # 个股契约：行情+估值(PE/PB)+财务排雷比率（A10/A11数据缺口补齐）+大盘流动性7指标
    assert plan["indicators"] == [
        "stock_close:600001",
        "PE(TTM):600001",
        "PB:600001",
        "资产负债率:600001",
        "流动比率:600001",
        "mkt:turnover:total",
        "mkt:turnover:hist",
        "mkt:turnover_rate:all_a",
        "mkt:margin_balance",
        "mkt:margin_balance:hist",
        "mkt:north_flow",
        "idx_val:snapshot:all",
        "mkt:cybkcb:turnover:all",
        "mkt:cybkcb:val:all",
        "mkt:cybkcb:spot_summary",
    ]


def test_full_plan_includes_compliance():
    assert "A12_compliance" in plan_run("full", "")["agents"]


def test_macro_plan_excludes_compliance():
    assert "A12_compliance" not in plan_run("macro", "")["agents"]
