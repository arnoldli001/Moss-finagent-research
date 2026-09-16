"""ReAct推理循环执行器（Reasoning → Acting → Observing）。

由于LLM提供商不支持原生tool calling，本模块用JSON输出模拟工具调用：
LLM输出 {"action": {"name", "args"}} 触发工具执行，结果喂回LLM继续推理，
直到输出 {"final_answer": {...}} 或达到最大步数。

工具注册表提供可扩展的工具集（ask_agent/query_data/list_outputs等），
Agent可通过ReAct循环主动获取信息而非一次性被动综合。

Token节约：观测结果截断至800字符（从1500降），避免多步累积淹没prompt；
max_steps=5时最大观测总量4000字符，确保总prompt不超输入硬上限。
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from src.core.cancel import CancellationToken
from src.domain.agents.analysis.base import parse_llm_json
from src.infrastructure.llm import LLMGateway

# 观测结果截断字符数：ReAct多步循环中每步观测累积注入prompt，
# 800字符×5步=4000字符上限，防止prompt膨胀超输入硬上限
_OBS_TRUNCATE = 800


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
        except Exception as exc:  # noqa: BLE001 工具执行失败返回说明，不阻断循环
            return f"（工具{name}执行失败：{exc}）"

    def describe_all(self) -> str:
        lines = []
        for name, tool in self._tools.items():
            lines.append(f"- {name}: {tool['description']}")
        return "\n".join(lines)


class ReActExecutor:
    """ReAct循环：LLM推理 → 工具调用 → 观察结果 → 再推理，直到给出最终答案。"""

    def __init__(
        self, gateway: LLMGateway, tools: ToolRegistry, *,
        max_steps: int = 3, task_tier: str = "reasoning",
    ) -> None:
        self._gateway = gateway
        self._tools = tools
        self._max_steps = max_steps
        self._task_tier = task_tier

    async def run(
        self, system: str, prompt: str, *,
        agent_id: str, trace_id: str, json_mode: bool = True,
        cancel_token: CancellationToken | None = None,
    ) -> dict[str, Any]:
        """执行ReAct循环，返回final_answer的JSON内容。"""
        tool_descs = self._tools.describe_all()
        full_system = (
            f"{system}\n\n你可以使用以下工具来获取额外信息：\n{tool_descs}\n\n"
            "推理流程：先思考是否需要调用工具，如果需要则输出 "
            '{"action": {"name": "工具名", "args": {...}}}；'
            "如果信息已足够则输出 {\"final_answer\": {你的最终JSON答案}}。"
            "工具调用结果会作为观察返回给你，你可以继续调用或给出最终答案。"
        )
        conversation = f"## 任务\n{prompt}\n"
        last_data: dict[str, Any] = {}
        for step in range(self._max_steps):
            # 取消检查：用户停止时终止ReAct循环
            if cancel_token is not None:
                cancel_token.check()
            current_prompt = conversation
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
            )
            try:
                data = parse_llm_json(agent_id, response.content)
            except Exception:  # noqa: BLE001
                conversation += f"\n## 观察（第{step+1}步）\n输出非合法JSON，重新推理。\n"
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
                conversation += (
                    f"\n## 观察（第{step+1}步）\n工具{name}不存在，可用工具："
                    f"{', '.join(self._tools._tools.keys())}\n"
                )
                continue
            obs = await self._tools.execute(name, args)
            conversation += (
                f"\n## 第{step+1}步动作：调用 {name}({args})\n"
                f"## 观察结果\n{obs[:_OBS_TRUNCATE]}\n"
            )

        # 达到最大步数仍未输出final_answer，返回最后一次输出（兜底）
        return last_data.get("final_answer", last_data)
