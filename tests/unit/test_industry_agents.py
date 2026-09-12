"""行业层A13-A16测试（本地景气信号 + FakeGateway + Supervisor行业路由）。"""

import json

from src.core.models import AgentInput
from src.domain.agents.industry import (
    ConsumerIndustryAgent,
    CyclicalIndustryAgent,
    PharmaIndustryAgent,
    TechIndustryAgent,
)
from src.domain.agents.industry.base import IndustryAgentBase
from src.orchestration.supervisor import INDUSTRY_AGENTS, plan_run, route_industry


class FakeGateway:
    def __init__(self, reply: dict) -> None:
        self._reply = reply
        self.calls: list[dict] = []

    async def complete(self, task_tier, system, prompt, **kwargs):
        self.calls.append({"task_tier": task_tier, "system": system,
                           "prompt": prompt, **kwargs})
        from src.infrastructure.llm.models import LLMResponse

        return LLMResponse(
            content=json.dumps(self._reply, ensure_ascii=False),
            model_used="fake", provider="fake", prompt_hash="ph", response_hash="rh",
        )


REPLY = {
    "conclusion": "行业景气向上", "confidence": "medium", "outlook": "向好",
    "cycle_position": "成长期", "drivers": ["需求扩张"], "risks": ["竞争加剧"],
}


def _dp(indicator: str, value: float, period: str) -> dict:
    return {"indicator": indicator, "value": value, "period_date": period,
            "source_name": "Wind", "data_id": f"d_{indicator}_{period}"}


def _input(agent_cls, points: list[dict], focus: str = "测试行业") -> AgentInput:
    return AgentInput(task_id="t_ind", tenant_id="tenant_001",
                      payload={"focus": focus, "data_points": points})


# ---------- 本地信号（基类纯函数） ----------

def test_trend_signal_up():
    pts = [_dp("半导体出货量同比", 10.0, "2026-06"),
           _dp("半导体出货量同比", 15.0, "2026-07")]
    sig = IndustryAgentBase._trend_signal(pts)
    assert "上行" in sig["trend"]
    assert "50.0%" in sig["detail"]
    assert "2026-06" in sig["detail"] and "2026-07" in sig["detail"]
    assert sig["per_indicator"]["半导体出货量同比"].startswith("环比上行")


def test_trend_signal_flat_and_down():
    pts = [_dp("PPI同比", -3.6, "2026-07"), _dp("PPI同比", -3.6, "2026-08")]
    assert "持平" in IndustryAgentBase._trend_signal(pts)["trend"]
    pts2 = [_dp("PPI同比", 5.0, "2026-07"), _dp("PPI同比", 4.0, "2026-08")]
    assert "下行" in IndustryAgentBase._trend_signal(pts2)["trend"]


def test_trend_signal_groups_per_indicator_no_cross_compare():
    """两个指标同期数据：禁止跨指标误比，各自成组后投票分化。"""
    pts = [
        _dp("动力煤价格", 700.0, "2026-07"), _dp("动力煤价格", 720.0, "2026-08"),
        _dp("煤炭库存", 2600.0, "2026-07"), _dp("煤炭库存", 2400.0, "2026-08"),
    ]
    sig = IndustryAgentBase._trend_signal(pts)
    assert "分化" in sig["trend"]
    assert "动力煤价格" in sig["detail"] and "煤炭库存" in sig["detail"]
    assert len(sig["per_indicator"]) == 2


def test_trend_signal_insufficient_data():
    sig = IndustryAgentBase._trend_signal([_dp("x", 1.0, "2026-08")])
    assert sig["trend"] == "数据不足"


# ---------- watch_keywords 行业过滤 ----------

async def test_tech_agent_watches_only_semiconductor_indicators():
    gw = FakeGateway(REPLY)
    out = await TechIndustryAgent(gw).execute(_input(TechIndustryAgent, [
        _dp("半导体出货量同比", 10.0, "2026-06"),
        _dp("半导体出货量同比", 15.0, "2026-07"),
        _dp("白酒批价", 950.0, "2026-07"),  # 不属于科技关注指标
    ], focus="半导体"))

    calc = out.result["industry_signal_calc"]
    assert calc["watched_indicator_count"] == 2
    assert "上行" in calc["trend"]
    assert "半导体出货量同比" in calc["detail"]
    assert "出货量" in gw.calls[0]["prompt"]


async def test_tech_pe_watermark_50():
    """仅PE点、无科技关注指标：跳过LLM，但本地估值旗标仍入calc。"""
    gw = FakeGateway(REPLY)
    out = await TechIndustryAgent(gw).execute(_input(TechIndustryAgent, [
        _dp("行业PE", 48.0, "2026-08"),
    ]))
    assert out.confidence.value == "low"
    assert out.result["skipped"] is True
    assert "常规区间" in out.result["industry_signal_calc"]["valuation"]
    assert gw.calls == []


async def test_cyclical_pe_watermark_20():
    """周期股PE警戒线20，同值48在周期行业判偏高（行业差异实证）。"""
    gw = FakeGateway(REPLY)
    out = await CyclicalIndustryAgent(gw).execute(_input(CyclicalIndustryAgent, [
        _dp("PPI同比", 5.0, "2026-07"), _dp("PPI同比", 6.0, "2026-08"),
        _dp("行业PE", 48.0, "2026-08"),
    ], focus="煤炭"))
    calc = out.result["industry_signal_calc"]
    assert calc["watched_indicator_count"] == 2  # 仅PPI，PE不在watch但估值旗标仍算
    assert "估值偏高" in calc["valuation"]
    assert "反身性" in gw.calls[0]["system"]  # 周期股prompt带估值反身性提醒


async def test_consumer_agent_happy_path():
    gw = FakeGateway({**REPLY, "cycle_position": "高端复苏"})
    out = await ConsumerIndustryAgent(gw).execute(_input(ConsumerIndustryAgent, [
        _dp("社零同比", 3.0, "2026-06"), _dp("社零同比", 4.2, "2026-07"),
    ], focus="白酒"))
    assert out.agent_id == "A14_consumer"
    assert out.result["outlook"] == "向好"
    calc = out.result["industry_signal_calc"]
    assert calc["industry"] == "消费"


async def test_pharma_agent_watches_policy_indicators():
    gw = FakeGateway(REPLY)
    out = await PharmaIndustryAgent(gw).execute(_input(PharmaIndustryAgent, [
        _dp("研发费用率", 12.0, "2025"),
        _dp("研发费用率", 14.0, "2026"),
    ], focus="创新药"))
    calc = out.result["industry_signal_calc"]
    assert calc["watched_indicator_count"] == 2
    assert out.agent_id == "A16_pharma"
    assert "集采" in gw.calls[0]["system"]


async def test_industry_agent_empty_data_skips_llm():
    gw = FakeGateway(REPLY)
    out = await TechIndustryAgent(gw).execute(
        _input(TechIndustryAgent, [])
    )
    assert out.confidence.value == "low"
    assert gw.calls == []


async def test_industry_agent_skips_when_no_watched_indicator():
    """有关注外数据但零命中（如科技任务只采到CPI/PPI）→ 不硬聊，跳过LLM。"""
    gw = FakeGateway(REPLY)
    out = await TechIndustryAgent(gw).execute(_input(TechIndustryAgent, [
        _dp("CPI", 0.4, "2026-07"), _dp("CPI", 0.5, "2026-08"),
    ], focus="软件"))
    assert out.confidence.value == "low"
    assert out.result["skipped"] is True
    assert out.result["industry_signal_calc"]["watched_indicator_count"] == 0
    assert gw.calls == []


async def test_industry_context_only_kept_six_latest_periods():
    """LLM上下文只含关注指标最近6期，更早期间不注入（省token且聚焦）。"""
    gw = FakeGateway(REPLY)
    points = [_dp("PPI同比", float(-i), f"2025-{m:02d}")
              for i, m in enumerate(range(1, 11), start=1)]
    await CyclicalIndustryAgent(gw).execute(_input(
        CyclicalIndustryAgent, points, focus="煤炭"))
    prompt = gw.calls[0]["prompt"]
    assert "2025-10" in prompt and "2025-05" in prompt  # 最近6期
    assert "2025-04" not in prompt and "2025-01" not in prompt  # 早期被截断
    assert "CPI" not in prompt  # 非关注指标不入上下文


async def test_valuation_flag_uses_latest_pe_period_not_first():
    """PE时序点必须取最新期：旧值48超线但最新39在区间内，应报常规区间。"""
    gw = FakeGateway(REPLY)
    out = await TechIndustryAgent(gw).execute(_input(TechIndustryAgent, [
        _dp("半导体出货量同比", 10.0, "2026-07"),
        _dp("半导体出货量同比", 12.0, "2026-08"),
        _dp("科技行业PE(TTM)", 48.0, "2025-09"),   # 早期高点
        _dp("科技行业PE(TTM)", 39.0, "2026-09"),   # 最新期
    ], focus="半导体"))
    val = out.result["industry_signal_calc"]["valuation"]
    assert "39" in val and "常规区间" in val
    assert "48" not in val


def test_agent_ids_and_capabilities():
    cases = [
        (TechIndustryAgent, "A13_tech", "penetration_rate_tracking"),
        (ConsumerIndustryAgent, "A14_consumer", "channel_inventory_tracking"),
        (CyclicalIndustryAgent, "A15_cyclical", "inventory_cycle_analysis"),
        (PharmaIndustryAgent, "A16_pharma", "pipeline_valuation"),
    ]
    gw = FakeGateway(REPLY)
    for cls, aid, capability in cases:
        agent = cls(gw)
        assert agent.agent_id == aid
        assert capability in agent.get_capabilities()["capabilities"]


# ---------- Supervisor行业路由 ----------

def test_route_industry_keywords():
    assert route_industry("半导体芯片") == ["A13_tech"]
    assert route_industry("白酒消费") == ["A14_consumer"]
    assert route_industry("煤炭有色") == ["A15_cyclical"]
    assert route_industry("创新药") == ["A16_pharma"]
    assert route_industry("AI算力") == ["A13_tech"]  # 英文关键词大小写不敏感


def test_route_industry_no_match():
    assert route_industry("某不知名板块") == []
    assert route_industry("") == []


def test_plan_industry_routes_specialist():
    plan = plan_run("industry", "半导体")
    assert "A09_meso" in plan["agents"]
    assert "A13_tech" in plan["agents"]
    assert "A14_consumer" not in plan["agents"]
    # 命中行业后下发专业产业指标（模拟连接器占位，付费接口替换同id）
    assert "ind:半导体销售额同比" in plan["indicators"]
    assert "ind:科技行业PE(TTM)" in plan["indicators"]
    assert "CPI" in plan["indicators"]  # 通用宏观背景仍保留给A09


def test_plan_industry_no_match_only_meso():
    plan = plan_run("industry", "未知板块XYZ")
    assert "A09_meso" in plan["agents"]
    assert not (set(INDUSTRY_AGENTS) & set(plan["agents"]))
    assert not [i for i in plan["indicators"] if i.startswith("ind:")]


def test_plan_full_does_not_force_industry_agents():
    """full类型不自动挂全部行业Agent（控制token成本，行业分析显式走industry）。"""
    plan = plan_run("full", "")
    assert not (set(INDUSTRY_AGENTS) & set(plan["agents"]))
