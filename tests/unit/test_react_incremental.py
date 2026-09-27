"""ReAct 增量传递协议的单元测试（2026-09-27 A17 token 优化）。

守卫三件事：
  1. **第 2+ 步不重发全量**：prompt 只含"任务头 + 上次输出摘要 + 新观察 +
     输出 schema"，不再包含上游分析的大 payload（−60% tokens_in 的来源）；
  2. **observation 必须送达模型**：增量步如果只带"上次输出"不带观察结果，
     ReAct 的 Observe 环节就整个丢了（2026-09-27 修复的真 bug）；
  3. **工具描述不丢**：`describe_all` 曾经被误写成 join(键名)，
     LLM 拿不到参数说明。
"""

from __future__ import annotations

import json

from src.domain.agents.analysis.react import ReActExecutor, ToolRegistry


class FakeGateway:
    """记录每步 prompt 的网关替身。"""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    async def complete(self, tier, system, prompt, **kwargs):
        self.prompts.append(prompt)
        return type("R", (), {"content": self._replies.pop(0)})()


def _tools() -> ToolRegistry:
    tools = ToolRegistry()

    async def _query_data(indicator: str, limit: int = 10) -> str:
        return f"- 2026-08-01: 2.1 (来源AkShare) 指标{indicator}"

    tools.register("query_data", _query_data,
                   "查询已采集的数据点，参数indicator=指标名, limit=条数")
    return tools


def _task_prompt() -> str:
    """模拟 A17 build_prompt 的结构（react_mode + compact）。"""
    return (
        "## 投研标的/主题\n600519\n"
        "## 用户问题（conclusion必须直接回答）\n茅台当前估值如何？\n\n"
        "## 上游分析结论\n"
        "### A08_macro（置信度:medium）\n结论: CPI温和复苏，流动性中性。\n\n"
        "### A10_micro（置信度:high）\n结论: 估值处于低位。关键数值: pe_percentile=15.0\n\n"
        "## 本地量化参考（流动性周期skill）\n总量阶段：放量。\n\n"
        "## 任务要求\n"
        '输出JSON二选一：{"action": {...}} 或 {"final_answer": {...}}'
    )


async def test_incremental_step2_gets_observation_not_full_payload():
    """第 2 步：有观察、有 schema、无上游大块。"""
    gw = FakeGateway([
        json.dumps({"action": {"name": "query_data",
                               "args": {"indicator": "PE(TTM)"}}}),
        json.dumps({"final_answer": {"conclusion": "估值低位", "confidence": "high"}}),
    ])
    react = ReActExecutor(gw, _tools(), max_steps=3, task_tier="decision")
    data = await react.run("你是投研委员会主席。", _task_prompt(),
                           agent_id="A17_recommend", trace_id="t1")

    assert data["conclusion"] == "估值低位"
    assert len(gw.prompts) == 2

    first, second = gw.prompts
    # 第 1 步：全量任务（含上游分析与任务要求）
    assert "## 任务" in first
    assert "上游分析结论" in first
    assert "任务要求" in first

    # 第 2 步（增量）：
    assert "续推" in second
    assert "query_data" in second                     # 上次动作摘要
    assert "PE(TTM)" in second                        # ★ 新观察里的工具结果
    assert "来源AkShare" in second                    # ★ observation 本体送达
    assert "任务要求" in second                       # ★ 输出 schema 保留
    assert "茅台当前估值如何" in second               # 任务头（用户问题）保留
    # 不再重发的大块：
    assert "上游分析结论" not in second
    assert "A08_macro" not in second
    assert "本地量化参考" not in second


async def test_incremental_parse_error_note_reaches_next_step():
    """第 1 步输出非法 JSON → 提示作为"新观察"进入第 2 步，而不是丢失。"""
    gw = FakeGateway([
        "这不是JSON",
        json.dumps({"final_answer": {"conclusion": "ok", "confidence": "medium"}}),
    ])
    react = ReActExecutor(gw, _tools(), max_steps=3, task_tier="decision")
    data = await react.run("sys", _task_prompt(),
                           agent_id="A17_recommend", trace_id="t2")

    assert data["conclusion"] == "ok"
    second = gw.prompts[1]
    assert "非合法JSON" in second                     # 解析失败提示送达


async def test_legacy_mode_resends_everything():
    """incremental=False 保持旧行为：每步全量任务 + 累积 scratchpad。"""
    gw = FakeGateway([
        json.dumps({"action": {"name": "query_data",
                               "args": {"indicator": "PE(TTM)"}}}),
        json.dumps({"final_answer": {"conclusion": "ok", "confidence": "high"}}),
    ])
    react = ReActExecutor(gw, _tools(), max_steps=3, task_tier="decision",
                          incremental=False)
    await react.run("sys", _task_prompt(),
                    agent_id="A17_recommend", trace_id="t3")

    second = gw.prompts[1]
    assert "## 任务" in second
    assert "上游分析结论" in second                   # 旧模式：全量重发
    assert "来源AkShare" in second                    # scratchpad 带观察


def test_tool_registry_describes_tools_with_parameters():
    """describe_all 必须带描述（曾经被误写成只返回工具名）。"""
    tools = _tools()
    desc = tools.describe_all()
    assert "query_data" in desc
    assert "indicator" in desc                          # 参数说明在
