"""流动性能力集成测试：规划追加、作业采集管线、A17 hint注入。不联网。"""

from __future__ import annotations

import pytest

from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence
from src.domain.agents.decision.recommend.agent import RecommendationAgent
from src.orchestration.supervisor import (
    append_liquidity_indicators,
    plan_run,
)
from src.scheduler.jobs import _collect_through_pipeline, _finish_collection
from src.scheduler.run_log import RunLog

# ---------------- plan_run / 指标追加 ----------------

def test_stock_plan_always_includes_market_liquidity():
    plan = plan_run("stock", "300308",
                    query="基于当前宏观流动性，中际旭创还能买入持有3-6个月吗")
    inds = plan["indicators"]
    for required in ("mkt:turnover:total", "mkt:turnover:hist",
                     "mkt:turnover_rate:all_a", "mkt:margin_balance",
                     "mkt:north_flow", "idx_val:snapshot:all"):
        assert required in inds
    # 问题含宏观流动性语义 → FedWatch
    assert "fed:rate_prob:next" in inds
    # 不重复追加
    assert inds.count("mkt:turnover:total") == 1


def test_industry_plan_includes_liquidity_without_fed():
    plan = plan_run("industry", "光模块", query="光模块行业景气度")
    inds = plan["indicators"]
    assert "mkt:turnover:total" in inds
    assert "fed:rate_prob:next" not in inds  # 无海外流动性关键词


def test_macro_plan_with_a_share_keywords_gets_market_data():
    plan = plan_run("macro", "", query="当前A股两市成交额和两融余额是什么水平")
    inds = plan["indicators"]
    assert "mkt:turnover:total" in inds
    assert "mkt:margin_balance" in inds


def test_plain_macro_plan_excludes_market_data():
    plan = plan_run("macro", "", query="8月CPI同比多少")
    inds = plan["indicators"]
    assert not any(i.startswith(("mkt:", "idx_val:")) for i in inds)


def test_append_helper_dedup_and_fed_keyword():
    out = append_liquidity_indicators(
        "industry", "美联储降息预期",
        ["mkt:turnover:total", "mkt:turnover:total"])
    assert out.count("mkt:turnover:total") == 1
    assert "fed:rate_prob:next" in out


# ---------------- skill 1.1 条件触发：大盘预测/复盘/短线买卖/板块策略 ----------------

_BOARD_INDS = (
    "mkt:cybkcb:turnover:all", "mkt:cybkcb:val:all",
    "mkt:cybkcb:spot_summary",
)


@pytest.mark.parametrize("query", [
    "预测明日A股走势，大盘会涨吗",
    "复盘一下今天股市，为什么指数涨了个股不涨",
    "对当前A股后市怎么看",
])
def test_macro_prediction_and_review_queries_trigger_skill(query):
    """大盘预测/复盘类macro问题：确定性追加大盘+双创板块流动性指标。"""
    plan = plan_run("macro", "", query=query)
    inds = plan["indicators"]
    assert "mkt:turnover:total" in inds
    assert "mkt:turnover:hist" in inds
    for board in _BOARD_INDS:
        assert board in inds


def test_stock_short_term_trade_advice_triggers_board_indicators():
    plan = plan_run("stock", "300308", query="中际旭创短线怎么操作，给个买卖建议和止损位")
    inds = plan["indicators"]
    for board in _BOARD_INDS:
        assert board in inds


def test_industry_sector_strategy_triggers_board_indicators():
    plan = plan_run("industry", "半导体", query="半导体板块交易策略，现在还能追吗，如何轮动")
    inds = plan["indicators"]
    assert "mkt:turnover:total" in inds
    for board in _BOARD_INDS:
        assert board in inds


def test_us_macro_prediction_not_false_trigger_board_data():
    """纯美国宏观预测（无A股语境词）不得误触发A股/双创指标。

    ## ★ 2026-09-30 本判据被**更新过一次**（如实记）

    原来断言的是 `"us_nonfarm" in inds`（老的 Tushare 口径序列）。
    那条断言现在**必须失败**，而且**不该被"修好"** —— 因为 `us_nonfarm` 已经
    **源停更**：实测库里 **107 条、最新期 2025-08-01**（停在 13 个月前），
    而接替它的 `fred:PAYEMS` 有 **1052 条、最新期 2026-08-01**。

    也就是说：要求规划器追加 `us_nonfarm`，等于要求它去排一条
    **必然取不到数据的序列** —— 正是 `AGENTS.md`《必然失败的指标不许留在
    喂给 LLM 的菜单里》要挡的事（本项目为此付过代价：`股息率` 每次都失败）。
    `_US_RATES_INDICATORS` 里放的也确实是 `fred:PAYEMS`。

    ⇒ 判据改成断言**当前存活**的那条序列（断言的是"美国非农**这一维**仍在
    计划里"这个意图，而不是某个具体来源名）。
    """
    plan = plan_run("macro", "", query="预测下个月美国非农就业和美联储利率决议")
    inds = plan["indicators"]
    assert not any(i.startswith("mkt:") for i in inds)
    assert not any(i.startswith("mkt:cybkcb") for i in inds)
    # 美国宏观指标仍正常追加：非农那一维在（用**存活**的 FRED 序列）
    assert "fred:PAYEMS" in inds, (
        "美国非农这一维没进计划 —— `_US_RATES_INDICATORS` 或触发词漏了")
    #: 反向判据：**不许**再去排已停更的 `us_nonfarm`（否则必然取不到）
    assert "us_nonfarm" not in inds, (
        "计划里出现了源停更的 `us_nonfarm`（库内最新期 2025-08-01）—— "
        "「必然失败的指标不许留在菜单里」")


def test_append_helper_includes_board_indicators():
    out = append_liquidity_indicators("stock", "300308 短线加仓建议", [])
    for board in _BOARD_INDS:
        assert board in out


# ---------------- 作业采集管线 ----------------

class _StageAgent:
    def __init__(self, aid: str, result: dict, outputs: list | None = None):
        self.agent_id = aid
        self._result = result
        self.calls: list[dict] = []
        self._outputs = outputs

    async def execute(self, input: AgentInput) -> AgentOutput:
        self.calls.append(input.payload)
        if self._outputs is not None:
            result = self._outputs[len(self.calls) - 1]
        else:
            result = self._result
        return AgentOutput(
            task_id=input.task_id, agent_id=self.agent_id,
            conclusion="", confidence=Confidence.HIGH, trace_id=input.task_id,
            result=result,
        )


class _FakeRuntime:
    def __init__(self, agents):
        self.agents = agents


@pytest.mark.asyncio
async def test_collect_through_pipeline_a01_to_a04():
    collected = [{"indicator": "mkt:turnover:total", "value": 16000.0}]
    a01 = _StageAgent("A01_data_collector", {"data_points": collected})
    a02 = _StageAgent("A02_data_cleaner", {"data_points": collected})
    a03 = _StageAgent("A03_data_validator", {"data_points": collected})
    a04 = _StageAgent("A04_data_storage",
                      {"storage_stats": {"total": 1, "inserted": 1, "skipped": 0}})
    runtime = _FakeRuntime({"A01_data_collector": a01, "A02_data_cleaner": a02,
                            "A03_data_validator": a03, "A04_data_storage": a04})
    total, errors = await _collect_through_pipeline(
        runtime, ["mkt:turnover:total"], "liq_intra")
    assert total == 1 and errors == []
    # A04确实收到了数据（管线贯通而非只采集）
    assert a04.calls[0]["data_points"] == collected
    # A02/A03依次收到上一阶段输出
    assert a02.calls[0]["data_points"] == collected
    assert a03.calls[0]["data_points"] == collected


@pytest.mark.asyncio
async def test_collect_pipeline_one_indicator_failure_does_not_block():
    class _BoomA01:
        agent_id = "A01_data_collector"

        async def execute(self, input):
            raise RuntimeError("A01采集失败")

    a02 = _StageAgent("A02_data_cleaner", {"data_points": []})
    a03 = _StageAgent("A03_data_validator", {"data_points": []})
    a04 = _StageAgent("A04_data_storage",
                      {"storage_stats": {"total": 0, "inserted": 0, "skipped": 0}})

    class _Rt:
        agents = {"A01_data_collector": _BoomA01(), "A02_data_cleaner": a02,
                  "A03_data_validator": a03, "A04_data_storage": a04}

    # 两个指标全部失败也不抛异常，错误逐个记录
    total, errors = await _collect_through_pipeline(
        _Rt(), ["bad_one", "bad_two"], "t")
    assert total == 0
    assert len(errors) == 2
    assert any("bad_one" in e for e in errors)


# ---------------- A17 hint渲染 ----------------

def test_a17_prompt_contains_liquidity_hint_and_return_spec():
    agent = RecommendationAgent(gateway=None)  # type: ignore[arg-type]
    payload = agent._parse_payload({  # noqa: SLF001
        "analyses": [{"agent_id": "A10_micro", "conclusion": "基本面强",
                      "confidence": "medium", "result": {}}],
        "focus": "中际旭创",
        "user_query": "还能买入持有3-6个月吗，收益预期多少",
        "hint": {"market_liquidity": {
            "liquidity_phase": "存量博弈（哑铃/跷跷板轮动）",
            "suggested_position_pct": [40, 50],
            "turnover": {"total_yi": 16291.8, "as_of": "2026-09-14",
                         "ma5_yi": 17000.0, "ma10_yi": 16500.0,
                         "ma50_yi": 15800.0, "vs_ma5_pct": -4.2},
            "turnover_rate": {"all_a_weighted_pct": 2.1},
            "margin": {"balance_yi": 19850.0, "coverage": "沪深两市",
                       "as_of": "2026-09-11", "change_5d_pct": -0.3},
            "northbound": {"status": "disclosure_halted",
                           "halted_since": "2024-08-19",
                           "last_net_buy_yi": 42.3,
                           "last_date": "2024-08-16"},
            "index_valuation": [{"index_name": "创业板指", "pe_ttm": 35.0,
                                 "pe_pct_5y": 45.0}],
            "fedwatch": {"status": "unavailable"},
            "signals": [], "risk_alerts": [],
            "data_gaps": ["北向资金日度净买额（2024-08-19起停止披露）",
                          "CME FedWatch利率概率"],
            "summary_text": "流动性阶段：存量博弈（哑铃/跷跷板轮动），对应建议总仓位区间40-50%。\n"
                            "[数据缺口] 北向资金日度净买额（2024-08-19起停止披露）\n"
                            "[数据缺口] CME FedWatch利率概率",
        }},
    })
    prompt = agent.build_prompt(payload)
    assert "存量博弈" in prompt
    assert "40-50" in prompt
    assert "expected_return_3_6m" in prompt
    assert "position_advice" in prompt
    assert "数据缺口" in prompt
    assert "中际旭创" in prompt


# ---------------- 多指标采集作业状态：success/partial/failed ----------------

def test_finish_collection_all_success(tmp_dir):
    log = RunLog(f"{tmp_dir}/runs.jsonl")
    rec = _finish_collection(log.start("j", "manual"), log, 5, [])
    assert rec["status"] == "success" and rec["records_processed"] == 5


def test_finish_collection_partial_does_not_pause(tmp_dir):
    """东财限流但腾讯成交额入库 → partial，中断连续失败计数不触发自动暂停。"""
    log = RunLog(f"{tmp_dir}/runs.jsonl")
    rec = _finish_collection(
        log.start("market_intraday_snapshot"), log, 1,
        ["mkt:turnover_rate:all_a: 东财clist失败"])
    assert rec["status"] == "partial" and rec["records_processed"] == 1
    assert "turnover_rate" in rec["error_message"]
    assert log.is_paused("market_intraday_snapshot") is False


def test_finish_collection_all_failed(tmp_dir):
    log = RunLog(f"{tmp_dir}/runs.jsonl")
    rec = _finish_collection(
        log.start("market_intraday_snapshot"), log, 0, ["boom"])
    assert rec["status"] == "failed" and rec["records_processed"] == 0


def test_three_consecutive_failures_pause_but_partial_resets(tmp_dir):
    from src.scheduler.registry import PAUSE_AFTER_CONSECUTIVE_FAILURES

    log = RunLog(f"{tmp_dir}/runs.jsonl")
    assert PAUSE_AFTER_CONSECUTIVE_FAILURES == 3
    _finish_collection(log.start("j"), log, 0, ["e1"])
    _finish_collection(log.start("j"), log, 0, ["e2"])
    assert log.is_paused("j") is False
    # 第3次partial → 连续失败中断，不暂停
    _finish_collection(log.start("j"), log, 2, ["e3"])
    assert log.is_paused("j") is False
    # 再来3次全失败才暂停
    for i in range(3):
        _finish_collection(log.start("j"), log, 0, [f"f{i}"])
    assert log.is_paused("j") is True
