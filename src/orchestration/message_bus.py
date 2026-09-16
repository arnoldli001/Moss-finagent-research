"""Agent间多轮对话（A2A）消息总线。

PRD要求"A2A作为Agent互操作层，实现Agent间通信协作"。本模块提供：
- AgentMessage: 标准消息结构（sender/receiver/content/timestamp）
- ask_agent: 同步向目标Agent提问并获取回答，问答记录由调用方写入state

各层Agent的execute payload契约不同，追问时必须按目标Agent所属层适配：
- 分析层(A08-A12)/行业层(A13-A16)：AnalysisPayload（focus/user_query/data_points/events）
- 信息层(A05/A06/A07)：各自info_items/events契约
- 数据层(A01-A04)/审计层(A18)：非问答型Agent，不调用execute，返回引导性说明
"""

from __future__ import annotations

import re
import time
from typing import Any
from uuid import uuid4

from src.core.models import AgentInput, AgentOutput


def normalize_question(question: str) -> str:
    """归一化提问文本用于幂等判重：去标点/空白/大小写（保留中日韩字符）。"""
    text = re.sub(r"[\s\W_]+", "", str(question).lower(), flags=re.UNICODE)
    return text

# 可接受定性追问的Agent层级（按agent_id前缀，命名规范见architecture-naming-spec）
_ANALYSIS_PREFIXES = ("A08", "A09", "A10", "A11", "A12",
                      "A13", "A14", "A15", "A16")


def make_message(
    sender: str,
    receiver: str,
    content: str,
    *,
    message_type: str = "question",
    reply_to: str = "",
) -> dict[str, Any]:
    """构造一条A2A消息（与PRD 2.3内部消息格式对齐）。"""
    return {
        "message_id": f"msg_{uuid4().hex[:12]}",
        "sender": sender,
        "receiver": receiver,
        "message_type": message_type,  # question / answer
        "content": content,
        "timestamp": time.time(),
        "reply_to": reply_to,
    }


def _is_analysis_agent(receiver: str) -> bool:
    return receiver.startswith(_ANALYSIS_PREFIXES)


def _collected_indicators(state: dict[str, Any]) -> list[str]:
    """state中已采集/校验过的指标目录（去重保序）。"""
    seen: list[str] = []
    for p in state.get("validated_points") or state.get("raw_points") or []:
        ind = str((p or {}).get("indicator", ""))
        if ind and ind not in seen:
            seen.append(ind)
    return seen


def _non_qa_answer(receiver: str, state: dict[str, Any]) -> str:
    """数据层/审计层等非问答型Agent收到追问时的引导性回答。"""
    indicators = _collected_indicators(state)
    if receiver.startswith(("A01", "A02", "A03", "A04")):
        ind_text = "、".join(indicators[:15]) if indicators else "无"
        return (
            f"{receiver}是数据层Agent，仅负责数据采集/清洗/校验/入库，"
            "不直接回答定性问题。当前已采集指标："
            f"{ind_text}。如需具体数值，请改用 query_data 工具按指标名查询。"
        )
    if receiver == "A18_audit":
        return "A18是审计Agent，仅负责哈希链与完整性校验，不提供投研观点。"
    if receiver == "A17_recommend":
        return "A17是综合决策Agent，不能向自己提问。"
    return f"{receiver}不支持直接问答。"


def _build_payload(receiver: str, question: str, state: dict[str, Any]) -> dict[str, Any] | None:
    """按目标Agent所属层构造其execute能解析的payload；非问答型返回None。"""
    focus = state.get("target_display") or state.get("target", "")
    events = (state.get("extracted_events") or {}).get("events", [])
    if _is_analysis_agent(receiver):
        # AnalysisPayload（src/domain/agents/analysis/base.py）
        return {
            "focus": focus,
            "user_query": question,
            "data_points": state.get("validated_points", []),
            "events": events,
        }
    if receiver == "A05_verifier":
        return {"info_items": state.get("info_items", [])}
    if receiver == "A06_extractor":
        verified = (state.get("verified_items") or {}).get("items", [])
        return {"info_items": [i for i in verified if i.get("verified")]}
    if receiver == "A07_sentiment":
        return {"events": events}
    return None


async def ask_agent(
    sender: str,
    receiver: str,
    question: str,
    agents: dict[str, Any],
    state: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """向目标Agent提问并返回(回答摘要, 产生的消息列表)。

    消息列表由调用方通过节点返回值写入state（LangGraph reducer只追踪返回值，
    原地修改state的list不会被持久化）。调用方应自行做"同一问题不重复询问"的幂等。
    """
    question_msg = make_message(sender, receiver, question, message_type="question")
    messages: list[dict[str, Any]] = [question_msg]

    agent = agents.get(receiver)
    if agent is None:
        answer = f"（{receiver}未注册，无法回答；请只使用工具说明中列出的可用Agent）"
    else:
        payload = _build_payload(receiver, question, state)
        if payload is None:
            # 数据层/审计层：不调用execute，避免payload契约不匹配报错
            answer = _non_qa_answer(receiver, state)
        else:
            try:
                output: AgentOutput = await agent.execute(AgentInput(
                    task_id=state.get("task_id", ""),
                    tenant_id=state.get("tenant_id", ""),
                    payload=payload,
                ))
                answer = output.conclusion
            except Exception as exc:  # noqa: BLE001 追问失败不阻断主链路
                answer = f"（{receiver}回答失败：{exc}）"

    answer_msg = make_message(
        receiver, sender, answer, message_type="answer",
        reply_to=question_msg["message_id"],
    )
    messages.append(answer_msg)
    return answer, messages
