"""A12合规爆雷Agent测试（规则引擎纯本地 + Agent层FakeGateway不联网）。"""

import json

from src.core.models import AgentInput
from src.domain.agents.analysis import ComplianceAnalysisAgent
from src.domain.agents.analysis.compliance.logic import evaluate_compliance
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

def test_no_signals_level_none():
    r = evaluate_compliance([_dp("PE", 18.0)])
    assert r["compliance_level_calc"] == "无"
    assert r["severe_flag_count"] == 0
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
    events = [{"event_type": "product", "direction": "positive",
               "evidence_quote": "发布新品", "confidence": 0.9}]
    r = evaluate_compliance([], events=events)
    assert r["compliance_level_calc"] == "无"


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
    gw = FakeGateway(REPLY)
    out = await ComplianceAnalysisAgent(gw).execute(_make_input(
        {"data_points": [], "events": []}
    ))
    assert out.confidence.value == "low"
    assert gw.calls == []


async def test_agent_calc_fields_attached_even_clean():
    gw = FakeGateway({**REPLY, "conclusion": "未见明显合规风险",
                      "compliance_level": "无", "burst_risk": "未见明确爆雷路径",
                      "red_flags": []})
    out = await ComplianceAnalysisAgent(gw).execute(_make_input({
        "data_points": [_dp("PE", 12.0)],
    }))
    assert out.result["compliance_level_calc"] == "无"
    assert out.result["compliance_flags_calc"] == ["未见明显合规风险信号"]


def test_capabilities_surface():
    caps = ComplianceAnalysisAgent(FakeGateway(REPLY)).get_capabilities()
    assert "compliance_risk" in caps["capabilities"]
    assert "litigation_monitoring" in caps["capabilities"]


# ---------- 路由 ----------

def test_stock_plan_includes_compliance():
    plan = plan_run("stock", "600001")
    assert "A12_compliance" in plan["agents"]
    assert plan["indicators"] == ["stock_close:600001"]


def test_full_plan_includes_compliance():
    assert "A12_compliance" in plan_run("full", "")["agents"]


def test_macro_plan_excludes_compliance():
    assert "A12_compliance" not in plan_run("macro", "")["agents"]
