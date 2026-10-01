"""ReAct推理循环执行器（Reasoning → Acting → Observing）。

由于LLM提供商不支持原生tool calling，本模块用JSON输出模拟工具调用：
LLM输出 {"action": {"name", "args"}} 触发工具执行，结果喂回LLM继续推理，
直到输出 {"final_answer": {...}} 或达到最大步数。

工具注册表提供可扩展的工具集（ask_agent/query_data/list_outputs等），
Agent可通过ReAct循环主动获取信息而非一次性被动综合。

## ★★★ Token优化（2026-09-27 第八轮）：增量传递协议

**问题**（审计实证，984 次 LLM 调用基线）：
  · A17 ReAct prompt = **7,582 tokens**（上游全量 `result` JSON，只剔了 3 个字段）。
  · 3 步 = 同一份 payload 发 3 次 → 169 次调用 = 356,911 tokens_in / 411,726 tokens_out，
    跑在 deepseek-v4-pro（4.5 / 13.5 元每百万）→ **约占全系统成本 55%**。
  · 其中 **98 次输出撞 4096 上限**。

**改造**：
  · Step 1：发"原始任务 + 全量上游摘要（轻量版）"—— 上游 analyses 由调用方预压缩
    （不是 raw JSON，是 `{agent_id, conclusion摘要≤60字, confidence, 2-3个关键数值}`）。
  · Step 2+：**只发**"上次 LLM 输出的 action/thinking + 本步新 observation"，
    不再重发上游分析与原始任务。
  · Observation 仍按 800 字符截断。

**收益（实测目标）**：
  · A17 tokens_in ≈ −55%（平均从 7,582 → ~3,400）
  · A17 tokens_out 下降有限（输出受 max_tokens 影响）
  · 全系统成本 −55% / 0.20 元/轮 → 0.09 元/轮
  · 月成本 200 元 → 90 元
"""
from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Any

from src.core.cancel import CancellationToken
from src.domain.agents.analysis.base import parse_llm_json
from src.infrastructure.llm import LLMGateway

# 观测结果截断字符数：ReAct多步循环中每步观测累积注入prompt，
# 800字符×5步=4000字符上限，防止prompt膨胀超输入硬上限
_OBS_TRUNCATE = 800

#: 上一次LLM输出（action+thinking）的截断字符数。
#: Step 2+ 只发这个，不再重发上游7K tokens。
_LAST_LM_SNIPPET = 600


def _summarize_lm_output(data: dict[str, Any]) -> str:
    """把上一次 LLM 的输出压缩成短摘要（用于 step 2+ 注入）。

    只保留：
      · thinking：推理思路（≤300 字）
      · action.name / action.args：决定下一步的工具调用
      · 不要 final_answer（那是终态）
    """
    parts: list[str] = []
    thinking = data.get("thinking") or data.get("thought")
    if isinstance(thinking, str) and thinking.strip():
        parts.append(f"推理：{thinking.strip()[:300]}")
    action = data.get("action")
    if isinstance(action, dict):
        name = str(action.get("name", ""))
        args = action.get("args") or {}
        # args 一般是 dict，转短 JSON
        try:
            args_short = json.dumps(args, ensure_ascii=False)[:200]
        except (TypeError, ValueError):
            args_short = str(args)[:200]
        parts.append(f"动作：{name}({args_short})")
    if not parts:
        # 兜底：整段截断
        try:
            parts.append(json.dumps(data, ensure_ascii=False)[:_LAST_LM_SNIPPET])
        except (TypeError, ValueError):
            parts.append(str(data)[:_LAST_LM_SNIPPET])
    return "\n".join(parts)


class ToolRegistry:
    """工具注册表：name → {func, description}。"""

    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}

    def register(
        self, name: str, func: Callable[..., Coroutine[Any, Any, str]],
        description: str,
    ) -> None:
        self._tools[name] = {"func": func, "description": description}

    def has(self, name: str) -> bool:
        return name in self._tools

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"（工具不存在：{name}）"
        try:
            result = await tool["func"](**args)
            return str(result)
        except Exception as exc:  # noqa: BLE001
            return f"（工具{name}执行失败：{exc}）"

    def describe_all(self) -> str:
        lines = []
        for name, tool in self._tools.items():
            lines.append(f"- {name}: {tool['description']}")
        # ★ 注意返回 lines（工具名+描述）；曾经被误写成 join(self._tools)
        # 只剩工具名，LLM 拿不到参数说明 → 工具调用参数全靠猜。
        return "\n".join(lines)


class ReActExecutor:
    """ReAct循环：LLM推理 → 工具调用 → 观察结果 → 再推理，直到给出最终答案。

    ★ 2026-09-27 增量协议（向后兼容）：
      · 第 1 步发 `initial_prompt`（调用方传入的完整 prompt，已包含压缩后的上游 analyses）
      · 第 2+ 步只发"上次 LLM 输出摘要 + 本步新增 observation"，**不再重发上游分析**
    """

    def __init__(
        self, gateway: LLMGateway, tools: ToolRegistry, *,
        max_steps: int = 3, task_tier: str = "reasoning",
        incremental: bool = True,
    ) -> None:
        self._gateway = gateway
        self._tools = tools
        self._max_steps = max_steps
        self._task_tier = task_tier
        # ★ 默认开启增量协议；要恢复旧行为传 `incremental=False`
        self._incremental = incremental

    async def run(
        self, system: str, prompt: str, *,
        agent_id: str, trace_id: str, json_mode: bool = True,
        cancel_token: CancellationToken | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """执行ReAct循环，返回final_answer的JSON内容。

        Args:
            prompt: 第 1 步使用的完整 prompt（已由调用方压缩）。
                    第 2+ 步会被丢弃，改为"上次 LLM 输出摘要 + 新 observation"。
            system: 系统提示（每一步都会带上，包含工具描述）。
            reasoning_effort: ★ 第十轮新增 —— 覆盖该次调用的思维链强度。
                    A17 step 1 默认 "high"（decision 层级映射），但合成类任务
                    砍到 "low" 可省 30-50% 输出 token + 墙钟（详见
                    `INTERVIEW_FAQ_SESSION_20260926.md` §4）。
        """
        tool_descs = self._tools.describe_all()
        full_system = (
            f"{system}\n\n你可以使用以下工具来获取额外信息：{tool_descs}\n\n"
            "推理流程：先思考是否需要调用工具，如果需要则输出 "
            '{"action": {"name": "工具名", "args": {...}}}；'
            "如果信息已足够则输出 {\"final_answer\": {你的最终JSON答案}}。"
            "工具调用结果会作为观察返回给你，你可以继续调用或给出最终答案。"
        )

        last_data: dict[str, Any] = {}
        # 累积 scratchpad（非增量模式每步全量重发）
        scratchpad = ""
        # ★ 自上一次 LLM 调用以来的**新**观察（增量模式第 2+ 步注入后清空）。
        # 2026-09-27 修正：增量步如果只带"上次输出摘要"而不带 observation，
        # 模型根本看不到工具返回结果 —— ReAct 的 Observe 环节被整个丢掉了。
        pending_obs = ""

        # 增量步保留的**最小任务上下文**：头部（标的/用户问题）+ 尾部（任务要求/
        # 输出 schema）。丢掉的大头是"上游分析结论"与"本地量化参考"——只发第 1 步。
        head_marker = "## 上游分析结论"
        tail_marker = "## 任务要求"
        head_at = prompt.find(head_marker)
        tail_at = prompt.rfind(tail_marker)
        task_head = prompt[:head_at].strip() if head_at > 0 else prompt[:600]
        task_tail = prompt[tail_at:].strip() if tail_at > 0 else prompt[-800:]

        for step in range(self._max_steps):
            # 取消检查：用户停止时终止ReAct循环
            if cancel_token is not None:
                cancel_token.check()

            # ★ 构造本步 prompt：增量协议 vs. 旧协议
            if step == 0 or not self._incremental:
                # 第 1 步或旧模式：发完整任务 + 累积 scratchpad
                current_prompt = f"## 任务\n{prompt}\n"
                if scratchpad:
                    current_prompt += f"\n{scratchpad}\n"
                pending_obs = ""  # 旧模式 scratchpad 已全量带上，无需另发
            else:
                # 第 2+ 步（增量）：任务头 + 上次输出摘要 + **新观察** + 输出 schema。
                # 不再重发上游分析与量化参考；目标 tokens_in ≈ step1 的 30~40%
                current_prompt = (
                    f"{task_head}\n\n"
                    "## 续推（增量）\n"
                    "原始任务与上游分析已在第 1 步提供，不要再要求重发；"
                    "信息不足可调用工具。\n\n"
                    f"### 上一次输出\n{_summarize_lm_output(last_data)}\n"
                )
                if pending_obs:
                    current_prompt += f"\n### 新观察（第 1 步之后）\n{pending_obs}"
                current_prompt += f"\n\n{task_tail}"
                pending_obs = ""

            is_last = step == self._max_steps - 1
            if is_last:
                current_prompt += (
                    "\n## 重要提醒\n这是最后一轮，"
                    "必须直接输出 final_answer，禁止再调用工具。\n"
                )

            response = await self._gateway.complete(
                self._task_tier, full_system, current_prompt,
                agent_id=agent_id, trace_id=trace_id, json_mode=json_mode,
                use_cache=(step == 0),
                reasoning_effort=reasoning_effort,  # ★ 第十轮：透传给网关
            )
            try:
                data = parse_llm_json(agent_id, response.content)
            except Exception:  # noqa: BLE001
                data = {"_parse_error": True}
                note = f"\n## 观察（第{step+1}步）\n输出非合法JSON，重新推理。\n"
                scratchpad += note
                pending_obs += note
                last_data = data
                continue

            last_data = data

            if "final_answer" in data:
                return data["final_answer"] if isinstance(data["final_answer"], dict) else data

            action = data.get("action")
            if not action or not isinstance(action, dict):
                return data

            name = str(action.get("name", ""))
            args = action.get("args") or {}
            if not self._tools.has(name):
                note = (
                    f"\n## 观察（第{step+1}步）\n工具{name}不存在，可用工具："
                    f"{', '.join(self._tools._tools.keys())}\n"
                )
                scratchpad += note
                pending_obs += note
                continue
            obs = await self._tools.execute(name, args)
            obs_short = str(obs)[:_OBS_TRUNCATE]
            note = (
                f"\n## 第{step+1}步动作：调用 {name}({args})\n"
                f"## 观察结果\n{obs_short}\n"
            )
            scratchpad += note
            pending_obs += note

        # 达到最大步数仍未输出final_answer，返回最后一次输出（兜底）
        return last_data.get("final_answer", last_data)
